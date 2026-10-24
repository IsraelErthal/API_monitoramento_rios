"""Features e alvos. REGRA DE OURO: a feature de um instante t só pode usar informação disponível em t.
(Um valor do futuro, por engano, faz o modelo parecer perfeito nos testes e falhar na vida real.)"""
import numpy as np
import pandas as pd

HORIZONS_H = (1, 3, 6)
RAIN_WINDOWS_H = (1, 3, 24, 72)


def rain_features(rain: pd.Series | None, grid: pd.DatetimeIndex) -> pd.DataFrame:
    """Chuva acumulada nas últimas N horas. O valor rotulado em T é a chuva da hora anterior a T, só conhecida
    em T: no instante t usamos apenas as horas já completas (rótulo <= t)."""
    out = pd.DataFrame(index=grid)
    if rain is None or rain.empty:
        return out
    hourly = rain.sort_index().resample("1h").sum(min_count=1)
    for n in RAIN_WINDOWS_H:
        acc = hourly.rolling(n, min_periods=max(1, n // 2)).sum()
        out[f"rain_{n}h"] = acc.reindex(grid, method="ffill", tolerance=pd.Timedelta(hours=2))
    return out


def build_features(level: pd.Series, rain: pd.Series | None, step_min: int) -> pd.DataFrame:
    """`level` já na grade regular. Retorna uma linha de features por instante."""
    per_h = 60 // step_min
    X = pd.DataFrame(index=level.index)
    X["level"] = level
    for name, hours in (("d_30m", 0.5), ("d_1h", 1), ("d_3h", 3), ("d_6h", 6), ("d_24h", 24)):
        k = max(1, int(round(hours * per_h)))
        X[name] = level - level.shift(k)
    X["rate_1h"] = X["d_1h"]                                   # m/h
    r6 = level.rolling(6 * per_h, min_periods=3 * per_h)
    X["mean_6h"], X["max_6h"], X["min_6h"] = r6.mean(), r6.max(), r6.min()
    X["rel_max_6h"] = level - X["max_6h"]                      # já passou do pico?
    r7 = level.rolling(7 * 24 * per_h, min_periods=24 * per_h)
    X["anomaly_7d"] = level - r7.mean()                        # quão acima do nível "médio" do rio
    return X.join(rain_features(rain, level.index))


def build_targets(level: pd.Series, step_min: int, horizons=HORIZONS_H) -> pd.DataFrame:
    """Alvo = variação do nível daqui a h horas (nível futuro menos o atual). Prever a variação é mais
    estável do que prever o nível absoluto, e o 'não muda nada' vira o baseline natural (persistência)."""
    per_h = 60 // step_min
    return pd.DataFrame({f"y_{h}h": level.shift(-h * per_h) - level for h in horizons}, index=level.index)


def feature_columns(X: pd.DataFrame) -> list[str]:
    return list(X.columns)


def align_columns(X: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Garante as mesmas colunas do treino (as que faltarem, como a chuva, ficam NaN)."""
    return X.reindex(columns=columns, fill_value=np.nan)
