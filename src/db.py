"""Engine, session factory, and table creation.

No migrations tool (Alembic): one user, small schema, `create_all` is enough.
If the schema starts changing after real data exists, add Alembic then.
"""

from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.config import get_settings
from src.models import Base


def make_engine(db_path: str | None = None) -> Engine:
    path = db_path or get_settings().db_path
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{path}")


def init_db(engine: Engine) -> None:
    """Create all tables that do not exist yet. Safe to call repeatedly."""
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine)
