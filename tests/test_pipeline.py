from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

import app.pipeline as pipeline
from app import watchdog
from app.db import SessionLocal
from app.models import ProcessedReading

INGEST = {"Authorization": "Bearer ingest-test"}
API = {"Authorization": "Bearer api-test"}
BASE = "/api/v1/stations/rio-piloto-01"


def send(client, at, level, seq):
    r = client.post("/api/v1/readings", headers=INGEST, json={
        "station_id": "rio-piloto-01", "timestamp": at.isoformat().replace("+00:00", "Z"),
        "sequence": seq, "water_level_m": level})
    assert r.status_code == 201, r.text


def status(client):
    return client.get(f"{BASE}/status", headers=API).json()


def alerts(client, **params):
    rows = client.get(f"{BASE}/alerts", headers=API, params=params).json()
    return sorted(rows, key=lambda a: a["id"])


def t0(hours_ago=20):
    return datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=hours_ago)


def test_risk_escalates_holds_and_releases(client):
    start = t0()
    levels = [1.0, 2.6, 3.1, 3.6, 3.48, 3.40, 2.0]
    expected = ["normal", "attention", "alert", "critical", "critical", "alert", "normal"]
    got = []
    for i, level in enumerate(levels):
        send(client, start + timedelta(hours=i), level, i + 1)
        got.append(status(client)["processed"]["risk"])
    assert got == expected
    history = alerts(client)
    assert [a["severity"] for a in history if a["kind"] == "risk"] == ["attention", "alert", "critical", "alert"]
    assert all(a["closed_at"] for a in history)                       # tudo fechado ao normalizar
    assert alerts(client, open_only=True) == []


def test_open_alert_has_reasons_and_one_per_kind(client):
    start = t0()
    send(client, start, 1.0, 1)
    send(client, start + timedelta(hours=1), 2.6, 2)
    send(client, start + timedelta(hours=2), 2.7, 3)         # continua em atenção: não abre outro alerta
    open_alerts = alerts(client, open_only=True)
    assert len(open_alerts) == 1 and open_alerts[0]["severity"] == "attention"
    assert "cota de atenção" in open_alerts[0]["message"]


def test_derived_metrics_are_stored(client):
    start = t0(2)
    for i in range(7):                                        # a cada 10 min, subindo 0,6 m/h
        send(client, start + timedelta(minutes=10 * i), round(1.0 + 0.1 * i, 2), i + 1)
    p = status(client)["processed"]
    assert abs(p["delta_60m_m"] - 0.6) < 1e-6
    assert abs(p["rate_m_h"] - 0.6) < 0.01 and p["trend"] == "rising"
    assert p["risk"] == "attention" and any("subindo" in r for r in p["reasons"])
    assert p["max_3h_m"] == 1.6


def test_impossible_jump_is_held_until_confirmed(client):
    start = t0(5)
    for i in range(6):
        send(client, start + timedelta(minutes=10 * i), 1.0, i + 1)
    send(client, start + timedelta(minutes=60), 6.0, 7)       # salto de 5 m em 10 min
    s = status(client)
    assert s["processed"]["flags"] == ["jump"] and s["processed"]["risk"] == "normal"
    assert s["processed"]["trend"] is None and alerts(client, open_only=True) == []
    send(client, start + timedelta(minutes=70), 6.05, 8)      # a leitura seguinte confirma: cheia real
    s = status(client)
    assert "jump" not in s["processed"]["flags"] and s["processed"]["risk"] == "critical"
    assert [a["severity"] for a in alerts(client, open_only=True)] == ["critical"]


def test_isolated_spike_never_raises_alarm(client):
    start = t0(5)
    for i in range(6):
        send(client, start + timedelta(minutes=10 * i), 1.0, i + 1)
    send(client, start + timedelta(minutes=60), 7.5, 7)
    send(client, start + timedelta(minutes=70), 1.0, 8)       # volta ao normal: era defeito
    assert status(client)["processed"]["risk"] == "normal"
    assert alerts(client) == []


def test_gap_is_flagged(client):
    start = t0(5)
    send(client, start, 1.0, 1)
    send(client, start + timedelta(minutes=40), 1.0, 2)
    assert status(client)["processed"]["flags"] == ["gap"]


def test_late_reading_does_not_change_alerts(client):
    start = t0()
    send(client, start, 1.0, 1)
    send(client, start + timedelta(hours=2), 3.1, 2)          # alerta aberto
    send(client, start + timedelta(hours=1), 1.0, 3)          # chega atrasada
    assert [a["severity"] for a in alerts(client, open_only=True)] == ["alert"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ProcessedReading)) == 3


def test_watchdog_opens_offline_alert_once_and_closes_on_new_reading(client):
    send(client, t0(1), 1.0, 1)
    later = datetime.now(timezone.utc) + timedelta(minutes=30)
    with SessionLocal() as db:
        assert watchdog.check_offline(db, now=later) == 1
        assert watchdog.check_offline(db, now=later) == 0     # não duplica
    assert [a["kind"] for a in alerts(client, open_only=True)] == ["sensor_offline"]
    send(client, t0(0) - timedelta(minutes=1), 1.0, 2)         # sensor voltou
    assert alerts(client, open_only=True) == []
    assert status(client)["stale"] is False


def test_status_without_readings(client):
    s = status(client)
    assert s["reading"] is None and s["processed"] is None and s["stale"] is True and s["data_age_s"] is None


def test_processing_failure_never_breaks_ingestion(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("falha simulada")
    monkeypatch.setattr(pipeline, "process_reading", boom)
    send(client, t0(1), 1.0, 1)                                # continua 201
    r = client.get(f"{BASE}/latest", headers=API)
    assert r.status_code == 200 and r.json()["water_level_m"] == 1.0


def test_new_routes_require_api_token(client):
    assert client.get(f"{BASE}/status").status_code == 401
    assert client.get(f"{BASE}/alerts", headers=INGEST).status_code == 401
    assert client.get("/api/v1/stations/nao-existe/status", headers=API).status_code == 404
