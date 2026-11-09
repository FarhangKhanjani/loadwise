"""The data states a scenario can start from. Each eval run gets a fresh in-memory database."""

from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from src.import_csv import import_sessions, import_wellness
from src.models import TrainingSession

DATA = Path(__file__).resolve().parent.parent / "data"


def build(name: str, db: Session) -> None:
    if name == "empty":
        return
    if name == "sample":
        # Synthetic 2026-01-05 .. 2026-03-05 with a load spike in Feb 9-15 (ACWR 1.69 on Feb 15).
        import_sessions(db, DATA / "sample_sessions.csv")
        import_wellness(db, DATA / "sample_wellness.csv")
        return
    if name == "gap":
        # Like the real history in November 2026: logging stopped in July, then one gym
        # session on Nov 8. On Nov 9 ACWR is 4.0 with only 1 of 4 weeks logged.
        db.add_all([
            TrainingSession(date=date(2026, 7, 20), sport_type="running", duration_min=45, rpe=6),
            TrainingSession(date=date(2026, 7, 26), sport_type="running", duration_min=45, rpe=6),
            TrainingSession(date=date(2026, 11, 8), sport_type="gym", duration_min=60, rpe=7),
        ])
        db.commit()
        return
    raise ValueError(f"unknown dataset {name!r}")


DATASETS = ("empty", "sample", "gap")
