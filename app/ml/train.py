"""Treino com validação TEMPORAL: treina no passado, valida e testa em períodos posteriores.

  python -m app.ml.train --csv dados.csv --tz America/Sao_Paulo      # dados manuais
  python -m app.ml.train --db rio-piloto-01                          # dados do próprio sensor (no banco)

Compara o modelo com baselines. Um modelo só é marcado como 'approved' se vencer a persistência
("daqui a N horas o nível será o de agora") também nos eventos de subida. Mesmo aprovado, ele começa em modo
sombra: grava previsões mas não gera alertas.
"""
import argparse
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from app.ml.data import csv_is_synthetic, load_csv, to_grid
from app.ml.features import HORIZONS_H, build_features, build_targets

try:                                          # LightGBM é preferido; sem ele usa o equivalente do scikit-learn
    import lightgbm as lgb
except Exception:                             # noqa: BLE001
    lgb = None

warnings.filterwarnings("ignore", message=".*eval_set.*")

TRAIN_FRAC, VAL_FRAC = 0.70, 0.85
MIN_SKILL = 0.05                              # precisa reduzir o erro da persistência em pelo menos 5%
MIN_EVENTS = 10


class InsufficientData(Exception):
    pass


def mae(y, p) -> float:
    return float(np.mean(np.abs(np.asarray(y) - np.asarray(p))))


def rmse(y, p) -> float:
    return float(np.sqrt(np.mean((np.asarray(y) - np.asarray(p)) ** 2)))


def make_gbm():
    if lgb is not None:
        return "lightgbm", lgb.LGBMRegressor(n_estimators=600, learning_rate=0.03, num_leaves=15,
                                             min_child_samples=50, subsample=0.8, subsample_freq=1,
                                             colsample_bytree=0.8, random_state=42, verbose=-1)
    return "hist_gradient_boosting", HistGradientBoostingRegressor(
        max_iter=300, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=50, random_state=42)


def fit_gbm(model, kind, Xtr, ytr, Xva, yva):
    if kind == "lightgbm":
        model.fit(Xtr, ytr, eval_set=[(Xva, yva)], callbacks=[lgb.early_stopping(50, verbose=False)])
    else:
        model.fit(Xtr, ytr)
    return model


def metrics(y, pred, thr):
    ev = y >= thr
    out = {"mae": mae(y, pred), "rmse": rmse(y, pred), "n": int(len(y)), "n_events": int(ev.sum())}
    if ev.any():
        out["mae_events"] = mae(y[ev], pred[ev])
        out["event_recall"] = float(np.mean(pred[ev] >= 0.5 * thr))
    return out


def train(level: pd.Series, rain: pd.Series | None, out_dir: str | Path, step_min: int = 10,
          horizons=HORIZONS_H, data_source: str = "manual", station_id: str | None = None,
          min_train_rows: int = 1000, min_test_rows: int = 300) -> dict:
    grid = to_grid(level, step_min)
    X = build_features(grid, rain, step_min)
    Y = build_targets(grid, step_min, horizons)
    valid = X["level"].notna()
    X, Y = X[valid], Y[valid]
    if len(X) == 0:
        raise InsufficientData("nenhuma leitura utilizável")
    t1, t2 = X.index[int(len(X) * TRAIN_FRAC)], X.index[int(len(X) * VAL_FRAC)]
    columns = list(X.columns)

    version = "v" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    folder = Path(out_dir) / version
    meta = {"version": version, "created_at": datetime.now(timezone.utc).isoformat(), "step_min": step_min,
            "horizons_h": list(horizons), "feature_columns": columns, "data_source": data_source,
            "data_range": [str(X.index[0]), str(X.index[-1])], "rows": int(len(X)),
            "has_rain": any(c.startswith("rain_") for c in columns), "station_id": station_id,
            "per_horizon": {}}
    folder.mkdir(parents=True, exist_ok=True)

    for h in horizons:
        gap = pd.Timedelta(hours=h)                    # evita que o alvo de uma linha caia no período seguinte
        y = Y[f"y_{h}h"]
        ok = y.notna()
        tr = ok & (X.index <= t1 - gap)
        va = ok & (X.index >= t1) & (X.index <= t2 - gap)
        te = ok & (X.index >= t2)
        if tr.sum() < min_train_rows or te.sum() < min_test_rows or va.sum() < 50:
            raise InsufficientData(
                f"horizonte {h}h: treino={int(tr.sum())} (mín. {min_train_rows}), validação={int(va.sum())}, "
                f"teste={int(te.sum())} (mín. {min_test_rows}). Faltam dados contínuos.")
        Xtr, Xva, Xte = X[tr], X[va], X[te]
        ytr, yva, yte = y[tr].values, y[va].values, y[te].values
        pos = ytr[ytr > 0]
        thr = float(max(0.05, np.quantile(pos, 0.9))) if len(pos) >= 20 else 0.05

        candidates = {}
        ridge = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=1.0)).fit(Xtr, ytr)
        candidates["ridge"] = ridge
        kind, gbm = make_gbm()
        candidates[kind] = fit_gbm(gbm, kind, Xtr, ytr, Xva, yva)

        val_mae = {name: mae(yva, m.predict(Xva)) for name, m in candidates.items()}
        best = min(val_mae, key=val_mae.get)
        pred_best = candidates[best].predict(Xte)

        results = {
            "persistence": metrics(yte, np.zeros_like(yte), thr),
            "linear_extrapolation": metrics(yte, np.nan_to_num(Xte["rate_1h"].values) * h, thr),
        }
        for name, m in candidates.items():
            results[name] = metrics(yte, m.predict(Xte), thr)
        base, mod = results["persistence"], results[best]

        skill = 1 - mod["mae"] / base["mae"] if base["mae"] > 0 else 0.0
        reasons = []
        if data_source == "synthetic":
            reasons.append("dados sintéticos: nunca aprovado")
        if skill < MIN_SKILL:
            reasons.append(f"ganho sobre a persistência {skill:.1%} < {MIN_SKILL:.0%}")
        if mod["n_events"] < MIN_EVENTS:
            reasons.append(f"poucos eventos de subida no teste ({mod['n_events']} < {MIN_EVENTS})")
        elif mod.get("mae_events", 9e9) > base.get("mae_events", 0):
            reasons.append("pior que a persistência nos eventos de subida")

        joblib.dump(candidates[best], folder / f"model_{h}h.joblib")
        meta["per_horizon"][str(h)] = {
            "model": best, "val_mae": val_mae, "event_threshold_m": thr, "skill_vs_persistence": float(skill),
            "test": results, "approved": not reasons, "reasons_not_approved": reasons,
            "split": {"train_end": str(t1), "test_start": str(t2), "n_train": int(tr.sum()),
                      "n_val": int(va.sum()), "n_test": int(te.sum())}}

    meta["approved"] = all(v["approved"] for v in meta["per_horizon"].values())
    (folder / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta


def report(meta: dict) -> str:
    lines = [f"Versão {meta['version']} | dados: {meta['data_source']} | {meta['rows']} instantes | "
             f"{meta['data_range'][0][:10]} a {meta['data_range'][1][:10]} | chuva: {'sim' if meta['has_rain'] else 'não'}",
             "", "horizonte | modelo | MAE persistência | MAE modelo | ganho | eventos (MAE pers. -> modelo) | aprovado"]
    for h, d in meta["per_horizon"].items():
        b, m = d["test"]["persistence"], d["test"][d["model"]]
        ev = (f"{b.get('mae_events', float('nan')):.3f} -> {m.get('mae_events', float('nan')):.3f} (n={m['n_events']})")
        lines.append(f"{h:>2} h      | {d['model']:<22} | {b['mae']:.3f} m | {m['mae']:.3f} m | "
                     f"{d['skill_vs_persistence']:+.1%} | {ev} | {'SIM' if d['approved'] else 'não'}")
        for r in d["reasons_not_approved"]:
            lines.append(f"            - {r}")
    lines.append("")
    lines.append("APROVADO para uso em modo sombra." if meta["approved"] else
                 "NÃO aprovado: serve para testar o código, não para confiar na previsão.")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.ml.train")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="dados manuais: timestamp, water_level_m[, rain_mm]")
    src.add_argument("--db", metavar="STATION_ID", help="usa as leituras do banco")
    p.add_argument("--tz", default="America/Sao_Paulo", help="fuso dos horários sem fuso no CSV")
    p.add_argument("--step-min", type=int, default=10, help="grade de tempo (use 60 se os dados são horários)")
    p.add_argument("--out", default="models_store")
    a = p.parse_args(argv)

    if a.csv:
        level, rain = load_csv(a.csv, a.tz)
        source = "synthetic" if csv_is_synthetic(a.csv) else "manual"
    else:
        from app.db import SessionLocal, init_db
        from app.ml.data import load_db
        init_db()
        with SessionLocal() as db:
            level, rain = load_db(db, a.db)
        source = "sensor"
    try:
        meta = train(level, rain, a.out, step_min=a.step_min, data_source=source,
                     station_id=a.db if a.db else None)
    except InsufficientData as e:
        print(f"Dados insuficientes: {e}")
        return 1
    print(report(meta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
