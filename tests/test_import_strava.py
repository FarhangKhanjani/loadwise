"""Tests for the Strava importer. Uses a small fake export, never the athlete's real file."""

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from src.import_strava import import_strava, parse_rpe, strava_local_date
from src.models import TrainingSession

VIENNA = ZoneInfo("Europe/Vienna")

# Real Strava exports repeat some column names. The second "Elapsed Time" here has a
# deliberately different value, to prove the importer reads the first one.
HEADER = "Activity ID,Activity Date,Activity Name,Activity Type,Elapsed Time,Distance,Elapsed Time,Moving Time,Perceived Exertion"


def strava_csv(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "activities.csv"
    path.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def row(activity_id, when, strava_type, seconds, pe=""):
    return f'{activity_id},"{when}",Afternoon Activity,{strava_type},{seconds},0,99999,{seconds},{pe}'


def sessions(db):
    return list(db.scalars(select(TrainingSession).order_by(TrainingSession.date)))


# ---------- date conversion ----------

@pytest.mark.parametrize("utc_text, local_day", [
    ("Mar 1, 2025, 11:30:00 PM", date(2025, 3, 2)),  # winter, UTC+1: 00:30 next day
    ("Mar 1, 2025, 10:30:00 PM", date(2025, 3, 1)),  # 23:30 same day
    ("Jul 1, 2025, 10:30:00 PM", date(2025, 7, 2)),  # summer, UTC+2: 00:30 next day
    ("Jul 1, 2025, 9:30:00 PM", date(2025, 7, 1)),
    ("Jul 1, 2025, 9:30:00 PM", date(2025, 7, 1)),  # narrow no-break space variant
])
def test_strava_utc_time_becomes_local_calendar_date(utc_text, local_day):
    assert strava_local_date(utc_text, VIENNA) == local_day


# ---------- RPE parsing ----------

@pytest.mark.parametrize("value, expected", [("7.0", 7), ("10.0", 10), ("0.0", None), ("", None), ("x", None)])
def test_parse_rpe(value, expected):
    assert parse_rpe(value) == expected


# ---------- full import ----------

def test_maps_types_uses_own_rpe_and_estimates_missing_rpe(db, tmp_path):
    path = strava_csv(
        tmp_path,
        row(1, "Mar 3, 2025, 5:00:00 PM", "Weight Training", 5400, "8.0"),  # own RPE
        row(2, "Mar 4, 2025, 5:00:00 PM", "Workout", 4800),                 # gym, estimated
        row(3, "Mar 5, 2025, 5:00:00 PM", "Run", 2700, "0.0"),              # 0 = not set
    )
    report = import_strava(db, path, VIENNA)

    assert report.inserted == 3
    assert report.rpe_estimated == 2
    gym_own, gym_est, run = sessions(db)
    assert (gym_own.sport_type, gym_own.duration_min, gym_own.rpe) == ("gym", 90, 8)
    assert "estimated" not in gym_own.notes and "Strava activity 1" in gym_own.notes
    assert (gym_est.sport_type, gym_est.duration_min, gym_est.rpe) == ("gym", 80, 6)
    assert "RPE estimated" in gym_est.notes
    assert (run.sport_type, run.rpe) == ("running", 6)


def test_uses_first_elapsed_time_column(db, tmp_path):
    import_strava(db, strava_csv(tmp_path, row(1, "Mar 3, 2025, 5:00:00 PM", "Run", 1800)), VIENNA)
    assert sessions(db)[0].duration_min == 30  # not 99999 s from the duplicate column


def test_skips_walks_unknown_types_and_overlong_sessions_but_keeps_long_hikes(db, tmp_path):
    path = strava_csv(
        tmp_path,
        row(1, "Mar 3, 2025, 5:00:00 PM", "Walk", 3600),
        row(2, "Mar 4, 2025, 5:00:00 PM", "Kitesurf", 3600),
        row(3, "Oct 31, 2026, 10:22:51 PM", "Workout", 324 * 60),  # forgotten stop / not training
        row(4, "Mar 6, 2025, 8:00:00 AM", "Hike", 314 * 60),
        row(5, "Mar 7, 2025, 8:00:00 AM", "Run", 20),               # under a minute
    )
    report = import_strava(db, path, VIENNA)

    assert report.inserted == 1
    assert sessions(db)[0].sport_type == "hiking"
    assert sessions(db)[0].duration_min == 314
    assert report.skipped == {
        "not training (Walk)": 1,
        "unknown type 'Kitesurf'": 1,
        "longer than 240 min": 1,
        "shorter than 1 minute": 1,
    }


def test_unreadable_rows_are_skipped_not_fatal(db, tmp_path):
    path = strava_csv(
        tmp_path,
        row(1, "yesterday-ish", "Run", 1800),
        row(2, "Mar 3, 2025, 5:00:00 PM", "Run", "abc"),
        row(3, "Mar 4, 2025, 5:00:00 PM", "Run", 1800),
    )
    report = import_strava(db, path, VIENNA)
    assert report.inserted == 1
    assert report.skipped == {"unreadable date or duration": 2}


def test_reimport_adds_nothing(db, tmp_path):
    path = strava_csv(tmp_path, row(1, "Mar 3, 2025, 5:00:00 PM", "Run", 2700, "6.0"))
    import_strava(db, path, VIENNA)
    second = import_strava(db, path, VIENNA)
    assert (second.inserted, second.duplicates) == (0, 1)
    assert len(sessions(db)) == 1


def test_report_tracks_date_range(db, tmp_path):
    path = strava_csv(
        tmp_path,
        row(1, "Mar 9, 2025, 5:00:00 PM", "Run", 1800),
        row(2, "Mar 3, 2025, 5:00:00 PM", "Run", 1800),
    )
    report = import_strava(db, path, VIENNA)
    assert (report.first_date, report.last_date) == (date(2025, 3, 3), date(2025, 3, 9))


def test_rejects_a_file_that_is_not_a_strava_export(db, tmp_path):
    path = tmp_path / "other.csv"
    path.write_text("date,sport_type,duration_min,rpe,notes\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Strava"):
        import_strava(db, path, VIENNA)
