"""
Database engine and session setup.

Required env var:
  DATABASE_URL — PostgreSQL connection string, e.g.:
    postgresql+psycopg2://user:pass@host:5432/dbname
    (Railway injects this automatically as DATABASE_URL)
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

def _get_database_url() -> str:
    url = os.environ["DATABASE_URL"]
    # SQLAlchemy requires the psycopg2 dialect prefix;
    # Railway provides "postgresql://" which must become "postgresql+psycopg2://"
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def _make_engine():
    return create_engine(
        _get_database_url(),
        pool_pre_ping=True,   # test connections before use (handles Railway sleep/wake)
        pool_size=5,
        max_overflow=2,
    )


# Lazily initialised — engine is created on first access, not at import time.
# This allows the module to be imported in tests or scripts without DATABASE_URL set.
_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = _make_engine()
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autocommit=False, autoflush=False)
    return _SessionLocal


def get_db():
    """FastAPI dependency: yields a DB session and ensures it's closed."""
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()
