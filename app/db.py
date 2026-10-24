from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import Base, Station

_is_sqlite = settings.database_url.startswith("sqlite")
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_con, _):
        cur = dbapi_con.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_db():
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    """Cria as tabelas e, se não houver nenhuma estação, cadastra a do piloto."""
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if db.scalar(select(Station.id).limit(1)) is None:
            # ATENÇÃO: cotas e faixa são PLACEHOLDERS. Troque pelos valores reais do ponto.
            db.add(Station(
                id="rio-piloto-01", name="Estação piloto",
                attention_level_m=2.5, alert_level_m=3.0, flood_level_m=3.5,
                min_level_m=0.0, max_level_m=8.0,
            ))
            db.commit()
