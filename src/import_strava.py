"""Import training history from a Strava bulk export (activities.csv).

Get the file: Strava website -> Settings -> My Account -> "Download or Delete Your Account"
-> "Request your archive". Only activities.csv from the zip is needed.

Usage:
  python -m src.import_strava data/private/activities.csv --db data/loadwise.db

How Strava data becomes LoadWise sessions:
- Activity type -> sport_type via STRAVA_TYPES. Walks are skipped (not training).
  Unknown types are skipped and reported rather than guessed.
- RPE: the athlete's own "Perceived Exertion" when it is set (1-10). Otherwise a default
  per sport, and the note says "RPE estimated", so estimates are always visible.
- Duration: elapsed time (includes rest between sets), matching Foster's session-RPE,
  which rates the whole session.
- Sessions longer than MAX_MINUTES are skipped and listed (forgotten stop button, or not
  training at all). Hikes are exempt: they are genuinely long.
- Date: Strava stores start times in UTC. They are converted to the athlete's time zone
  before taking the calendar date, so a 00:30 local session is not filed under the day before.
- Safe to re-run: duplicates are skipped exactly like the CSV import.

--db is required on purpose: real history must never land in the demo database by accident.
"""

import argparse
import csv
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.config import get_settings
from src.db import init_db, make_engine, make_session_factory
from src.import_csv import add_session_if_new

# Strava "Activity Type" -> (sport_type, default RPE when none was recorded).
# None means: skip this activity type.
STRAVA_TYPES: dict[str, tuple[str, int] | None] = {
    "Workout": ("gym", 6),          # confirmed by the athlete: mostly gym
    "Weight Training": ("gym", 6),
    "Crossfit": ("gym", 6),
    "Run": ("running", 6),
    "Ride": ("cycling", 5),
    "Hike": ("hiking", 4),
    "Football (Soccer)": ("football", 6),
    "Walk": None,
}

MAX_MINUTES = 240
NO_DURATION_LIMIT = {"hiking"}

STRAVA_DATE_FORMAT = "%b %d, %Y, %I:%M:%S %p"  # e.g. "Oct 31, 2026, 10:22:51 PM"


@dataclass
class StravaReport:
    inserted: int = 0
    duplicates: int = 0
    rpe_estimated: int = 0  # among inserted
    skipped: Counter = field(default_factory=Counter)  # reason -> count
    skipped_details: list[str] = field(default_factory=list)
    first_date: date | None = None
    last_date: date | None = None


def strava_local_date(value: str, tz: ZoneInfo) -> date:
    """Strava's UTC start time -> calendar date in the athlete's time zone."""
    # Some exports use a narrow no-break space before AM/PM.
    utc = datetime.strptime(value.replace(" ", " ").strip(), STRAVA_DATE_FORMAT)
    return utc.replace(tzinfo=timezone.utc).astimezone(tz).date()


def parse_rpe(value: str) -> int | None:
    """Strava stores perceived exertion as e.g. "7.0". 0 or empty means not set."""
    try:
        rpe = round(float(value))
    except (TypeError, ValueError):
        return None
    return rpe if 1 <= rpe <= 10 else None


def _column_indexes(header: list[str]) -> dict[str, int]:
    """Index of the FIRST occurrence of each column. Strava repeats some names
    (two "Elapsed Time" columns, two "Distance" columns), so a DictReader would
    silently keep the last one."""
    indexes: dict[str, int] = {}
    for i, name in enumerate(header):
        indexes.setdefault(name.strip(), i)
    return indexes


def strava_row_to_session(row: list[str], col: dict[str, int], tz: ZoneInfo) -> tuple[dict | None, str]:
    """Convert one Strava row. Returns (session data, "") or (None, reason it was skipped)."""
    activity_id = row[col["Activity ID"]].strip()
    strava_type = row[col["Activity Type"]].strip()

    if strava_type not in STRAVA_TYPES:
        return None, f"unknown type '{strava_type}'"
    mapping = STRAVA_TYPES[strava_type]
    if mapping is None:
        return None, f"not training ({strava_type})"
    sport, default_rpe = mapping

    try:
        minutes = round(float(row[col["Elapsed Time"]]) / 60)
        day = strava_local_date(row[col["Activity Date"]], tz)
    except ValueError:
        return None, "unreadable date or duration"
    if minutes < 1:
        return None, "shorter than 1 minute"
    if minutes > MAX_MINUTES and sport not in NO_DURATION_LIMIT:
        return None, f"longer than {MAX_MINUTES} min"

    rpe = parse_rpe(row[col["Perceived Exertion"]]) if "Perceived Exertion" in col else None
    note = f"Strava activity {activity_id}"
    if rpe is None:
        rpe = default_rpe
        note += "; RPE estimated (Strava import)"

    return {"date": day, "sport_type": sport, "duration_min": minutes, "rpe": rpe, "notes": note}, ""


def import_strava(db, path: Path, tz: ZoneInfo) -> StravaReport:
    report = StravaReport()
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        col = _column_indexes(next(reader))
        missing = {"Activity ID", "Activity Date", "Activity Type", "Elapsed Time"} - col.keys()
        if missing:
            raise ValueError(f"{path.name} does not look like a Strava activities.csv (missing {sorted(missing)})")

        for row in reader:
            data, reason = strava_row_to_session(row, col, tz)
            if data is None:
                report.skipped[reason] += 1
                report.skipped_details.append(
                    f"{row[col['Activity Date']]} {row[col['Activity Type']]}: {reason}")
                continue
            if not add_session_if_new(db, data):
                report.duplicates += 1
                continue
            report.inserted += 1
            report.rpe_estimated += "RPE estimated" in data["notes"]
            report.first_date = min(filter(None, [report.first_date, data["date"]]))
            report.last_date = max(filter(None, [report.last_date, data["date"]]))
    db.commit()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a Strava activities.csv export.")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--db", required=True, help="SQLite file, e.g. data/loadwise.db")
    args = parser.parse_args()

    tz = ZoneInfo(get_settings().timezone)
    engine = make_engine(args.db)
    init_db(engine)
    with make_session_factory(engine)() as db:
        report = import_strava(db, args.csv_path, tz)

    print(f"Database: {args.db}  (dates converted from UTC to {tz.key})")
    print(f"Inserted {report.inserted} sessions"
          + (f" from {report.first_date} to {report.last_date}" if report.inserted else "")
          + f"; RPE estimated for {report.rpe_estimated}")
    print(f"Already present: {report.duplicates}")
    print(f"Skipped: {sum(report.skipped.values())}")
    for reason, count in report.skipped.most_common():
        print(f"  {count:4d}  {reason}")
    long_ones = [d for d in report.skipped_details if "longer than" in d]
    if long_ones:
        print("Skipped as too long (check these):")
        for detail in long_ones:
            print(f"  - {detail}")


if __name__ == "__main__":
    main()
