from datetime import date
from pathlib import Path

import pytest

from src.import_csv import (
    import_sessions,
    import_wellness,
    parse_session_row,
    parse_wellness_row,
    weekly_load_summary,
)

SESSIONS_CSV = """date,sport_type,duration_min,rpe,notes
2026-01-05,running,50,6,easy
2026-01-06,Strength,60,7,
2026-01-12,running,40,8,intervals
"""

WELLNESS_CSV = """date,sleep_hours,soreness,fatigue,stress,resting_hr,notes
2026-01-05,7.5,3,4,3,52,
2026-01-06,6.0,5,5,4,,no watch
"""


def write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


# ---------- import behaviour ----------

def test_sessions_import_and_reimport_adds_no_duplicates(db, tmp_path):
    path = write(tmp_path, "s.csv", SESSIONS_CSV)

    first = import_sessions(db, path)
    second = import_sessions(db, path)

    assert (first.inserted, first.skipped, first.errors) == (3, 0, [])
    assert (second.inserted, second.skipped, second.errors) == (0, 3, [])


def test_wellness_import_and_reimport_adds_no_duplicates(db, tmp_path):
    path = write(tmp_path, "w.csv", WELLNESS_CSV)

    first = import_wellness(db, path)
    second = import_wellness(db, path)

    assert (first.inserted, first.skipped) == (2, 0)
    assert (second.inserted, second.skipped) == (0, 2)


def test_duplicate_rows_inside_one_file_are_skipped(db, tmp_path):
    content = "date,sport_type,duration_min,rpe,notes\n2026-01-05,running,50,6,\n2026-01-05,running,50,6,\n"
    report = import_sessions(db, write(tmp_path, "s.csv", content))
    assert (report.inserted, report.skipped) == (1, 1)


def test_bad_rows_are_rejected_with_line_numbers_and_good_rows_still_import(db, tmp_path):
    content = (
        "date,sport_type,duration_min,rpe,notes\n"
        "2026-01-05,running,50,6,\n"      # line 2: ok
        "2026-01-06,running,50,11,\n"     # line 3: rpe too high
        "not-a-date,running,50,6,\n"      # line 4: bad date
        "2026-01-07,,50,6,\n"             # line 5: missing sport
    )
    report = import_sessions(db, write(tmp_path, "s.csv", content))

    assert report.inserted == 1
    assert len(report.errors) == 3
    assert "line 3" in report.errors[0] and "rpe" in report.errors[0]
    assert "line 4" in report.errors[1]
    assert "line 5" in report.errors[2]


# ---------- parsing ----------

def test_parse_session_row_normalises_values():
    row = {"date": "2026-01-06", "sport_type": " Strength ", "duration_min": "60", "rpe": "7", "notes": ""}
    assert parse_session_row(row) == {
        "date": date(2026, 1, 6), "sport_type": "strength", "duration_min": 60, "rpe": 7, "notes": None,
    }


@pytest.mark.parametrize("field, value", [("duration_min", "0"), ("rpe", "0"), ("rpe", "7.5")])
def test_parse_session_row_rejects_out_of_range(field, value):
    row = {"date": "2026-01-06", "sport_type": "running", "duration_min": "60", "rpe": "7", "notes": ""}
    row[field] = value
    with pytest.raises(ValueError):
        parse_session_row(row)


def test_parse_wellness_row_resting_hr_is_optional():
    row = {"date": "2026-01-06", "sleep_hours": "6.5", "soreness": "5", "fatigue": "5",
           "stress": "4", "resting_hr": "", "notes": ""}
    parsed = parse_wellness_row(row)
    assert parsed["resting_hr"] is None
    assert parsed["sleep_hours"] == 6.5


@pytest.mark.parametrize("field, value", [("sleep_hours", "25"), ("soreness", "11"), ("stress", "abc")])
def test_parse_wellness_row_rejects_bad_values(field, value):
    row = {"date": "2026-01-06", "sleep_hours": "7", "soreness": "5", "fatigue": "5",
           "stress": "4", "resting_hr": "", "notes": ""}
    row[field] = value
    with pytest.raises(ValueError):
        parse_wellness_row(row)


# ---------- weekly summary (a calculation, so it gets a test) ----------

def test_weekly_load_summary_groups_by_monday_and_sums_rpe_times_duration(db, tmp_path):
    import_sessions(db, write(tmp_path, "s.csv", SESSIONS_CSV))

    assert weekly_load_summary(db) == [
        (date(2026, 1, 5), 2, 50 * 6 + 60 * 7),   # Mon 5 + Tue 6 Jan
        (date(2026, 1, 12), 1, 40 * 8),
    ]
