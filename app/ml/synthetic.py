"""Gera uma série SINTÉTICA de rio e chuva, só para verificar se o código de treino/previsão roda.

ATENÇÃO: os números não dizem nada sobre o seu rio. Nenhum modelo treinado com isto é aprovado.

  python -m app.ml.synthetic --days 180 --out dados_sinteticos.csv
"""
import argparse

import numpy as np
import pandas as pd


def generate(days: int = 180, step_min: int = 10, seed: int = 42, start: str = "2025-01-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    hours = days * 24
    hourly_idx = pd.date_range(start, periods=hours, freq="1h", tz="UTC")
    rain = np.zeros(hours)
    h = 0
    while h < hours:
        if rng.random() < 0.012:                               # início de uma chuva
            dur = int(rng.integers(3, 12))
            rain[h:h + dur] += rng.exponential(5.0, size=len(rain[h:h + dur]))
        h += 1
    steps = days * 24 * (60 // step_min)
    dt_h = step_min / 60
    x1 = x2 = 0.0
    levels = np.empty(steps)
    per_h = 60 // step_min
    for i in range(steps):
        inflow = rain[min(i // per_h, hours - 1)]              # a água cai durante a hora
        x1 += dt_h * (inflow - 0.5 * x1)
        x2 += dt_h * (0.5 * x1 - 0.25 * x2)
        levels[i] = 1.0 + 0.15 * x2 + rng.normal(0, 0.01)
    idx = pd.date_range(start, periods=steps, freq=f"{step_min}min", tz="UTC")
    df = pd.DataFrame({"timestamp": idx, "water_level_m": np.round(levels, 3), "rain_mm": np.nan})
    labels = hourly_idx + pd.Timedelta(hours=1)                # a chuva rotulada em T é a da hora anterior
    rain_rows = pd.DataFrame({"timestamp": labels, "water_level_m": np.nan, "rain_mm": np.round(rain, 2)})
    out = pd.concat([df, rain_rows]).sort_values("timestamp", kind="stable").reset_index(drop=True)
    out["timestamp"] = out["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--step-min", type=int, default=10)
    p.add_argument("--out", default="dados_sinteticos.csv")
    a = p.parse_args()
    with open(a.out, "w", encoding="utf-8", newline="") as f:
        f.write("# SINTETICO: dados gerados por simulacao, nao representam o rio real\n")
        generate(a.days, a.step_min).to_csv(f, index=False)
    print(f"{a.out}: série SINTÉTICA de {a.days} dias (não representa o rio real)")
