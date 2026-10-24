from datetime import datetime, timedelta, timezone

INGEST = {"Authorization": "Bearer ingest-test"}
API = {"Authorization": "Bearer api-test"}


def iso(minutes_ago: float = 5) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def body(**over):
    base = {"station_id": "rio-piloto-01", "timestamp": iso(), "sequence": 1, "water_level_m": 1.37}
    base.update(over)
    return base


def post(client, **over):
    return client.post("/api/v1/readings", json=body(**over), headers=INGEST)


def count(client):
    return len(client.get("/api/v1/stations/rio-piloto-01/history?limit=10000", headers=API).json())


def test_valid_reading_is_accepted(client):
    r = post(client)
    assert r.status_code == 201
    assert r.json()["status"] == "accepted" and r.json()["reading_id"] > 0


def test_missing_or_wrong_token(client):
    assert client.post("/api/v1/readings", json=body()).status_code == 401
    assert client.post("/api/v1/readings", json=body(), headers={"Authorization": "Bearer x"}).status_code == 401
    assert count(client) == 0


def test_invalid_payloads_are_rejected_and_not_saved(client):
    bad = body()
    del bad["water_level_m"]
    assert client.post("/api/v1/readings", json=bad, headers=INGEST).status_code == 422
    assert post(client, water_level_m="alto").status_code == 422
    assert post(client, timestamp="2026-10-02T14:30:00").status_code == 422      # sem fuso
    assert post(client, water_level_m=99.0).status_code == 422                   # fora da faixa
    assert post(client, water_level_m=-1.0).status_code == 422
    assert post(client, station_id="nao-existe").status_code == 422
    assert post(client, timestamp=iso(-60)).status_code == 422                   # futuro
    assert count(client) == 0


def test_duplicate_sequence_is_not_saved_twice(client):
    assert post(client, sequence=5).status_code == 201
    r = post(client, sequence=5, timestamp=iso(4))       # reenvio com horário um pouco diferente
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    assert count(client) == 1


def test_same_timestamp_is_duplicate(client):
    ts = iso(10)
    assert post(client, sequence=1, timestamp=ts).status_code == 201
    assert post(client, sequence=2, timestamp=ts).status_code == 200
    assert count(client) == 1


def test_sequence_reuse_after_window_is_accepted(client):
    """Se o ESP reiniciar e o contador voltar, leituras novas não podem ser descartadas."""
    assert post(client, sequence=5, timestamp=iso(180)).status_code == 201
    assert post(client, sequence=5, timestamp=iso(5)).status_code == 201
    assert count(client) == 2


def test_latest_and_history(client):
    post(client, sequence=1, timestamp=iso(30), water_level_m=1.0)
    post(client, sequence=2, timestamp=iso(20), water_level_m=1.2)
    post(client, sequence=3, timestamp=iso(10), water_level_m=1.5)
    latest = client.get("/api/v1/stations/rio-piloto-01/latest", headers=API).json()
    assert latest["water_level_m"] == 1.5 and latest["timestamp"].endswith("Z") or "+00:00" in latest["timestamp"]
    hist = client.get("/api/v1/stations/rio-piloto-01/history", headers=API).json()
    assert [h["water_level_m"] for h in hist] == [1.0, 1.2, 1.5]       # cronológica


def test_read_endpoints_need_api_token(client):
    assert client.get("/api/v1/stations").status_code == 401
    assert client.get("/api/v1/stations", headers=INGEST).status_code == 401   # token do receptor não lê
    assert client.get("/api/v1/stations", headers=API).status_code == 200
    assert client.get("/api/v1/stations/nao-existe/latest", headers=API).status_code == 404
