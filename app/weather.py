"""Coletor de chuva (Open-Meteo): previsão e chuva recente por ponto da bacia.

  python -m app.weather add-point --station rio-piloto-01 --name "Ponto local" --lat -28.6775 --lon -49.3708
  python -m app.weather collect                 # rodar a cada hora (cron / Agendador de Tarefas)
  python -m app.weather backfill --from 2023-01-01 --to 2025-12-31   # histórico para treino

Dados: Weather data by Open-Meteo.com (CC BY 4.0). Uso gratuito é para fins não comerciais.
"""
import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal, init_db
from app.models import Station, WeatherForecast, WeatherPoint, WeatherRecent

log = logging.getLogger("weather")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
SRC_FORECAST = "open-meteo-forecast"
SRC_ARCHIVE = "open-meteo-archive"
PAST_DAYS = 3
FORECAST_DAYS = 3
MAX_MM_PER_HOUR = 300.0       # acima disso o valor é tratado como erro de dado


def fetch_json(url: str, params: dict, retries: int = 3, timeout: int = 15) -> dict:
    """GET com nova tentativa em falhas temporárias (rede, 429, 5xx). Erro de parâmetro não repete."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code < 400:
                return r.json()
            if r.status_code != 429 and r.status_code < 500:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            last = RuntimeError(f"HTTP {r.status_code}")
        except (requests.RequestException, ValueError) as e:
            last = e
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Falha ao consultar {url}: {last}")


def parse_hourly(payload: dict) -> list[tuple[datetime, float]]:
    """Extrai (hora UTC, mm) da resposta. Ignora valores nulos, negativos ou absurdos."""
    hourly = payload.get("hourly") or {}
    times, values = hourly.get("time"), hourly.get("precipitation")
    if not times or values is None or len(times) != len(values):
        raise ValueError("Resposta inesperada do Open-Meteo (hourly.time / hourly.precipitation)")
    rows = []
    for t, v in zip(times, values):
        if v is None:
            continue
        if not (0 <= v <= MAX_MM_PER_HOUR):
            log.warning("Chuva fora do plausível em %s: %s mm/h (ignorada)", t, v)
            continue
        rows.append((datetime.fromisoformat(t).replace(tzinfo=timezone.utc), float(v)))
    return rows


def _upsert_recent(db: Session, point_id: int, source: str, collected_at: datetime,
                   rows: list[tuple[datetime, float]]) -> int:
    if not rows:
        return 0
    lo, hi = min(t for t, _ in rows), max(t for t, _ in rows)
    existing = {
        r.valid_time: r
        for r in db.scalars(select(WeatherRecent).where(
            WeatherRecent.point_id == point_id, WeatherRecent.source == source,
            WeatherRecent.valid_time.between(lo, hi)))
    }
    for t, v in rows:
        row = existing.get(t)
        if row:
            row.precipitation_mm, row.collected_at = v, collected_at
        else:
            db.add(WeatherRecent(point_id=point_id, valid_time=t, precipitation_mm=v,
                                 source=source, collected_at=collected_at))
    return len(rows)


def collect_point(db: Session, point: WeatherPoint, now: datetime | None = None,
                  force: bool = False, min_interval_min: int = 30) -> dict:
    """Uma coleta: horas futuras viram previsão (acrescenta); horas passadas atualizam a chuva recente."""
    now = now or datetime.now(timezone.utc)
    if not force:
        last = db.scalar(select(func.max(WeatherForecast.collected_at))
                         .where(WeatherForecast.point_id == point.id))
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last < timedelta(minutes=min_interval_min):
                return {"status": "skipped", "forecast": 0, "recent": 0}

    payload = fetch_json(FORECAST_URL, {
        "latitude": point.latitude, "longitude": point.longitude,
        "hourly": "precipitation", "past_days": PAST_DAYS, "forecast_days": FORECAST_DAYS,
        "timezone": "UTC",
    })
    rows = parse_hourly(payload)
    hour_now = now.replace(minute=0, second=0, microsecond=0)
    future = [(t, v) for t, v in rows if t >= hour_now]
    past = [(t, v) for t, v in rows if t < hour_now]

    for t, v in future:
        db.add(WeatherForecast(point_id=point.id, collected_at=now, valid_time=t,
                               precipitation_mm=v, source=SRC_FORECAST))
    n_recent = _upsert_recent(db, point.id, SRC_FORECAST, now, past)
    db.commit()
    return {"status": "ok", "forecast": len(future), "recent": n_recent}


def collect_all(force: bool = False) -> tuple[int, int]:
    """Coleta todos os pontos ativos. A falha de um ponto não impede os demais."""
    ok = failed = 0
    with SessionLocal() as db:
        for point in db.scalars(select(WeatherPoint).where(WeatherPoint.active)).all():
            try:
                res = collect_point(db, point, force=force)
                log.info("Ponto %s (%s): %s, %d previsões, %d recentes",
                         point.id, point.name, res["status"], res["forecast"], res["recent"])
                ok += 1
            except Exception as e:                      # noqa: BLE001
                db.rollback()
                log.error("Ponto %s (%s) falhou: %s", point.id, point.name, e)
                failed += 1
    return ok, failed


def backfill_point(db: Session, point: WeatherPoint, start: date, end: date) -> int:
    """Histórico horário do arquivo (ERA5), em blocos de um ano. Reexecutar não duplica."""
    total, cur = 0, start
    while cur <= end:
        chunk_end = min(end, date(cur.year, 12, 31))
        payload = fetch_json(ARCHIVE_URL, {
            "latitude": point.latitude, "longitude": point.longitude,
            "start_date": cur.isoformat(), "end_date": chunk_end.isoformat(),
            "hourly": "precipitation", "timezone": "UTC",
        })
        total += _upsert_recent(db, point.id, SRC_ARCHIVE, datetime.now(timezone.utc), parse_hourly(payload))
        db.commit()
        cur = chunk_end + timedelta(days=1)
    return total


def add_point(db: Session, station_id: str, name: str, lat: float, lon: float, role: str = "local") -> WeatherPoint:
    if db.get(Station, station_id) is None:
        raise ValueError(f"Estação {station_id!r} não existe")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Coordenadas fora do intervalo válido")
    if role not in ("local", "upstream"):
        raise ValueError("role deve ser 'local' ou 'upstream'")
    point = WeatherPoint(name=name, latitude=lat, longitude=lon, station_id=station_id, role=role)
    db.add(point)
    db.commit()
    return point


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m app.weather")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-point", help="cadastra um ponto de coleta")
    a.add_argument("--station", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--lat", type=float, required=True)
    a.add_argument("--lon", type=float, required=True)
    a.add_argument("--role", choices=["local", "upstream"], default="local")
    sub.add_parser("list-points", help="lista os pontos")
    c = sub.add_parser("collect", help="coleta previsão e chuva recente")
    c.add_argument("--force", action="store_true", help="ignora o intervalo mínimo entre coletas")
    b = sub.add_parser("backfill", help="baixa histórico para treino")
    b.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    b.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    b.add_argument("--point", type=int, help="id do ponto (padrão: todos os ativos)")
    args = p.parse_args(argv)

    init_db()
    if args.cmd == "add-point":
        with SessionLocal() as db:
            try:
                pt = add_point(db, args.station, args.name, args.lat, args.lon, args.role)
            except ValueError as e:
                print(f"Erro: {e}")
                return 1
            print(f"Ponto {pt.id} cadastrado: {pt.name} ({pt.latitude}, {pt.longitude})")
        return 0
    if args.cmd == "list-points":
        with SessionLocal() as db:
            for pt in db.scalars(select(WeatherPoint).order_by(WeatherPoint.id)):
                print(f"{pt.id}\t{pt.station_id}\t{pt.role}\t{pt.name}\t{pt.latitude}, {pt.longitude}\t{'ativo' if pt.active else 'inativo'}")
        return 0
    if args.cmd == "collect":
        ok, failed = collect_all(force=args.force)
        print(f"Coleta concluída: {ok} ponto(s) ok, {failed} com falha")
        return 1 if failed else 0
    if args.cmd == "backfill":
        failed = 0
        with SessionLocal() as db:
            q = select(WeatherPoint).where(WeatherPoint.active)
            if args.point:
                q = q.where(WeatherPoint.id == args.point)
            for pt in db.scalars(q).all():
                try:
                    n = backfill_point(db, pt, args.start, args.end)
                    print(f"Ponto {pt.id} ({pt.name}): {n} horas gravadas")
                except Exception as e:                  # noqa: BLE001
                    db.rollback()
                    log.error("Ponto %s falhou: %s", pt.id, e)
                    failed += 1
        return 1 if failed else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
