import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://aviationlog:aviationlog@db:5432/aviationlog"
)

# SQLAlchemy 2.1 defaults to psycopg 3 for "postgresql://"; this app uses psycopg2.
_SQLA_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1) if DATABASE_URL.startswith(
    "postgresql://") else DATABASE_URL

engine = create_engine(_SQLA_URL, pool_pre_ping=True, pool_size=10, max_overflow=10)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
