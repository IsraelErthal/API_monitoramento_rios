"""Previsão em MODO SOMBRA: grava a previsão do modelo e depois compara com o que de fato ocorreu.
Nada aqui altera risco ou alertas.

  python -m app.ml.predict --station rio-piloto-01              # uma previsão (agendar a cada 10 min)
  python -m app.ml.predict --station rio-piloto-01 --allow-unapproved   # só para testes (mode='test')

O pipeline também chama isto a cada leitura nova quando existe um modelo em MODELS_DIR.
"""
import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Prediction, Reading, Station

log = logging.getLogger("ml.predict")
MAX_AGE_MIN = 60                  # não prevê a partir de dado velho
_cache: dict = {}


def models_dir() -> Path:
    return Path(os.environ.get("MODELS_DIR", "models_store"))


def latest_version(directory: Path | None = None, allow_unapproved: bool = False,
                   station_id: str | None = None) -> Path | None:
    """Versão mais recente utilizável para a estação.

    Modelos treinados com leituras do banco ficam vinculados à estação. Isso evita que o
    comportamento hidrológico de um rio seja usado por engano para prever outro.
    """
    directory = directory or models_dir()
    if not directory.is_dir():
        return None
    for folder in sorted((d for d in directory.iterdir() if (d / "meta.json").is_file()), reverse=True):
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        if station_id is not None and meta.get("station_id") != station_id:
            continue
        if meta.get("approved") or allow_unapproved:
            return folder
    return None


def _load(folder: Path):
    import joblib
    meta_file = folder / "meta.json"
    key = (str(folder), meta_file.stat().st_mtime)
    if key not in _cache:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        models = {int(h): joblib.load(folder / f"model_{h}h.joblib") for h in meta["horizons_h"]}
        _cache.clear()
        _cache[key] = (meta, models)
    return _cache[key]


def predict_station(db: Session, station_id: str, allow_unapproved: bool | None = None,
                    now: datetime | None = None) -> list[Prediction]:
    if allow_unapproved is None:
        allow_unapproved = os.environ.get("SHADOW_ALLOW_UNAPPROVED") == "1"
    folder = latest_version(allow_unapproved=allow_unapproved, station_id=station_id)
    if folder is None:
        return []
    import pandas as pd                                    # só carrega as bibliotecas pesadas se há modelo
    from app.ml.data import load_db, to_grid
    from app.ml.features import align_columns, build_features

    now = now or datetime.now(timezone.utc)
    meta, models = _load(folder)
    station = db.get(Station, station_id)
    step = meta["step_min"]
    level, rain = load_db(db, station_id, since=now - timedelta(days=8))
    grid = to_grid(level, step)
    if grid.empty:
        return []
    X = build_features(grid, rain, step)
    X = X[X["level"].notna()]
    if X.empty:
        return []
    made_at = X.index[-1].to_pydatetime()
    if now - made_at > timedelta(minutes=MAX_AGE_MIN + step):
        log.info("Sem dado recente em %s: previsão não gerada", station_id)
        return []
    row = align_columns(X.tail(1), meta["feature_columns"])
    if meta["has_rain"] and row[[c for c in row.columns if c.startswith("rain_")]].isna().all(axis=None):
        log.warning("O modelo usa chuva, mas não há chuva recente para %s: previsão não gerada", station_id)
        return []

    mode = "shadow" if meta.get("approved") else "test"
    base = float(row["level"].iloc[0])
    saved = []
    for h, model in sorted(models.items()):
        delta = float(model.predict(row)[0])
        predicted = min(max(base + delta, station.min_level_m), station.max_level_m)
        exists = db.scalar(select(Prediction.id).where(
            Prediction.station_id == station_id, Prediction.made_at == made_at,
            Prediction.horizon_h == h, Prediction.model_version == meta["version"]))
        if exists:
            continue
        pr = Prediction(station_id=station_id, made_at=made_at, horizon_h=h, base_level_m=base,
                        target_time=made_at + timedelta(hours=h), predicted_level_m=predicted,
                        model_version=meta["version"], mode=mode)
        db.add(pr)
        saved.append(pr)
    db.commit()
    return saved


def shadow_predict(reading_id: int) -> None:
    """Chamado pelo pipeline depois de processar uma leitura. Nunca levanta erro."""
    try:
        from app.db import SessionLocal
        with SessionLocal() as db:
            reading = db.get(Reading, reading_id)
            if reading is not None:
                if latest_version(allow_unapproved=os.environ.get("SHADOW_ALLOW_UNAPPROVED") == "1",
                                  station_id=reading.station_id) is None:
                    return
                predict_station(db, reading.station_id)
    except ModuleNotFoundError as e:
        log.warning("Bibliotecas de ML não instaladas (%s): previsão ignorada", e.name)
    except Exception:                                      # noqa: BLE001
        log.exception("Falha na previsão em modo sombra (leitura %s)", reading_id)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m app.ml.predict")
    p.add_argument("--station", required=True)
    p.add_argument("--allow-unapproved", action="store_true", help="usa modelo não aprovado (mode='test')")
    a = p.parse_args(argv)
    from app.db import SessionLocal, init_db
    init_db()
    with SessionLocal() as db:
        saved = predict_station(db, a.station, allow_unapproved=a.allow_unapproved)
    if not saved:
        print("Nenhuma previsão gerada (sem modelo aprovado, sem dado recente ou já gerada).")
        return 1
    for pr in saved:
        print(f"+{pr.horizon_h} h ({pr.mode}): {pr.base_level_m:.2f} m -> {pr.predicted_level_m:.2f} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
