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


class Food(Base):
    """A food you can search and log. Loaded from data/foods.csv (USDA values or package labels)."""
    __tablename__ = "foods"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kcal_per_100g: Mapped[float]
    protein_per_100g: Mapped[float]
    aliases: Mapped[list] = mapped_column(JSON, default=list)    # ["ei", "eier"]
    portions: Mapped[dict] = mapped_column(JSON, default=dict)   # {"1 large": 50}
    source: Mapped[str]                                          # where the numbers come from


class MealTemplate(Base):
    """A saved meal: several foods with default amounts. Loaded from data/meals.json."""
    __tablename__ = "meal_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    slot: Mapped[str] = mapped_column(String(10))               # default meal slot
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    items: Mapped[list] = mapped_column(JSON)                    # [{"food", "grams" | "portion"+"count", "optional"}]


class FoodLogEntry(Base):
    """One eaten item. kcal and protein are stored when logged (a snapshot), so changing a
    food's values later never rewrites history."""
    __tablename__ = "food_log"
    __table_args__ = (
        CheckConstraint("grams > 0", name="ck_food_grams_positive"),
        CheckConstraint("slot IN ('breakfast', 'lunch', 'dinner', 'snack')", name="ck_food_slot"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(index=True)
    slot: Mapped[str] = mapped_column(String(10))
    food_name: Mapped[str] = mapped_column(String(100))
    grams: Mapped[float]
    kcal: Mapped[float]
    protein: Mapped[float]
    meal_name: Mapped[str | None] = mapped_column(String(100))  # set when logged as part of a saved meal
    group_id: Mapped[str | None] = mapped_column(String(32))    # items logged together share it
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())


class PendingAction(Base):
    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    action_type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(10), default="proposed")  # proposed / confirmed / rejected
