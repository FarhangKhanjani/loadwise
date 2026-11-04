"""Risk flag rules. Pure functions on top of src/metrics.py: no database, no LLM.

These are monitoring signals, not predictions or diagnoses.

Rounding: ACWR and monotony are rounded to 2 decimals BEFORE comparing to thresholds,
so the number shown in a message always agrees with the flag. Without this, a raw ACWR
of 1.5004 would display as "1.50" but still be flagged as "above 1.5".

Usage (inspect a day in a database):
  python -m src.rules --db data/demo.db --as-of 2026-02-15
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, Protocol

from src.metrics import LoadMetrics, compute_load_metrics

# ACWR bands
ACWR_HIGH = 1.5         # > 1.5           -> high load spike (high)
ACWR_ELEVATED = 1.3     # 1.3 <= x <= 1.5 -> elevated load (medium)
ACWR_LOW = 0.8          # < 0.8           -> undertraining (low)

MONOTONY_HIGH = 2.0     # > 2.0           -> high monotony (medium)

POOR_SLEEP_HOURS = 6.0  # sleep < 6.0 h counts as a poor night
POOR_SLEEP_WINDOW = 4   # ... on at least 3 of the last 4 days
POOR_SLEEP_MIN_NIGHTS = 3

HIGH_SORENESS = 7       # soreness >= 7 ...
SORENESS_STREAK = 2     # ... on 2 days in a row (as_of and the day before)


class WellnessLike(Protocol):
    sleep_hours: float
    soreness: int


@dataclass(frozen=True)
class Flag:
    flag_type: str
    severity: str  # low / medium / high
    message: str


def acwr_flag(acwr: float | None) -> Flag | None:
    if acwr is None:
        return None  # not enough history: say nothing rather than guess
    value = round(acwr, 2)
    if value > ACWR_HIGH:
        return Flag("high_load_spike", "high",
                    f"ACWR is {value:.2f}, above the {ACWR_HIGH} threshold: this week's load is much higher than usual.")
    if value >= ACWR_ELEVATED:
        return Flag("elevated_load", "medium",
                    f"ACWR is {value:.2f}, in the elevated {ACWR_ELEVATED}-{ACWR_HIGH} range.")
    if value < ACWR_LOW:
        return Flag("undertraining", "low",
                    f"ACWR is {value:.2f}, below {ACWR_LOW}: this week's load is well under your usual.")
    return None


def monotony_flag(monotony: float | None) -> Flag | None:
    if monotony is None:
        return None
    value = round(monotony, 2)
    if value > MONOTONY_HIGH:
        return Flag("high_monotony", "medium",
                    f"Training monotony is {value:.2f}, above {MONOTONY_HIGH}: daily loads this week are very similar.")
    return None


def poor_sleep_flag(wellness: Mapping[date, WellnessLike], as_of: date) -> Flag | None:
    """Sleep < 6 h on at least 3 of the last 4 days. A day with no entry is not counted as poor."""
    days = [as_of - timedelta(days=offset) for offset in range(POOR_SLEEP_WINDOW)]
    short_nights = [d for d in days if d in wellness and wellness[d].sleep_hours < POOR_SLEEP_HOURS]
    if len(short_nights) >= POOR_SLEEP_MIN_NIGHTS:
        hours = ", ".join(f"{wellness[d].sleep_hours:g} h" for d in sorted(short_nights))
        return Flag("poor_sleep", "medium",
                    f"Sleep under {POOR_SLEEP_HOURS:g} h on {len(short_nights)} of the last "
                    f"{POOR_SLEEP_WINDOW} days ({hours}).")
    return None


def high_soreness_flag(wellness: Mapping[date, WellnessLike], as_of: date) -> Flag | None:
    """Soreness >= 7 on as_of and the day before. A missing day breaks the streak."""
    days = [as_of - timedelta(days=offset) for offset in range(SORENESS_STREAK - 1, -1, -1)]
    if all(d in wellness and wellness[d].soreness >= HIGH_SORENESS for d in days):
        values = ", ".join(str(wellness[d].soreness) for d in days)
        return Flag("high_soreness", "medium",
                    f"Soreness at or above {HIGH_SORENESS} for {SORENESS_STREAK} days in a row ({values}).")
    return None


def evaluate_flags(
    daily: Mapping[date, int],
    wellness: Mapping[date, WellnessLike],
    as_of: date,
    history_start: date,
) -> tuple[LoadMetrics, list[Flag]]:
    """Compute metrics for as_of and return them together with every active flag."""
    metrics = compute_load_metrics(dict(daily), as_of, history_start)
    candidates = [
        acwr_flag(metrics.acwr),
        monotony_flag(metrics.monotony),
        poor_sleep_flag(wellness, as_of),
        high_soreness_flag(wellness, as_of),
    ]
    return metrics, [f for f in candidates if f is not None]


def main() -> None:
    import argparse

    from sqlalchemy import func, select

    from src.db import make_engine, make_session_factory
    from src.metrics import daily_loads
    from src.models import TrainingSession, Wellness

    parser = argparse.ArgumentParser(description="Show load metrics and active flags for one day.")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--db", help="SQLite file (default: DB_PATH from .env)")
    args = parser.parse_args()

    with make_session_factory(make_engine(args.db))() as db:
        starts = [db.scalar(select(func.min(TrainingSession.date))), db.scalar(select(func.min(Wellness.date)))]
        starts = [d for d in starts if d is not None]
        if not starts:
            print("No data in the database.")
            return
        daily = daily_loads(db.scalars(select(TrainingSession).where(TrainingSession.date <= args.as_of)))
        wellness = {w.date: w for w in db.scalars(select(Wellness).where(Wellness.date <= args.as_of))}
        metrics, flags = evaluate_flags(daily, wellness, args.as_of, min(starts))

    def fmt(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2f}"

    print(f"As of {metrics.as_of} (history since {min(starts)})")
    print(f"  acute {metrics.acute}  chronic {metrics.chronic:.0f}  ACWR {fmt(metrics.acwr)}  "
          f"monotony {fmt(metrics.monotony)}  strain {fmt(metrics.strain)}")
    print(f"Flags ({len(flags)}):" if flags else "No flags.")
    for f in flags:
        print(f"  [{f.severity}] {f.flag_type}: {f.message}")


if __name__ == "__main__":
    main()
