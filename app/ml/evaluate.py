"""Avaliação do modo sombra: compara cada previsão com o nível que de fato ocorreu no horário-alvo.
O que vale é o ganho sobre a persistência ("o nível daqui a N horas será o de agora").

  python -m app.ml.evaluate [--station rio-piloto-01]
"""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Prediction, Reading


def evaluate_shadow(db: Session, station_id: str | None = None, now: datetime | None = None,
                    tolerance_min: int = 10) -> dict:
    now = now or datetime.now(timezone.utc)
    tol = timedelta(minutes=tolerance_min)
    q = select(Prediction).where(Prediction.target_time <= now - tol)
    if station_id:
        q = q.where(Prediction.station_id == station_id)
    groups: dict = defaultdict(lambda: {"err_model": [], "err_persist": []})
    for pr in db.scalars(q):
        near = db.scalars(select(Reading).where(
            Reading.station_id == pr.station_id,
            Reading.timestamp.between(pr.target_time - tol, pr.target_time + tol))).all()
        if not near:
            continue
        actual = min(near, key=lambda r: abs(r.timestamp.replace(tzinfo=timezone.utc) - pr.target_time.replace(tzinfo=timezone.utc))).water_level_m
        g = groups[(pr.model_version, pr.mode, pr.horizon_h)]
        g["err_model"].append(abs(pr.predicted_level_m - actual))
        g["err_persist"].append(abs(pr.base_level_m - actual))
    out = {}
    for key, g in sorted(groups.items()):
        n = len(g["err_model"])
        mm, mp = sum(g["err_model"]) / n, sum(g["err_persist"]) / n
        out[key] = {"n": n, "mae_model": mm, "mae_persistence": mp, "skill": (1 - mm / mp) if mp > 0 else 0.0}
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.ml.evaluate")
    p.add_argument("--station")
    a = p.parse_args(argv)
    from app.db import SessionLocal, init_db
    init_db()
    with SessionLocal() as db:
        res = evaluate_shadow(db, a.station)
    if not res:
        print("Ainda não há previsões cujo horário-alvo já tenha passado e tenha leitura correspondente.")
        return 0
    print("versão | modo | horizonte | n | MAE persistência | MAE modelo | ganho")
    for (version, mode, h), r in res.items():
        print(f"{version} | {mode} | {h} h | {r['n']} | {r['mae_persistence']:.3f} m | {r['mae_model']:.3f} m | {r['skill']:+.1%}")
    print("\nCom poucas previsões (n pequeno) o ganho não é confiável. Espere semanas e inclua uma cheia.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
