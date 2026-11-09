import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(Path(__file__).parent))

os.environ.setdefault("DATABASE_URL", os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres@localhost:5432/aviationlog_test"))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "admin-password")
os.environ["DISABLE_SCHEDULER"] = "1"
os.environ["ADSBDB_URL"] = "off"  # no network in tests; test_adsbdb switches it on with a fake answer
os.environ["AIRPORTDATA_URL"] = "off"  # same for airport-data.com (test_airportdata uses a fake page)


@pytest.fixture(scope="session")
def tmp_data(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    os.environ["DATA_DIR"] = str(data)
    os.environ["BACKUP_DIR"] = str(data / "backups")
    return data


@pytest.fixture()
def db(tmp_data):
    import photos
    from database import SessionLocal, engine

    photos.DATA_DIR = tmp_data
    photos.THUMB_DIR = tmp_data / "thumbs"
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    import main

    main.migrate()
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def client(db, tmp_data, monkeypatch):
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        r = c.post("/login", data={"username": "admin", "password": "admin-password"})
        assert r.status_code == 200
        yield c
