from datetime import datetime, timedelta, timezone

from app import live, watchdog
from app.db import SessionLocal

INGEST = {"Authorization": "Bearer ingest-test"}
API = {"Authorization": "Bearer api-test"}


def send(client, at, level, seq):
    r = client.post("/api/v1/readings", headers=INGEST, json={
        "station_id": "rio-piloto-01", "timestamp": at.isoformat().replace("+00:00", "Z"),
        "sequence": seq, "water_level_m": level})
    assert r.status_code == 201, r.text


def names(events):
    return [n for n, _ in events]


def test_live_requires_api_token(client):
    assert client.get("/api/v1/live").status_code == 401
    assert client.get("/api/v1/live", headers=INGEST).status_code == 401


def test_poll_emits_only_new_events(client):
    now = datetime.now(timezone.utc)
    send(client, now - timedelta(hours=3), 1.0, 1)
    cursor = live.init_cursor()
    assert live.poll_once(cursor) == []                         # o que já existia não é reenviado

    send(client, now - timedelta(hours=2), 2.6, 2)              # atenção
    events = live.poll_once(cursor)
    assert names(events) == ["reading", "alert_opened"]
    assert events[0][1]["risk"] == "attention" and events[1][1]["severity"] == "attention"
    assert live.poll_once(cursor) == []                         # sem duplicar

    send(client, now - timedelta(hours=1), 1.0, 3)              # normaliza
    events = live.poll_once(cursor)
    assert names(events) == ["reading", "alert_closed"]
    assert events[1][1]["closed_at"] is not None


def test_alerts_from_another_process_are_streamed(client):
    """O watchdog roda em outro processo: o stream precisa enxergá-lo pelo banco."""
    send(client, datetime.now(timezone.utc) - timedelta(minutes=1), 1.0, 1)
    cursor = live.init_cursor()
    with SessionLocal() as db:
        watchdog.check_offline(db, now=datetime.now(timezone.utc) + timedelta(minutes=30))
    events = live.poll_once(cursor)
    assert names(events) == ["alert_opened"] and events[0][1]["kind"] == "sensor_offline"


def test_sse_format():
    out = live.format_sse("reading", {"risk": "alerta", "x": 1})
    assert out == 'event: reading\ndata: {"risk": "alerta", "x": 1}\n\n'
