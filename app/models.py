from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Guarda sempre em UTC e devolve sempre com fuso, tanto no SQLite quanto no PostgreSQL."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime sem fuso horário")
        value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Station(Base):
    __tablename__ = "stations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)   # station_id
    name: Mapped[str] = mapped_column(String(100))
    latitude: Mapped[float | None]
    longitude: Mapped[float | None]
    attention_level_m: Mapped[float | None]    # cotas de risco, em metros, mesma referência da régua
    alert_level_m: Mapped[float | None]
    flood_level_m: Mapped[float | None]
    min_level_m: Mapped[float] = mapped_column(default=0.0)    # faixa física válida do sensor
    max_level_m: Mapped[float] = mapped_column(default=10.0)
    active: Mapped[bool] = mapped_column(default=True)


class Reading(Base):
    """Leitura bruta. Nunca é alterada: cálculos e correções vão para tabelas derivadas."""
    __tablename__ = "readings"
    __table_args__ = (UniqueConstraint("station_id", "timestamp", name="uq_reading_station_ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime)        # horário da medição (UTC)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    sequence: Mapped[int]
    water_level_m: Mapped[float]


class WeatherPoint(Base):
    """Ponto da bacia onde se coleta chuva: o local da estação e/ou pontos a montante."""
    __tablename__ = "weather_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    latitude: Mapped[float]
    longitude: Mapped[float]
    station_id: Mapped[str | None] = mapped_column(ForeignKey("stations.id"), index=True)  # estação que ele alimenta
    role: Mapped[str] = mapped_column(String(20), default="local")                          # local | upstream
    active: Mapped[bool] = mapped_column(default=True)


class WeatherForecast(Base):
    """Previsão de chuva (mm/h). Só acrescenta: cada coleta é guardada, nunca sobrescrita.
    collected_at = quando perguntamos; valid_time = hora a que o valor se refere."""
    __tablename__ = "weather_forecasts"
    __table_args__ = (
        UniqueConstraint("point_id", "collected_at", "valid_time", name="uq_forecast"),
        Index("ix_forecast_point_valid", "point_id", "valid_time"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    point_id: Mapped[int] = mapped_column(ForeignKey("weather_points.id"))
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime)
    valid_time: Mapped[datetime] = mapped_column(UTCDateTime)
    precipitation_mm: Mapped[float]
    source: Mapped[str] = mapped_column(String(40), default="open-meteo-forecast")


class WeatherRecent(Base):
    """Chuva já ocorrida, segundo o modelo meteorológico (não é pluviômetro).
    source separa o que veio da API de previsão (horas recentes) do arquivo histórico (ERA5)."""
    __tablename__ = "weather_recent"
    __table_args__ = (UniqueConstraint("point_id", "valid_time", "source", name="uq_recent"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    point_id: Mapped[int] = mapped_column(ForeignKey("weather_points.id"), index=True)
    valid_time: Mapped[datetime] = mapped_column(UTCDateTime)
    precipitation_mm: Mapped[float]
    source: Mapped[str] = mapped_column(String(40))
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ProcessedReading(Base):
    """Dados derivados de uma leitura. A leitura bruta (readings) nunca é alterada."""
    __tablename__ = "processed_readings"
    __table_args__ = (Index("ix_processed_station_ts", "station_id", "timestamp"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    reading_id: Mapped[int] = mapped_column(ForeignKey("readings.id"), unique=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime)
    delta_5m_m: Mapped[float | None]
    delta_15m_m: Mapped[float | None]
    delta_60m_m: Mapped[float | None]
    delta_3h_m: Mapped[float | None]
    moving_avg_1h_m: Mapped[float | None]
    max_3h_m: Mapped[float | None]
    rate_m_h: Mapped[float | None]                     # taxa de subida (inclinação dos últimos 60 min)
    trend: Mapped[str | None] = mapped_column(String(10))   # rising | steady | falling
    flags: Mapped[str] = mapped_column(String(100), default="")   # gap, jump, stuck
    risk: Mapped[str] = mapped_column(String(10))      # normal | attention | alert | critical | unknown
    reasons: Mapped[str] = mapped_column(String(500), default="")
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class Alert(Base):
    """No máximo um alerta aberto por estação e tipo. A escalada fecha o anterior e abre outro."""
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alert_station_closed", "station_id", "closed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    kind: Mapped[str] = mapped_column(String(20))      # risk | sensor_offline | sensor_stuck
    severity: Mapped[str] = mapped_column(String(10))  # attention | alert | critical | warning
    message: Mapped[str] = mapped_column(String(500))
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reading_id: Mapped[int | None] = mapped_column(ForeignKey("readings.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(10), default="rule")   # rule | model (no futuro)


class Prediction(Base):
    """Previsão do modelo. Em modo 'shadow' ela é gravada e comparada com o que ocorreu, mas NÃO gera alerta."""
    __tablename__ = "predictions"
    __table_args__ = (
        UniqueConstraint("station_id", "made_at", "horizon_h", "model_version", name="uq_prediction"),
        Index("ix_prediction_station_made", "station_id", "made_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    made_at: Mapped[datetime] = mapped_column(UTCDateTime)       # instante-base (última leitura usada)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    horizon_h: Mapped[int]
    target_time: Mapped[datetime] = mapped_column(UTCDateTime)   # made_at + horizonte
    base_level_m: Mapped[float]
    predicted_level_m: Mapped[float]
    model_version: Mapped[str] = mapped_column(String(40))
    mode: Mapped[str] = mapped_column(String(10), default="shadow")
