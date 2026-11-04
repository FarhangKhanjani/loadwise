"""Database tables (SQLAlchemy 2.x typed declarative style).

Dates are stored as plain calendar dates (the athlete's local day), not timestamps,
because every metric is computed per day.
"""

import datetime as dt

from sqlalchemy import JSON, CheckConstraint, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TrainingSession(Base):
    # Named TrainingSession to avoid confusion with SQLAlchemy's own Session class.
    __tablename__ = "sessions"
    __table_args__ = (
        # Makes CSV import safe to re-run: the same session cannot be inserted twice.
        UniqueConstraint("date", "sport_type", "duration_min", "rpe", name="uq_session"),
        CheckConstraint("duration_min > 0", name="ck_duration_positive"),
        CheckConstraint("rpe BETWEEN 1 AND 10", name="ck_rpe_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(index=True)
    sport_type: Mapped[str] = mapped_column(String(50))
    duration_min: Mapped[int]
    rpe: Mapped[int]
    notes: Mapped[str | None]
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())


class Wellness(Base):
    __tablename__ = "wellness"
    __table_args__ = (
        CheckConstraint("sleep_hours BETWEEN 0 AND 24", name="ck_sleep_range"),
        CheckConstraint("soreness BETWEEN 1 AND 10", name="ck_soreness_range"),
        CheckConstraint("fatigue BETWEEN 1 AND 10", name="ck_fatigue_range"),
        CheckConstraint("stress BETWEEN 1 AND 10", name="ck_stress_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # One wellness entry per day.
    date: Mapped[dt.date] = mapped_column(unique=True, index=True)
    sleep_hours: Mapped[float]
    soreness: Mapped[int]
    fatigue: Mapped[int]
    stress: Mapped[int]
    resting_hr: Mapped[int | None]
    notes: Mapped[str | None]


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(index=True)
    flag_type: Mapped[str] = mapped_column(String(50))
    severity: Mapped[str] = mapped_column(String(10))  # low / medium / high
    message: Mapped[str]
    sent: Mapped[bool] = mapped_column(default=False)


class PendingAction(Base):
    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    action_type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(10), default="proposed")  # proposed / confirmed / rejected
