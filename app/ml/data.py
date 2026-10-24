"""Carregamento de séries (CSV manual ou banco) e conversão para uma grade regular de tempo."""
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ProcessedReading, Reading, WeatherPoint, WeatherRecent


def _to_utc(ts: pd.Series, tz: str | None) -> pd.DatetimeIndex:
    idx = pd.to_datetime(ts, errors="raise")
    if idx.dt.tz is None:
        if not tz:
            raise ValueError("Horários sem fuso: informe o fuso (ex.: America/Sao_Paulo)")
        idx = idx.dt.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
    return pd.DatetimeIndex(idx.dt.tz_convert("UTC"))


def load_csv(path: str, tz: str | None = "America/Sao_Paulo") -> tuple[pd.Series, pd.Series | None]:
    """CSV com colunas: timestamp, water_level_m e (opcional) rain_mm.
    rain_mm = chuva da hora que TERMINA no horário da linha (mm). Pode estar só nas linhas cheias."""
    df = pd.read_csv(path, comment="#")
    missing = {"timestamp", "water_level_m"} - set(df.columns)
    if missing:
        raise ValueError(f"CSV sem as colunas: {', '.join(sorted(missing))}")
    df.index = _to_utc(df["timestamp"], tz)
    df = df[~df.index.isna()].sort_index()
    df = df[~df.index.duplicated(keep="last")]
    level = pd.to_numeric(df["water_level_m"], errors="coerce").dropna()
    rain = None
    if "rain_mm" in df.columns:
        r = pd.to_numeric(df["rain_mm"], errors="coerce").dropna()
        rain = r if len(r) else None
    return level, rain


def csv_is_synthetic(path: str) -> bool:
    with open(path, encoding="utf-8") as f:
        return f.readline().lstrip().upper().startswith("# SINTETICO")


def load_db(db: Session, station_id: str, since: datetime | None = None
            ) -> tuple[pd.Series, pd.Series | None]:
    """Leituras confiáveis do banco (sem as marcadas como 'jump') e chuva horária do ponto local da estação."""
    q = (select(Reading.timestamp, Reading.water_level_m, ProcessedReading.flags)
         .outerjoin(ProcessedReading, ProcessedReading.reading_id == Reading.id)
         .where(Reading.station_id == station_id).order_by(Reading.timestamp))
    if since is not None:
        q = q.where(Reading.timestamp >= since)
    rows = [(r.timestamp, r.water_level_m) for r in db.execute(q) if "jump" not in (r.flags or "").split(",")]
    level = pd.Series([v for _, v in rows], index=pd.DatetimeIndex([t for t, _ in rows], tz="UTC"), dtype=float)

    point = db.scalar(select(WeatherPoint).where(WeatherPoint.station_id == station_id,
                                                 WeatherPoint.role == "local", WeatherPoint.active).limit(1))
    rain = None
    if point is not None:
        qr = select(WeatherRecent.valid_time, WeatherRecent.precipitation_mm, WeatherRecent.source).where(
            WeatherRecent.point_id == point.id)
        if since is not None:
            qr = qr.where(WeatherRecent.valid_time >= since - timedelta(days=4))
        by_time: dict = {}
        for t, mm, src in db.execute(qr):
            if t not in by_time or src == "open-meteo-archive":      # o arquivo (ERA5) vale mais que a previsão
                by_time[t] = mm
        if by_time:
            rain = pd.Series(by_time, dtype=float)
            rain.index = pd.DatetimeIndex(rain.index, tz="UTC").sort_values()
            rain = rain.sort_index()
    return level.sort_index(), rain


def to_grid(level: pd.Series, step_min: int = 10, max_gap_min: int = 60) -> pd.Series:
    """Nível numa grade regular: mediana por intervalo; falhas de até `max_gap_min` são interpoladas e as
    maiores ficam vazias (NaN). Nunca inventa dado em buracos longos."""
    if level.empty:
        return level
    step = f"{step_min}min"
    grid = level.sort_index().resample(step, label="left", closed="left").median()
    limit = max(1, max_gap_min // step_min)
    missing = grid.isna()
    run_id = (missing != missing.shift()).cumsum()
    too_long = missing & (missing.groupby(run_id).transform("sum") > limit)   # buraco inteiro maior que o limite
    filled = grid.interpolate(limit_area="inside")
    filled[too_long] = float("nan")                                           # não inventa dado em buracos longos
    return filled


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
