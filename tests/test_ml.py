import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app import live
from app.db import SessionLocal
from app.ml import evaluate, predict, synthetic
from app.ml.data import load_csv, to_grid
from app.ml.features import build_features, build_targets
from app.ml.train import InsufficientData, train
from app.models import Prediction, Reading, WeatherPoint, WeatherRecent

API = {"Authorization": "Bearer api-test"}


def series(n=600, step=10, seed=0, start="2026-01-01"):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq=f"{step}min", tz="UTC")
    return pd.Series(1.0 + np.cumsum(rng.normal(0, 0.02, n)), index=idx)


def hourly_rain(idx, seed=1):
    rng = np.random.default_rng(seed)
    h = pd.date_range(idx[0].floor("1h"), idx[-1].ceil("1h"), freq="1h")
    return pd.Series(rng.exponential(2, len(h)), index=h)


# ---------- grade e features ----------

def test_grid_interpolates_short_gaps_but_not_long_ones():
    idx = pd.date_range("2026-01-01", periods=40, freq="10min", tz="UTC")
    s = pd.Series(np.linspace(1, 2, 40), index=idx).drop(idx[5:7]).drop(idx[20:30])
    g = to_grid(s, 10, max_gap_min=60)
    assert g.iloc[5:7].notna().all()          # 20 min: interpolado
    assert g.iloc[20:30].isna().all()         # 100 min: continua vazio, nada é inventado


def test_features_never_use_the_future():
    level, rain = series(), None
    rain = hourly_rain(level.index)
    t = level.index[400]
    full = build_features(level, rain, 10).loc[t]
    cut_level, cut_rain = level[level.index <= t], rain[rain.index <= t]
    truncated = build_features(cut_level, cut_rain, 10).loc[t]
    pd.testing.assert_series_equal(full, truncated, check_names=False)
    changed = level.copy()
    changed[changed.index > t] += 99.0
    pd.testing.assert_series_equal(build_features(changed, rain, 10).loc[t], full, check_names=False)


def test_rain_uses_only_completed_hours():
    level = series(n=400)
    rain = pd.Series(0.0, index=pd.date_range(level.index[0].floor("1h"), periods=80, freq="1h"))
    t = level.index[200]                                       # instante no meio de uma hora
    rain[t.floor("1h") + pd.Timedelta(hours=1)] = 50.0         # chuva rotulada na PRÓXIMA hora cheia: futuro
    assert build_features(level, rain, 10).loc[t, "rain_1h"] == 0.0
    rain[t.floor("1h")] = 7.0                                  # esta hora já terminou em t.floor
    assert build_features(level, rain, 10).loc[t, "rain_1h"] == 7.0


def test_targets_are_future_minus_current():
    level = series()
    y = build_targets(level, 10, (1, 3))
    i = 100
    assert y["y_1h"].iloc[i] == pytest.approx(level.iloc[i + 6] - level.iloc[i])
    assert y["y_3h"].iloc[i] == pytest.approx(level.iloc[i + 18] - level.iloc[i])
    assert y["y_1h"].iloc[-6:].isna().all()


# ---------- treino ----------

@pytest.fixture(scope="module")
def synth():
    df = synthetic.generate(days=150)
    level = df.dropna(subset=["water_level_m"]).set_index(pd.to_datetime(df.dropna(subset=["water_level_m"])["timestamp"]))["water_level_m"]
    rdf = df.dropna(subset=["rain_mm"])
    rain = rdf.set_index(pd.to_datetime(rdf["timestamp"]))["rain_mm"]
    return df, level, rain


def test_synthetic_data_is_never_approved(tmp_path, synth):
    _, level, rain = synth
    meta = train(level, rain, tmp_path, horizons=(1,), data_source="synthetic")
    assert meta["approved"] is False
    assert any("sintéticos" in r for r in meta["per_horizon"]["1"]["reasons_not_approved"])
    assert (Path(tmp_path) / meta["version"] / "model_1h.joblib").exists()


def test_insufficient_data_is_refused(tmp_path):
    with pytest.raises(InsufficientData):
        train(series(n=300), None, tmp_path, horizons=(1,))


def test_noise_cannot_be_approved(tmp_path):
    """Passeio aleatório não tem o que ser previsto: o portão de aprovação não pode deixar passar."""
    level = series(n=6000, seed=3)
    meta = train(level, None, tmp_path, horizons=(3,), data_source="manual")
    assert meta["approved"] is False


def test_csv_loader_handles_timezone_and_rain(tmp_path):
    f = tmp_path / "d.csv"
    f.write_text("timestamp,water_level_m,rain_mm\n2026-01-01 08:00,1.5,\n2026-01-01 09:00,1.6,2.0\n", encoding="utf-8")
    level, rain = load_csv(str(f), "America/Sao_Paulo")
    assert level.index[0].hour == 11 and rain.iloc[0] == 2.0           # 08:00 em Brasília = 11:00 UTC
    with pytest.raises(ValueError):
        load_csv(str(f), None)
    (tmp_path / "x.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_csv(str(tmp_path / "x.csv"), "UTC")


# ---------- previsão em modo sombra ----------

@pytest.fixture()
def shadow_env(client, synth, monkeypatch):
    """Treina um modelo (marcado como manual, só para exercitar o caminho) e preenche o banco com 4 dias recentes."""
    df, level, rain = synth
    models = Path(__import__("os").environ["MODELS_DIR"])
    meta = train(level, rain, models, horizons=(1, 3), data_source="manual", station_id="rio-piloto-01")
    predict._cache.clear()
    now = datetime.now(timezone.utc)
    shift = now - timedelta(minutes=5) - level.index[-1]
    with SessionLocal() as db:
        recent = level[level.index > level.index[-1] - timedelta(days=4)]
        for i, (t, v) in enumerate(recent.items()):
            db.add(Reading(station_id="rio-piloto-01", timestamp=(t + shift).to_pydatetime(),
                           sequence=i, water_level_m=float(min(v, 7.9))))
        pt = WeatherPoint(name="local", latitude=-28.0, longitude=-49.0, station_id="rio-piloto-01", role="local")
        db.add(pt)
        db.flush()
        for t, v in rain[rain.index > rain.index[-1] - timedelta(days=9)].items():
            db.add(WeatherRecent(point_id=pt.id, valid_time=(t + shift).to_pydatetime(), precipitation_mm=float(v),
                                 source="open-meteo-archive", collected_at=now))
        db.commit()
    yield meta
    shutil.rmtree(models, ignore_errors=True)
    predict._cache.clear()


def test_shadow_prediction_is_saved_once_and_exposed(client, shadow_env):
    cursor = live.init_cursor()
    with SessionLocal() as db:
        saved = predict.predict_station(db, "rio-piloto-01", allow_unapproved=True)
    assert sorted(p.horizon_h for p in saved) == [1, 3]
    assert all(0 <= p.predicted_level_m <= 8 and p.model_version == shadow_env["version"] for p in saved)
    with SessionLocal() as db:
        assert predict.predict_station(db, "rio-piloto-01", allow_unapproved=True) == []     # não duplica
    r = client.get("/api/v1/stations/rio-piloto-01/predictions", headers=API).json()
    assert r["mode"] in ("shadow", "test") and [p["horizon_h"] for p in r["predictions"]] == [1, 3]
    assert [n for n, _ in live.poll_once(cursor)] == ["prediction", "prediction"]


def test_unapproved_model_is_ignored_by_default(client, shadow_env, monkeypatch):
    import json
    meta_file = Path(__import__("os").environ["MODELS_DIR"]) / shadow_env["version"] / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    meta["approved"] = False
    meta_file.write_text(json.dumps(meta), encoding="utf-8")
    predict._cache.clear()
    monkeypatch.delenv("SHADOW_ALLOW_UNAPPROVED", raising=False)
    with SessionLocal() as db:
        assert predict.predict_station(db, "rio-piloto-01") == []


def test_no_prediction_from_stale_data(client, shadow_env):
    with SessionLocal() as db:
        assert predict.predict_station(db, "rio-piloto-01", allow_unapproved=True,
                                       now=datetime.now(timezone.utc) + timedelta(hours=5)) == []


def test_predictions_never_change_risk_or_alerts(client, shadow_env):
    with SessionLocal() as db:
        predict.predict_station(db, "rio-piloto-01", allow_unapproved=True)
    assert client.get("/api/v1/stations/rio-piloto-01/alerts", headers=API).json() == []
    assert client.get("/api/v1/stations/rio-piloto-01/status", headers=API).json()["processed"] is None


def test_endpoint_without_predictions(client):
    r = client.get("/api/v1/stations/rio-piloto-01/predictions", headers=API).json()
    assert r["predictions"] == [] and r["mode"] == "shadow"


def test_evaluation_compares_with_what_happened(client):
    now = datetime.now(timezone.utc)
    base_t = now - timedelta(hours=3)
    with SessionLocal() as db:
        db.add(Reading(station_id="rio-piloto-01", timestamp=base_t + timedelta(hours=1), sequence=1, water_level_m=2.0))
        db.add(Prediction(station_id="rio-piloto-01", made_at=base_t, horizon_h=1, target_time=base_t + timedelta(hours=1),
                          base_level_m=1.0, predicted_level_m=1.9, model_version="v1", mode="shadow"))
        db.add(Prediction(station_id="rio-piloto-01", made_at=now, horizon_h=1, target_time=now + timedelta(hours=1),
                          base_level_m=1.0, predicted_level_m=1.0, model_version="v1", mode="shadow"))   # ainda no futuro
        db.commit()
        res = evaluate.evaluate_shadow(db)
    r = res[("v1", "shadow", 1)]
    assert r["n"] == 1 and r["mae_model"] == pytest.approx(0.1) and r["mae_persistence"] == pytest.approx(1.0)
    assert r["skill"] == pytest.approx(0.9)
