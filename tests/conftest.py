from pathlib import Path

import pytest

from src.db import init_db, make_engine, make_session_factory
from src.import_csv import import_sessions, import_wellness

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture
def db():
    """A fresh in-memory database for each test."""
    engine = make_engine(":memory:")
    init_db(engine)
    with make_session_factory(engine)() as session:
        yield session


@pytest.fixture
def sample_db(db):
    """The committed synthetic sample data (2026-01-05 .. 2026-03-05) in an in-memory database."""
    import_sessions(db, DATA / "sample_sessions.csv")
    import_wellness(db, DATA / "sample_wellness.csv")
    return db
