import pytest

from src.db import init_db, make_engine, make_session_factory


@pytest.fixture
def db():
    """A fresh in-memory database for each test."""
    engine = make_engine(":memory:")
    init_db(engine)
    with make_session_factory(engine)() as session:
        yield session
