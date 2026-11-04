"""Import training sessions and wellness entries from CSV files.

Safe to re-run: rows that already exist are skipped, never overwritten.
Invalid rows are rejected with a line number and reason; valid rows still import.

Usage:
  python -m src.import_csv --sessions data/sample_sessions.csv --wellness data/sample_wellness.csv --db data/demo.db

Expected columns:
  sessions: date, sport_type, duration_min, rpe, notes
  wellness: date, sleep_hours, soreness, fatigue, stress, resting_hr, notes
"""

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db import init_db, make_engine, make_session_factory
from src.models import TrainingSession, Wellness


@dataclass
class ImportReport:
    inserted: int = 0
    skipped: int = 0  # already in the database (or repeated in the same file)
    errors: list[str] = field(default_factory=list)


# ---------- row parsing and validation ----------

def _int_in_range(value: str, name: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    if not low <= number <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {number}")
    return number


def _optional_text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def parse_session_row(row: dict) -> dict:
    sport = (row.get("sport_type") or "").strip().lower()
    if not sport:
        raise ValueError("sport_type is empty")
    return {
        "date": date.fromisoformat((row.get("date") or "").strip()),
        "sport_type": sport,
        "duration_min": _int_in_range(row.get("duration_min"), "duration_min", 1, 1440),
        "rpe": _int_in_range(row.get("rpe"), "rpe", 1, 10),
        "notes": _optional_text(row.get("notes")),
    }


def parse_wellness_row(row: dict) -> dict:
    try:
        sleep = float(row.get("sleep_hours"))
    except (TypeError, ValueError):
        raise ValueError(f"sleep_hours must be a number, got {row.get('sleep_hours')!r}")
    if not 0 <= sleep <= 24:
        raise ValueError(f"sleep_hours must be between 0 and 24, got {sleep}")

    resting_hr_raw = (row.get("resting_hr") or "").strip()
    return {
        "date": date.fromisoformat((row.get("date") or "").strip()),
        "sleep_hours": sleep,
        "soreness": _int_in_range(row.get("soreness"), "soreness", 1, 10),
        "fatigue": _int_in_range(row.get("fatigue"), "fatigue", 1, 10),
        "stress": _int_in_range(row.get("stress"), "stress", 1, 10),
        "resting_hr": _int_in_range(resting_hr_raw, "resting_hr", 25, 220) if resting_hr_raw else None,
        "notes": _optional_text(row.get("notes")),
    }


# ---------- import ----------

def import_sessions(db: Session, path: Path) -> ImportReport:
    report = ImportReport()
    with path.open(newline="", encoding="utf-8") as f:
        # Line 1 is the header, so data starts at line 2.
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            try:
                data = parse_session_row(row)
            except ValueError as e:
                report.errors.append(f"{path.name} line {line_no}: {e}")
                continue

            exists = db.scalar(
                select(TrainingSession.id).where(
                    TrainingSession.date == data["date"],
                    TrainingSession.sport_type == data["sport_type"],
                    TrainingSession.duration_min == data["duration_min"],
                    TrainingSession.rpe == data["rpe"],
                )
            )
            if exists:
                report.skipped += 1
                continue
            db.add(TrainingSession(**data))
            report.inserted += 1
    db.commit()
    return report


def import_wellness(db: Session, path: Path) -> ImportReport:
    report = ImportReport()
    with path.open(newline="", encoding="utf-8") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            try:
                data = parse_wellness_row(row)
            except ValueError as e:
                report.errors.append(f"{path.name} line {line_no}: {e}")
                continue

            if db.scalar(select(Wellness.id).where(Wellness.date == data["date"])):
                report.skipped += 1
                continue
            db.add(Wellness(**data))
            report.inserted += 1
    db.commit()
    return report


# ---------- summary for a quick visual check ----------

def weekly_load_summary(db: Session) -> list[tuple[date, int, int]]:
    """Return (week_start_monday, session_count, total_load) per week.

    Load is session RPE x duration (Foster). The real metrics live in src/metrics.py
    (Phase 2); this is only a sanity check printed after import.
    """
    weeks: dict[date, list[int]] = defaultdict(lambda: [0, 0])
    for s in db.scalars(select(TrainingSession)):
        monday = s.date - timedelta(days=s.date.weekday())
        weeks[monday][0] += 1
        weeks[monday][1] += s.duration_min * s.rpe
    return [(monday, count, load) for monday, (count, load) in sorted(weeks.items())]


def _print_report(label: str, report: ImportReport) -> None:
    print(f"{label}: {report.inserted} inserted, {report.skipped} skipped (already present), "
          f"{len(report.errors)} rejected")
    for error in report.errors:
        print(f"  - {error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import sessions and wellness CSV files.")
    parser.add_argument("--sessions", type=Path, help="CSV with training sessions")
    parser.add_argument("--wellness", type=Path, help="CSV with daily wellness entries")
    parser.add_argument("--db", help="SQLite file (default: DB_PATH from .env)")
    args = parser.parse_args()
    if not args.sessions and not args.wellness:
        parser.error("give at least one of --sessions or --wellness")

    engine = make_engine(args.db)
    init_db(engine)
    print(f"Database: {engine.url.database}")

    with make_session_factory(engine)() as db:
        if args.sessions:
            _print_report("Sessions", import_sessions(db, args.sessions))
        if args.wellness:
            _print_report("Wellness", import_wellness(db, args.wellness))

        print("\nWeekly load (sessions, load units):")
        for monday, count, load in weekly_load_summary(db):
            print(f"  week of {monday}  {count:2d} sessions  {load:5d}  {'#' * (load // 200)}")


if __name__ == "__main__":
    main()
