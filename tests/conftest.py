import os
import pathlib
import tempfile

_tmp = pathlib.Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_tmp / 'test.db').as_posix()}"
os.environ["INGEST_TOKEN"] = "ingest-test"
os.environ["API_TOKEN"] = "api-test"
os.environ["MODELS_DIR"] = str(_tmp / "models")

import pytest                                   # noqa: E402
from fastapi.testclient import TestClient       # noqa: E402

from app.db import engine, init_db              # noqa: E402
from app.main import app                        # noqa: E402
from app.models import Base                     # noqa: E402


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db():
    from app.db import SessionLocal
    Base.metadata.drop_all(engine)
    init_db()
    with SessionLocal() as session:
        yield session
