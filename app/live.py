"""Tempo real por SSE (Server-Sent Events): o servidor empurra eventos ao dashboard por uma conexão aberta.

A fonte dos eventos é o próprio banco: a cada poucos segundos cada conexão pergunta "o que há de novo?".
Assim funcionam também os alertas abertos pelo watchdog (outro processo) e vários workers do servidor.
Com poucos dashboards conectados o custo é desprezível.
"""
import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Alert, Prediction, ProcessedReading

POLL_S = float(os.environ.get("SSE_POLL_S", "2"))
HEARTBEAT_S = 15


@dataclass
class Cursor:
    last_processed_id: int = 0
    last_alert_id: int = 0
    last_prediction_id: int = 0
    open_alert_ids: set[int] = field(default_factory=set)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _alert_payload(a: Alert) -> dict:
    return {"id": a.id, "station_id": a.station_id, "kind": a.kind, "severity": a.severity,
            "message": a.message, "opened_at": _iso(a.opened_at), "closed_at": _iso(a.closed_at)}


def init_cursor() -> Cursor:
    """Ponto de partida: só eventos novos a partir da conexão. Quem conecta busca o estado atual em /status."""
    with SessionLocal() as db:
        return Cursor(
            last_processed_id=db.scalar(select(func.coalesce(func.max(ProcessedReading.id), 0))),
            last_alert_id=db.scalar(select(func.coalesce(func.max(Alert.id), 0))),
            last_prediction_id=db.scalar(select(func.coalesce(func.max(Prediction.id), 0))),
            open_alert_ids=set(db.scalars(select(Alert.id).where(Alert.closed_at.is_(None)))),
        )


def poll_once(cursor: Cursor) -> list[tuple[str, dict]]:
    """Eventos novos desde a última consulta. Atualiza o cursor. Eventos: reading, alert_opened,
    alert_closed, prediction."""
    events: list[tuple[str, dict]] = []
    with SessionLocal() as db:
        for p in db.scalars(select(ProcessedReading).where(ProcessedReading.id > cursor.last_processed_id)
                            .order_by(ProcessedReading.id)):
            cursor.last_processed_id = p.id
            events.append(("reading", {
                "station_id": p.station_id, "reading_id": p.reading_id, "timestamp": _iso(p.timestamp),
                "risk": p.risk, "trend": p.trend, "rate_m_h": p.rate_m_h,
                "flags": [f for f in p.flags.split(",") if f]}))

        for a in db.scalars(select(Alert).where(Alert.id > cursor.last_alert_id).order_by(Alert.id)):
            cursor.last_alert_id = a.id
            events.append(("alert_opened", _alert_payload(a)))
            if a.closed_at is None:
                cursor.open_alert_ids.add(a.id)

        if cursor.open_alert_ids:
            still_open = set(db.scalars(select(Alert.id).where(Alert.id.in_(cursor.open_alert_ids),
                                                               Alert.closed_at.is_(None))))
            for a in db.scalars(select(Alert).where(Alert.id.in_(cursor.open_alert_ids - still_open))
                                .order_by(Alert.id)):
                events.append(("alert_closed", _alert_payload(a)))
            cursor.open_alert_ids = still_open

        for pr in db.scalars(select(Prediction).where(Prediction.id > cursor.last_prediction_id)
                             .order_by(Prediction.id)):
            cursor.last_prediction_id = pr.id
            events.append(("prediction", {
                "station_id": pr.station_id, "made_at": _iso(pr.made_at), "horizon_h": pr.horizon_h,
                "target_time": _iso(pr.target_time), "predicted_level_m": pr.predicted_level_m,
                "model_version": pr.model_version, "mode": pr.mode}))
    return events


def format_sse(event: str, data: dict, event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def event_stream(request: Request):
    cursor = await run_in_threadpool(init_cursor)
    yield "retry: 5000\n\n"                                   # o navegador reconecta após 5 s
    yield format_sse("hello", {"server_time": _iso(datetime.now(timezone.utc)), "poll_s": POLL_S})
    last_beat = time.monotonic()
    while not await request.is_disconnected():
        events = await run_in_threadpool(poll_once, cursor)
        for name, data in events:
            yield format_sse(name, data)
        if events:
            last_beat = time.monotonic()
        elif time.monotonic() - last_beat >= HEARTBEAT_S:
            yield ": keepalive\n\n"                           # mantém proxies e firewalls com a conexão viva
            last_beat = time.monotonic()
        await asyncio.sleep(POLL_S)
