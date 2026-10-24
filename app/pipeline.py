"""Processa cada leitura nova: calcula os derivados, classifica o risco e mantém os alertas."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Rules, rules as default_rules
from app.db import SessionLocal
from app.models import Alert, ProcessedReading, Reading, Station
from app.processing import (ALERT_RISKS, RISK_PT, Point, classify, delta, is_jump, is_stuck,
                            max_level, moving_avg, rate_m_h, trend, m)

log = logging.getLogger("pipeline")


def _open_alert(db: Session, station_id: str, kind: str) -> Alert | None:
    return db.scalar(select(Alert).where(Alert.station_id == station_id, Alert.kind == kind,
                                         Alert.closed_at.is_(None)))


def _sync_alerts(db: Session, station_id: str, proc: ProcessedReading, flags: list[str],
                 reading_id: int, rules: Rules, now: datetime) -> None:
    if (offline := _open_alert(db, station_id, "sensor_offline")):
        offline.closed_at = now                                   # o sensor voltou a enviar

    current = _open_alert(db, station_id, "risk")
    if proc.risk in ALERT_RISKS:
        if current is None or current.severity != proc.risk:
            if current:
                current.closed_at = now                           # escalada ou descida: fecha e abre outro
            db.add(Alert(station_id=station_id, kind="risk", severity=proc.risk,
                         message=proc.reasons, opened_at=now, reading_id=reading_id))
    elif current and proc.risk == "normal":
        current.closed_at = now

    stuck = _open_alert(db, station_id, "sensor_stuck")
    if "stuck" in flags:
        if stuck is None:
            db.add(Alert(station_id=station_id, kind="sensor_stuck", severity="warning", opened_at=now,
                         message=f"Nível idêntico há cerca de {rules.stuck_min} min (sensor possivelmente travado)",
                         reading_id=reading_id))
    elif stuck:
        stuck.closed_at = now


def process_reading(db: Session, reading_id: int, rules: Rules = default_rules,
                    now: datetime | None = None) -> ProcessedReading | None:
    now = now or datetime.now(timezone.utc)
    reading = db.get(Reading, reading_id)
    if reading is None or db.scalar(select(ProcessedReading.id).where(ProcessedReading.reading_id == reading_id)):
        return None
    station = db.get(Station, reading.station_id)
    ts, level = reading.timestamp, reading.water_level_m

    since = ts - timedelta(minutes=max(240, rules.stuck_min + 30))
    rows = db.execute(
        select(Reading.timestamp, Reading.water_level_m, ProcessedReading.flags)
        .outerjoin(ProcessedReading, ProcessedReading.reading_id == Reading.id)
        .where(Reading.station_id == station.id, Reading.timestamp >= since, Reading.timestamp < ts)
        .order_by(Reading.timestamp)
    ).all()
    history = [(Point(r.timestamp, r.water_level_m), "jump" in (r.flags or "").split(",")) for r in rows]
    good = [p for p, suspect in history if not suspect]
    prev, prev_suspect = history[-1] if history else (None, False)
    baseline = good[-1] if good else None

    flags: list[str] = []
    if prev is not None and (ts - prev.ts) > timedelta(minutes=rules.gap_min):
        flags.append("gap")
    jump = is_jump(baseline, prev, prev_suspect, ts, level, rules)
    if jump:
        flags.append("jump")
    elif is_stuck(good, ts, level, rules):
        flags.append("stuck")

    prev_proc = db.scalar(select(ProcessedReading).where(
        ProcessedReading.station_id == station.id, ProcessedReading.timestamp < ts
    ).order_by(ProcessedReading.timestamp.desc()).limit(1))

    metrics: dict = dict(delta_5m_m=None, delta_15m_m=None, delta_60m_m=None, delta_3h_m=None,
                         moving_avg_1h_m=None, max_3h_m=None, rate_m_h=None, trend=None)
    if jump:
        # Leitura suspeita: não vira alarme nem some. O risco fica como estava até a próxima confirmar.
        risk = prev_proc.risk if prev_proc else "unknown"
        reasons = [f"leitura suspeita ({m(level)}): variação impossível, aguardando confirmação - "
                   f"risco mantido em {RISK_PT[risk]}"]
    else:
        rate = rate_m_h(good, ts, level)
        metrics.update(delta_5m_m=delta(good, ts, level, 5), delta_15m_m=delta(good, ts, level, 15),
                       delta_60m_m=delta(good, ts, level, 60), delta_3h_m=delta(good, ts, level, 180),
                       moving_avg_1h_m=moving_avg(good, ts, level), max_3h_m=max_level(good, ts, level),
                       rate_m_h=rate, trend=trend(rate, rules.trend_m_h))
        cotas = (station.attention_level_m, station.alert_level_m, station.flood_level_m)
        risk, reasons = classify(level, rate, cotas, prev_proc.risk if prev_proc else "normal", rules)

    proc = ProcessedReading(reading_id=reading.id, station_id=station.id, timestamp=ts,
                            flags=",".join(flags), risk=risk, reasons=" | ".join(reasons), **metrics)
    db.add(proc)
    try:
        db.flush()
    except IntegrityError:                       # processada por outro worker ao mesmo tempo
        db.rollback()
        return None

    # Leitura atrasada (já existe uma mais nova processada) não mexe nos alertas.
    is_latest = db.scalar(select(ProcessedReading.id).where(
        ProcessedReading.station_id == station.id, ProcessedReading.timestamp > ts).limit(1)) is None
    if is_latest:
        _sync_alerts(db, station.id, proc, flags, reading.id, rules, now)
    db.commit()
    return proc


def process_new_reading(reading_id: int) -> None:
    """Chamado depois da resposta ao receptor. Uma falha aqui NUNCA derruba a ingestão."""
    try:
        with SessionLocal() as db:
            process_reading(db, reading_id)
    except Exception:                            # noqa: BLE001
        log.exception("Falha ao processar a leitura %s", reading_id)
    from app.ml.predict import shadow_predict    # modo sombra: grava a previsão, não mexe em risco nem alertas
    shadow_predict(reading_id)
