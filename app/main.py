import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import rules, settings
from app.db import get_db, init_db
from app.live import event_stream
from app.models import Alert, Prediction, ProcessedReading, Reading, Station
from app.pipeline import process_new_reading
from app.schemas import AlertOut, PredictionItem, PredictionsOut, ProcessedOut, ReadingAck, ReadingIn, ReadingOut, StationOut, StatusOut

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Monitoramento de rios", lifespan=lifespan)
router = APIRouter(prefix="/api/v1")


def require_token(expected: str):
    """Autenticação por cabeçalho: Authorization: Bearer <token>."""
    wanted = f"Bearer {expected}".encode()

    def dependency(authorization: str | None = Header(default=None)):
        if not authorization or not secrets.compare_digest(authorization.encode(), wanted):
            raise HTTPException(401, "Token inválido")

    return dependency


ingest_auth = Depends(require_token(settings.ingest_token))
api_auth = Depends(require_token(settings.api_token))


def _station_or_404(db: Session, station_id: str) -> Station:
    station = db.get(Station, station_id)
    if station is None or not station.active:
        raise HTTPException(404, "Estação não encontrada")
    return station


# --- Entrada (ESP32 receptor) ---------------------------------------------------------

@router.post("/readings", status_code=201, response_model=ReadingAck, dependencies=[ingest_auth])
def create_reading(body: ReadingIn, response: Response, bg: BackgroundTasks, db: Session = Depends(get_db)):
    """201 = nova; 200 = duplicada (não reenviar); 422 = inválida (reenviar não resolve);
    401 = token errado; 5xx = erro nosso (reenviar depois)."""
    station = db.get(Station, body.station_id)
    if station is None or not station.active:
        raise HTTPException(422, "station_id desconhecido ou inativo")

    ts = body.timestamp.astimezone(timezone.utc)
    if ts > datetime.now(timezone.utc) + timedelta(seconds=settings.future_tolerance_s):
        raise HTTPException(422, "timestamp no futuro")
    if not (station.min_level_m <= body.water_level_m <= station.max_level_m):
        log.warning("Nível fora da faixa em %s: %s m", station.id, body.water_level_m)
        raise HTTPException(
            422, f"water_level_m fora da faixa física ({station.min_level_m} a {station.max_level_m} m)"
        )

    window = timedelta(minutes=settings.dup_window_min)
    existing = db.scalar(
        select(Reading).where(
            Reading.station_id == station.id,
            Reading.sequence == body.sequence,
            Reading.timestamp.between(ts - window, ts + window),
        )
    ) or db.scalar(
        select(Reading).where(Reading.station_id == station.id, Reading.timestamp == ts)
    )
    if existing:
        response.status_code = 200
        return ReadingAck(status="duplicate", reading_id=existing.id)

    reading = Reading(
        station_id=station.id, timestamp=ts,
        sequence=body.sequence, water_level_m=body.water_level_m,
    )
    db.add(reading)
    try:
        db.commit()
    except IntegrityError:           # dois envios simultâneos da mesma leitura
        db.rollback()
        existing = db.scalar(
            select(Reading).where(Reading.station_id == station.id, Reading.timestamp == ts)
        )
        response.status_code = 200
        return ReadingAck(status="duplicate", reading_id=existing.id)
    bg.add_task(process_new_reading, reading.id)        # cálculo de risco depois de responder ao receptor
    return ReadingAck(status="accepted", reading_id=reading.id)


# --- Saída (dashboard) ----------------------------------------------------------------

@router.get("/stations", response_model=list[StationOut], dependencies=[api_auth])
def list_stations(db: Session = Depends(get_db)):
    return db.scalars(select(Station).where(Station.active).order_by(Station.id)).all()


@router.get("/stations/{station_id}/latest", response_model=ReadingOut, dependencies=[api_auth])
def latest(station_id: str, db: Session = Depends(get_db)):
    _station_or_404(db, station_id)
    reading = db.scalar(
        select(Reading).where(Reading.station_id == station_id).order_by(Reading.timestamp.desc()).limit(1)
    )
    if reading is None:
        raise HTTPException(404, "Sem leituras para esta estação")
    return reading


@router.get("/stations/{station_id}/history", response_model=list[ReadingOut], dependencies=[api_auth])
def history(
    station_id: str,
    date_from: AwareDatetime | None = Query(default=None, alias="from"),
    date_to: AwareDatetime | None = Query(default=None, alias="to"),
    limit: int = Query(1000, ge=1, le=10000),
    db: Session = Depends(get_db),
):
    """Ordem cronológica (pronta para gráfico). Padrão: últimas 24 h. Use 'Z' para UTC nas datas."""
    _station_or_404(db, station_id)
    date_to = date_to or datetime.now(timezone.utc)
    date_from = date_from or date_to - timedelta(hours=24)
    rows = db.scalars(
        select(Reading)
        .where(Reading.station_id == station_id, Reading.timestamp.between(date_from, date_to))
        .order_by(Reading.timestamp.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))


def _processed_out(p: ProcessedReading) -> ProcessedOut:
    return ProcessedOut(
        delta_5m_m=p.delta_5m_m, delta_15m_m=p.delta_15m_m, delta_60m_m=p.delta_60m_m, delta_3h_m=p.delta_3h_m,
        moving_avg_1h_m=p.moving_avg_1h_m, max_3h_m=p.max_3h_m, rate_m_h=p.rate_m_h, trend=p.trend, risk=p.risk,
        reasons=[r for r in p.reasons.split(" | ") if r], flags=[f for f in p.flags.split(",") if f],
    )


@router.get("/stations/{station_id}/status", response_model=StatusOut, dependencies=[api_auth])
def station_status(station_id: str, db: Session = Depends(get_db)):
    """Tudo o que o painel principal precisa: última leitura, tendência, risco, idade do dado e alertas abertos."""
    _station_or_404(db, station_id)
    reading = db.scalar(select(Reading).where(Reading.station_id == station_id)
                        .order_by(Reading.timestamp.desc()).limit(1))
    processed = db.scalar(select(ProcessedReading).where(ProcessedReading.station_id == station_id)
                          .order_by(ProcessedReading.timestamp.desc()).limit(1))
    last_rx = db.scalar(select(func.max(Reading.received_at)).where(Reading.station_id == station_id))
    age = None
    if last_rx is not None:
        if last_rx.tzinfo is None:
            last_rx = last_rx.replace(tzinfo=timezone.utc)
        age = int((datetime.now(timezone.utc) - last_rx).total_seconds())
    open_alerts = db.scalars(select(Alert).where(Alert.station_id == station_id, Alert.closed_at.is_(None))
                             .order_by(Alert.opened_at.desc(), Alert.id.desc())).all()
    return StatusOut(
        station_id=station_id, reading=reading, processed=_processed_out(processed) if processed else None,
        data_age_s=age, stale=age is None or age > rules.offline_min * 60, open_alerts=open_alerts,
    )


@router.get("/stations/{station_id}/alerts", response_model=list[AlertOut], dependencies=[api_auth])
def station_alerts(station_id: str, open_only: bool = False, limit: int = Query(100, ge=1, le=1000),
                   db: Session = Depends(get_db)):
    _station_or_404(db, station_id)
    q = select(Alert).where(Alert.station_id == station_id)
    if open_only:
        q = q.where(Alert.closed_at.is_(None))
    return db.scalars(q.order_by(Alert.opened_at.desc(), Alert.id.desc()).limit(limit)).all()


@router.get("/live", dependencies=[api_auth])
async def live(request: Request):
    """Tempo real (SSE). Eventos: hello, reading, alert_opened, alert_closed, prediction.
    Ao (re)conectar, busque o estado atual em /status: o stream só traz o que acontece depois."""
    return StreamingResponse(
        event_stream(request), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/stations/{station_id}/predictions", response_model=PredictionsOut, dependencies=[api_auth])
def station_predictions(station_id: str, db: Session = Depends(get_db)):
    """Última previsão por horizonte. Modo 'shadow': experimental, não alimenta o risco nem os alertas."""
    _station_or_404(db, station_id)
    made_at = db.scalar(select(func.max(Prediction.made_at)).where(Prediction.station_id == station_id))
    if made_at is None:
        return PredictionsOut(station_id=station_id, mode="shadow", made_at=None, model_version=None,
                              base_level_m=None, predictions=[])
    rows = db.scalars(select(Prediction).where(Prediction.station_id == station_id, Prediction.made_at == made_at)
                      .order_by(Prediction.horizon_h)).all()
    if made_at.tzinfo is None:
        made_at = made_at.replace(tzinfo=timezone.utc)
    return PredictionsOut(
        station_id=station_id, mode=rows[0].mode, made_at=made_at, model_version=rows[0].model_version,
        base_level_m=rows[0].base_level_m,
        predictions=[PredictionItem(horizon_h=r.horizon_h, target_time=r.target_time,
                                    predicted_level_m=r.predicted_level_m,
                                    delta_m=r.predicted_level_m - r.base_level_m) for r in rows])


@router.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok"}


app.include_router(router)
