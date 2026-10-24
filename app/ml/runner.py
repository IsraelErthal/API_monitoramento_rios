"""Ciclo operacional do modelo: retreina modelos por estação quando chegam dados novos.

Use como tarefa agendada diária, ou mantenha ``--loop`` em execução. O processo nunca
interrompe a API: se ainda não houver série suficiente ou o candidato não for aprovado,
o último modelo aprovado continua sendo usado em modo sombra.
"""
import argparse
import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db import SessionLocal, init_db
from app.ml.data import load_db
from app.ml.predict import latest_version, models_dir
from app.ml.train import InsufficientData, report, train
from app.models import Reading, Station

log = logging.getLogger("ml.runner")


def _as_utc(value: str) -> datetime:
    value = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def new_readings_since(db, station_id: str, model_dir, min_new_readings: int) -> bool:
    """Só retreina quando a estação acumulou dados após o último treino."""
    folder = latest_version(model_dir, allow_unapproved=True, station_id=station_id)
    if folder is None:
        return True
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    trained_through = _as_utc(meta["data_range"][1])
    count = db.scalar(select(func.count()).select_from(Reading).where(
        Reading.station_id == station_id, Reading.timestamp > trained_through))
    return count >= min_new_readings


def train_station(station_id: str, step_min: int = 10, min_new_readings: int = 144) -> str:
    """Treina uma estação e retorna ``trained``, ``skipped`` ou ``insufficient``."""
    with SessionLocal() as db:
        if db.get(Station, station_id) is None:
            raise ValueError(f"Estação inexistente: {station_id}")
        out = models_dir()
        if not new_readings_since(db, station_id, out, min_new_readings):
            return "skipped"
        level, rain = load_db(db, station_id)
    try:
        meta = train(level, rain, out, step_min=step_min, data_source="sensor", station_id=station_id)
    except InsufficientData as exc:
        log.info("Estação %s ainda sem dados suficientes: %s", station_id, exc)
        return "insufficient"
    log.info("Modelo treinado para %s\n%s", station_id, report(meta))
    return "trained"


def run_once(station_id: str | None, step_min: int, min_new_readings: int) -> dict[str, str]:
    with SessionLocal() as db:
        stations = [station_id] if station_id else list(db.scalars(select(Station.id).where(Station.active)))
    results = {}
    for current in stations:
        try:
            results[current] = train_station(current, step_min, min_new_readings)
        except Exception:  # noqa: BLE001 - uma estação não deve bloquear as demais
            log.exception("Falha no treino da estação %s", current)
            results[current] = "failed"
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ml.runner")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--station", help="retreina somente esta estação")
    target.add_argument("--all", action="store_true", help="retreina cada estação ativa")
    parser.add_argument("--step-min", type=int, default=10, help="resolução das leituras do ESP")
    parser.add_argument("--min-new-readings", type=int, default=144,
                        help="mínimo de novas leituras antes de outro treino (144 = 1 dia a cada 10 min)")
    parser.add_argument("--loop", type=int, metavar="SECONDS", help="repete a verificação a cada N segundos")
    args = parser.parse_args(argv)
    if args.step_min <= 0 or 60 % args.step_min:
        parser.error("--step-min deve dividir uma hora (por exemplo: 5, 10, 15, 30 ou 60)")
    if args.min_new_readings < 1:
        parser.error("--min-new-readings deve ser positivo")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    while True:
        results = run_once(args.station, args.step_min, args.min_new_readings)
        print(" ".join(f"{key}:{value}" for key, value in results.items()))
        if not args.loop:
            return 1 if "failed" in results.values() else 0
        time.sleep(args.loop)


if __name__ == "__main__":
    raise SystemExit(main())
