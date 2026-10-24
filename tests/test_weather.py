from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

import app.weather as weather
from app.models import WeatherForecast, WeatherPoint, WeatherRecent

NOW = datetime(2026, 10, 2, 14, 30, tzinfo=timezone.utc)


def payload(start, hours, base=1.0, none_at=()):
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(hours)]
    return {"hourly": {"time": times, "precipitation": [None if i in none_at else base + i for i in range(hours)]}}


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


@pytest.fixture()
def point(db):
    return weather.add_point(db, "rio-piloto-01", "Ponto local", -28.6775, -49.3708)


def test_splits_past_and_future(db, point, monkeypatch):
    start = datetime(2026, 10, 2, 11, 0, tzinfo=timezone.utc)          # 11h,12h,13h passadas; 14h em diante futuras
    monkeypatch.setattr(weather, "fetch_json", lambda url, params, **kw: payload(start, 8))
    res = weather.collect_point(db, point, now=NOW)
    assert res == {"status": "ok", "forecast": 5, "recent": 3}
    assert count(db, WeatherForecast) == 5 and count(db, WeatherRecent) == 3


def test_requests_expected_parameters(db, point, monkeypatch):
    seen = {}
    monkeypatch.setattr(weather, "fetch_json", lambda url, params, **kw: seen.update(url=url, **params) or payload(NOW, 2))
    weather.collect_point(db, point, now=NOW)
    assert seen["url"] == weather.FORECAST_URL
    assert seen["hourly"] == "precipitation" and seen["timezone"] == "UTC"
    assert seen["latitude"] == -28.6775 and seen["longitude"] == -49.3708


def test_null_and_implausible_values_are_skipped(db, point, monkeypatch):
    bad = payload(NOW.replace(minute=0), 4, none_at=(1,))
    bad["hourly"]["precipitation"][2] = 9999.0
    monkeypatch.setattr(weather, "fetch_json", lambda url, params, **kw: bad)
    weather.collect_point(db, point, now=NOW)
    assert count(db, WeatherForecast) == 2                               # 4 horas - 1 nula - 1 absurda


def test_malformed_payload_is_rejected():
    with pytest.raises(ValueError):
        weather.parse_hourly({"hourly": {"time": ["2026-10-02T10:00"], "precipitation": []}})
    with pytest.raises(ValueError):
        weather.parse_hourly({})


def test_forecasts_are_append_only_and_recent_is_upserted(db, point, monkeypatch):
    start = datetime(2026, 10, 2, 11, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(weather, "fetch_json", lambda url, params, **kw: payload(start, 8))
    weather.collect_point(db, point, now=NOW)
    weather.collect_point(db, point, now=NOW + timedelta(hours=1), force=True)
    assert count(db, WeatherForecast) == 9           # 5 da primeira coleta + 4 da segunda: ambas ficam guardadas
    assert count(db, WeatherRecent) == 4             # 3 recentes + a hora que virou passado, sem duplicar


def test_min_interval_skips_recent_collection(db, point, monkeypatch):
    calls = []
    monkeypatch.setattr(weather, "fetch_json", lambda url, params, **kw: calls.append(1) or payload(NOW.replace(minute=0), 3))
    weather.collect_point(db, point, now=NOW)
    assert weather.collect_point(db, point, now=NOW + timedelta(minutes=10))["status"] == "skipped"
    assert len(calls) == 1
    assert weather.collect_point(db, point, now=NOW + timedelta(minutes=10), force=True)["status"] == "ok"


def test_one_failing_point_does_not_stop_the_others(db, point, monkeypatch):
    weather.add_point(db, "rio-piloto-01", "A montante", -28.5, -49.4, role="upstream")

    def fake(url, params, **kw):
        if params["latitude"] == -28.5:
            raise RuntimeError("API fora do ar")
        return payload(datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0), 3)

    monkeypatch.setattr(weather, "fetch_json", fake)
    assert weather.collect_all() == (1, 1)
    assert count(db, WeatherForecast) == 3           # só o ponto que funcionou gravou


def test_backfill_chunks_by_year_and_is_idempotent(db, point, monkeypatch):
    calls = []

    def fake(url, params, **kw):
        calls.append((url, params["start_date"], params["end_date"]))
        return payload(datetime.fromisoformat(params["start_date"]).replace(tzinfo=timezone.utc), 5)

    monkeypatch.setattr(weather, "fetch_json", fake)
    weather.backfill_point(db, point, date(2023, 12, 30), date(2024, 1, 2))
    assert [c[1:] for c in calls] == [("2023-12-30", "2023-12-31"), ("2024-01-01", "2024-01-02")]
    assert all(c[0] == weather.ARCHIVE_URL for c in calls)
    first = count(db, WeatherRecent)
    weather.backfill_point(db, point, date(2023, 12, 30), date(2024, 1, 2))
    assert count(db, WeatherRecent) == first


def test_add_point_validation(db):
    with pytest.raises(ValueError):
        weather.add_point(db, "nao-existe", "x", -28.0, -49.0)
    with pytest.raises(ValueError):
        weather.add_point(db, "rio-piloto-01", "x", 95.0, -49.0)
    with pytest.raises(ValueError):
        weather.add_point(db, "rio-piloto-01", "x", -28.0, -49.0, role="outro")
    assert db.scalar(select(func.count()).select_from(WeatherPoint)) == 0
