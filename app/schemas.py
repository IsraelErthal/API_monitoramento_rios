from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ReadingIn(BaseModel):
    """Contrato de entrada: o que o receptor envia."""
    station_id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    timestamp: AwareDatetime                     # UTC, ex.: 2026-10-02T14:30:00Z
    sequence: int = Field(ge=0)                  # contador crescente por sensor
    water_level_m: float = Field(allow_inf_nan=False)


class ReadingAck(BaseModel):
    status: str                                  # "accepted" ou "duplicate"
    reading_id: int


class ReadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    timestamp: AwareDatetime
    sequence: int
    water_level_m: float


class StationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    latitude: float | None
    longitude: float | None
    attention_level_m: float | None
    alert_level_m: float | None
    flood_level_m: float | None


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    severity: str
    message: str
    opened_at: AwareDatetime
    closed_at: AwareDatetime | None
    reading_id: int | None
    source: str


class ProcessedOut(BaseModel):
    delta_5m_m: float | None
    delta_15m_m: float | None
    delta_60m_m: float | None
    delta_3h_m: float | None
    moving_avg_1h_m: float | None
    max_3h_m: float | None
    rate_m_h: float | None
    trend: str | None
    risk: str
    reasons: list[str]
    flags: list[str]


class StatusOut(BaseModel):
    station_id: str
    reading: ReadingOut | None
    processed: ProcessedOut | None
    data_age_s: int | None          # idade da última leitura recebida
    stale: bool                     # sem dados recentes: o risco mostrado pode estar defasado
    open_alerts: list[AlertOut]


class PredictionItem(BaseModel):
    horizon_h: int
    target_time: AwareDatetime
    predicted_level_m: float
    delta_m: float                  # previsto menos o nível-base


class PredictionsOut(BaseModel):
    station_id: str
    mode: str                       # "shadow": experimental, não alimenta alertas
    made_at: AwareDatetime | None
    model_version: str | None
    base_level_m: float | None
    predictions: list[PredictionItem]
