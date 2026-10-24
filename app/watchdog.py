"""Vigia de sensor offline. 'Sem dados' nunca pode parecer 'tudo normal'.

  python -m app.watchdog                 # uma verificação (agendar a cada minuto)
  python -m app.watchdog --loop 60       # verifica continuamente, a cada 60 s

O alerta é fechado automaticamente quando uma nova leitura chega (app/pipeline.py).
"""
import argparse
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import rules
from app.db import SessionLocal, init_db
from app.models import Alert, Reading, Station

log = logging.getLogger("watchdog")


def check_offline(db: Session, now: datetime | None = None) -> int:
    """Abre 'sensor_offline' para estações sem leitura recebida há mais de OFFLINE_MIN. Retorna quantos abriu."""
    now = now or datetime.now(timezone.utc)
    opened = 0
    for station in db.scalars(select(Station).where(Station.active)).all():
        last = db.scalar(select(func.max(Reading.received_at)).where(Reading.station_id == station.id))
        if last is None:
            continue                                   # nunca enviou: nada a comparar
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age_min = (now - last).total_seconds() / 60
        already = db.scalar(select(Alert.id).where(Alert.station_id == station.id,
                                                   Alert.kind == "sensor_offline", Alert.closed_at.is_(None)))
        if age_min > rules.offline_min and already is None:
            db.add(Alert(station_id=station.id, kind="sensor_offline", severity="warning", opened_at=now,
                         message=f"Sem leituras há {int(age_min)} min"))
            opened += 1
            log.warning("Estação %s sem leituras há %d min", station.id, int(age_min))
    db.commit()
    return opened


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m app.watchdog")
    p.add_argument("--loop", type=int, metavar="SEGUNDOS", help="repetir a cada N segundos")
    args = p.parse_args(argv)
    init_db()
    while True:
        with SessionLocal() as db:
            check_offline(db)
        if not args.loop:
            return 0
        time.sleep(args.loop)


if __name__ == "__main__":
    raise SystemExit(main())
