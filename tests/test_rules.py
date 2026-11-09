from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from src.import_csv import import_sessions, import_wellness
from src.metrics import daily_loads
from src.models import TrainingSession, Wellness
from src.rules import (
    acwr_flag,
    evaluate_flags,
    high_soreness_flag,
    monotony_flag,
    poor_sleep_flag,
    weeks_with_sessions,
)

AS_OF = date(2026, 2, 28)
DATA = Path(__file__).resolve().parent.parent / "data"


@dataclass
class W:
    sleep_hours: float = 7.5
    soreness: int = 3


def days_before(n: int) -> date:
    return AS_OF - timedelta(days=n)


def flag_type(flag) -> str | None:
    return None if flag is None else flag.flag_type


# ---------- ACWR bands, tested at the exact edges ----------

@pytest.mark.parametrize("value, expected", [
    (None, None),               # not enough history: no flag
    (1.51, "high_load_spike"),
    (1.5, "elevated_load"),     # exactly 1.5 is NOT a spike (> 1.5)
    (1.5049, "elevated_load"),  # rounds to 1.50, so message and flag agree
    (1.3, "elevated_load"),     # exactly 1.3 IS elevated (>= 1.3)
    (1.2999, "elevated_load"),  # rounds to 1.30
    (1.29, None),
    (1.0, None),
    (0.8, None),                # exactly 0.8 is fine (< 0.8 is undertraining)
    (0.79, "undertraining"),
])
def test_acwr_flag_bands(value, expected):
    assert flag_type(acwr_flag(value)) == expected


def test_acwr_flag_severity_and_message_quote_the_value():
    flag = acwr_flag(1.6218)
    assert flag.severity == "high"
    assert "1.62" in flag.message and "1.5" in flag.message
    assert acwr_flag(1.4).severity == "medium"
    assert acwr_flag(0.5).severity == "low"


# ---------- monotony ----------

@pytest.mark.parametrize("value, expected", [
    (None, None),
    (2.0, None),       # exactly 2.0 is not above 2.0
    (2.004, None),     # rounds to 2.00
    (2.01, "high_monotony"),
])
def test_monotony_flag_threshold(value, expected):
    assert flag_type(monotony_flag(value)) == expected


def test_monotony_flag_is_medium_and_quotes_value():
    flag = monotony_flag(2.346)
    assert flag.severity == "medium"
    assert "2.35" in flag.message


# ---------- poor sleep: < 6 h on 3 of the last 4 days ----------

def test_poor_sleep_three_of_four_nights():
    wellness = {days_before(0): W(5.5), days_before(1): W(7.0), days_before(2): W(5.0), days_before(3): W(5.9)}
    flag = poor_sleep_flag(wellness, AS_OF)
    assert flag.flag_type == "poor_sleep" and flag.severity == "medium"
    assert "5.5 h" in flag.message


def test_poor_sleep_exactly_six_hours_is_not_poor():
    wellness = {days_before(0): W(5.5), days_before(1): W(6.0), days_before(2): W(5.0), days_before(3): W(7.0)}
    assert poor_sleep_flag(wellness, AS_OF) is None


def test_poor_sleep_missing_days_do_not_count_as_poor():
    wellness = {days_before(0): W(5.0), days_before(1): W(5.0)}  # 2 poor nights, 2 missing
    assert poor_sleep_flag(wellness, AS_OF) is None


def test_poor_sleep_ignores_nights_older_than_4_days():
    wellness = {days_before(0): W(5.0), days_before(1): W(5.0), days_before(4): W(4.0), days_before(5): W(4.0)}
    assert poor_sleep_flag(wellness, AS_OF) is None


# ---------- high soreness: >= 7 on 2 days in a row ----------

@pytest.mark.parametrize("yesterday, today, expected", [
    (7, 7, "high_soreness"),
    (8, 9, "high_soreness"),
    (6, 7, None),
    (7, 6, None),
])
def test_high_soreness_two_days_in_a_row(yesterday, today, expected):
    wellness = {days_before(1): W(soreness=yesterday), days_before(0): W(soreness=today)}
    assert flag_type(high_soreness_flag(wellness, AS_OF)) == expected


def test_high_soreness_missing_day_breaks_the_streak():
    wellness = {days_before(2): W(soreness=8), days_before(0): W(soreness=8)}  # yesterday missing
    assert high_soreness_flag(wellness, AS_OF) is None


# ---------- evaluate_flags ----------

def test_evaluate_flags_quiet_week_has_no_flags():
    daily = {days_before(i): (300 if i % 3 else 0) for i in range(28)}
    wellness = {days_before(i): W() for i in range(4)}
    _, flags = evaluate_flags(daily, wellness, AS_OF, history_start=days_before(40))
    assert flags == []


def test_evaluate_flags_without_enough_history_skips_acwr_but_checks_wellness():
    daily = {days_before(i): 500 for i in range(7)}
    wellness = {days_before(0): W(soreness=8), days_before(1): W(soreness=8)}
    metrics, flags = evaluate_flags(daily, wellness, AS_OF, history_start=days_before(10))
    assert metrics.acwr is None
    assert [f.flag_type for f in flags] == ["high_soreness"]


# ---------- data coverage: weeks of the ACWR window with logged sessions ----------

@pytest.mark.parametrize("days_with_load, expected", [
    ([], 0),
    ([0, 7, 14, 21], 4),       # one session in each 7-day block
    ([0, 8, 27], 3),           # blocks 0, 1 and 3; block 2 (days 14-20) empty
    ([0, 1, 2, 3, 4, 5, 6], 1),  # a busy week after a gap is still only 1 week
    ([28], 0),                 # day 28 is outside the 4 weeks
])
def test_weeks_with_sessions(days_with_load, expected):
    daily = {days_before(n): 300 for n in days_with_load}
    assert weeks_with_sessions(daily, AS_OF) == expected


def test_zero_load_days_are_not_sessions():
    assert weeks_with_sessions({days_before(0): 0}, AS_OF) == 0


# ---------- the real sample data ----------

@pytest.fixture
def sample(sample_db):
    """Inputs for evaluate_flags built from the committed sample data."""
    daily = daily_loads(sample_db.scalars(select(TrainingSession)))
    wellness = {w.date: w for w in sample_db.scalars(select(Wellness))}
    history_start = min(min(daily), min(wellness))
    return daily, wellness, history_start


def flags_on(sample, day: date):
    daily, wellness, history_start = sample
    return evaluate_flags(daily, wellness, day, history_start)


def test_sample_week6_spike_triggers_high_load_flag(sample):
    metrics, flags = flags_on(sample, date(2026, 2, 15))
    types = {f.flag_type for f in flags}
    assert metrics.acwr == pytest.approx(1.69, abs=0.01)
    assert "high_load_spike" in types
    assert {"poor_sleep", "high_soreness"} <= types


def test_sample_normal_period_has_no_flags(sample):
    metrics, flags = flags_on(sample, date(2026, 2, 1))  # day 28: first day ACWR exists
    assert metrics.acwr is not None
    assert flags == []


def test_sample_acwr_not_available_before_28_days(sample):
    metrics, _ = flags_on(sample, date(2026, 1, 31))  # day 27
    assert metrics.acwr is None


def test_sample_deload_after_spike_flags_undertraining(sample):
    # Known limitation, kept on purpose: a sensible easy week after a spike
    # pushes ACWR under 0.8 because the spike inflates the chronic load.
    _, flags = flags_on(sample, date(2026, 2, 22))
    assert "undertraining" in {f.flag_type for f in flags}


# ---------- CLI ----------

def test_cli_prints_metrics_and_flags(tmp_path, monkeypatch, capsys):
    from src.db import init_db, make_engine, make_session_factory
    from src.rules import main

    db_file = str(tmp_path / "cli.db")
    engine = make_engine(db_file)
    init_db(engine)
    with make_session_factory(engine)() as db:
        import_sessions(db, DATA / "sample_sessions.csv")
        import_wellness(db, DATA / "sample_wellness.csv")
    engine.dispose()

    monkeypatch.setattr("sys.argv", ["rules", "--db", db_file, "--as-of", "2026-02-15"])
    main()
    out = capsys.readouterr().out
    assert "ACWR 1.69" in out
    assert "high_load_spike" in out


def test_cli_with_empty_database(tmp_path, monkeypatch, capsys):
    from src.db import init_db, make_engine
    from src.rules import main

    db_file = str(tmp_path / "empty.db")
    engine = make_engine(db_file)
    init_db(engine)
    engine.dispose()

    monkeypatch.setattr("sys.argv", ["rules", "--db", db_file])
    main()
    assert "No data" in capsys.readouterr().out
