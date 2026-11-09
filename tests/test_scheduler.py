import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from src.models import Alert, TrainingSession
from src.scheduler import (
    COOLDOWN_DAYS,
    NO_DATA,
    alert_candidates,
    daily_check,
    format_alert_message,
    run_check,
    send_unsent,
)
from src.tools import ToolContext, get_active_flags

SPIKE = date(2026, 2, 15)  # sample data: high_load_spike, high_monotony, poor_sleep, high_soreness
SPIKE_FLAGS = {"high_load_spike", "high_monotony", "poor_sleep", "high_soreness"}


def alert_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Alert))


def flag(flag_type, severity="medium"):
    return {"flag_type": flag_type, "severity": severity, "message": f"{flag_type} message"}


def status(flags, weeks=4, days=0, last="2026-02-15"):
    """A get_active_flags()-shaped result."""
    return {"flags": flags, "weeks_with_sessions_28d": weeks, "days_since_last_session": days,
            "last_session_date": last}


class Recorder:
    """An async stand-in for bot.send_message that records the texts."""
    def __init__(self, fail=False):
        self.texts, self.fail = [], fail

    async def __call__(self, text):
        if self.fail:
            raise ConnectionError("Telegram unreachable")
        self.texts.append(text)


# ---------- which flags become alerts ----------

def test_all_flags_alert_with_full_coverage():
    candidates = alert_candidates(status([flag("high_load_spike", "high"), flag("poor_sleep")]))
    assert [c["flag_type"] for c in candidates] == ["high_load_spike", "poor_sleep"]


@pytest.mark.parametrize("weeks", [0, 1, 3])
def test_load_flags_are_held_back_when_a_week_has_no_sessions(weeks):
    flags = [flag("high_load_spike", "high"), flag("high_monotony"), flag("high_soreness")]
    candidates = alert_candidates(status(flags, weeks=weeks))
    assert [c["flag_type"] for c in candidates] == ["high_soreness"]  # wellness flags still alert


def test_logging_gap_replaces_undertraining_with_a_reminder():
    candidates = alert_candidates(status([flag("undertraining", "low")], days=4, last="2026-02-11"))
    assert [c["flag_type"] for c in candidates] == [NO_DATA]
    assert "No session logged since 2026-02-11 (4 days)" in candidates[0]["message"]


def test_two_days_without_a_session_is_not_a_gap():
    candidates = alert_candidates(status([flag("undertraining", "low")], days=2))
    assert [c["flag_type"] for c in candidates] == ["undertraining"]


def test_empty_database_gives_no_alerts(db):
    assert run_check(db, SPIKE) == []


# ---------- run_check: never the same alert twice ----------

def test_spike_day_stores_one_unsent_alert_per_flag(sample_db):
    new = run_check(sample_db, SPIKE)
    assert {a.flag_type for a in new} == SPIKE_FLAGS
    assert all(a.date == SPIKE and a.sent is False for a in new)
    assert alert_count(sample_db) == 4


def test_running_the_check_again_creates_nothing(sample_db):
    run_check(sample_db, SPIKE)
    assert run_check(sample_db, SPIKE) == []
    assert alert_count(sample_db) == 4


def test_flags_that_stay_active_are_not_alerted_again_next_day(sample_db):
    run_check(sample_db, SPIKE)
    next_day = run_check(sample_db, SPIKE + timedelta(days=1))
    still_active = {f["flag_type"] for f in get_active_flags(ToolContext(sample_db, SPIKE + timedelta(days=1)))["flags"]}
    assert still_active & SPIKE_FLAGS                     # some flags are still active...
    assert not {a.flag_type for a in next_day} & SPIKE_FLAGS  # ...but none is alerted again


@pytest.mark.parametrize("previous_type, days_ago, alerted", [
    ("high_load_spike", COOLDOWN_DAYS - 1, False),  # inside the cooldown
    ("high_load_spike", COOLDOWN_DAYS, True),       # cooldown over: alert again
    ("elevated_load", 1, True),                     # escalation is a different flag type
])
def test_cooldown_per_flag_type(sample_db, previous_type, days_ago, alerted):
    sample_db.add(Alert(date=SPIKE - timedelta(days=days_ago), flag_type=previous_type, severity="high",
                        message="earlier alert", sent=True))
    sample_db.commit()
    new = run_check(sample_db, SPIKE)
    assert ("high_load_spike" in {a.flag_type for a in new}) is alerted


def test_dry_run_stores_nothing(sample_db):
    assert len(run_check(sample_db, SPIKE, store=False)) == 4
    assert alert_count(sample_db) == 0


def test_one_session_after_a_long_gap_is_not_a_spike_alert(db):
    # The real history in November 2026: last logs in July, then one gym session.
    db.add_all([TrainingSession(date=date(2026, 7, 26), sport_type="running", duration_min=45, rpe=6),
                TrainingSession(date=date(2026, 11, 8), sport_type="gym", duration_min=60, rpe=7)])
    db.commit()
    today = date(2026, 11, 9)
    flags = get_active_flags(ToolContext(db, today))
    assert [f["flag_type"] for f in flags["flags"]] == ["high_load_spike"]  # ACWR 4.0 from missing data
    assert flags["weeks_with_sessions_28d"] == 1
    assert run_check(db, today) == []  # ...but no alert


def test_one_reminder_per_logging_gap(sample_db):
    # Sample data ends 2026-03-05.
    first = run_check(sample_db, date(2026, 3, 10))
    assert NO_DATA in {a.flag_type for a in first}
    assert "undertraining" not in {a.flag_type for a in first}

    later = run_check(sample_db, date(2026, 3, 20))  # same gap, cooldown over: still no second reminder
    assert NO_DATA not in {a.flag_type for a in later}

    sample_db.add(TrainingSession(date=date(2026, 3, 21), sport_type="gym", duration_min=60, rpe=6))
    sample_db.commit()
    new_gap = run_check(sample_db, date(2026, 3, 25))  # a new gap after a new session
    assert NO_DATA in {a.flag_type for a in new_gap}


# ---------- sending ----------

def test_message_format():
    alerts = [Alert(flag_type="high_load_spike", severity="high", message="ACWR is 1.69."),
              Alert(flag_type=NO_DATA, severity="low", message="No session logged.")]
    assert format_alert_message(alerts, SPIKE) == (
        "LoadWise daily check · Sun 15 Feb\n"
        "🔴 ACWR is 1.69.\n"
        "📝 No session logged.\n"
        "/status for details. Monitoring only, not medical advice.")


def test_flags_produce_exactly_one_telegram_message(sample_db):
    send = Recorder()
    for _ in range(3):  # three runs on the same day, e.g. startup + 08:00 + a restart
        run_check(sample_db, SPIKE)
        asyncio.run(send_unsent(sample_db, send))

    assert len(send.texts) == 1
    assert all(f in send.texts[0] for f in ("ACWR is 1.69", "Sleep under 6 h", "Soreness"))
    assert all(a.sent for a in sample_db.scalars(select(Alert)))


def test_failed_send_keeps_alerts_for_the_next_run(sample_db):
    run_check(sample_db, SPIKE)
    with pytest.raises(ConnectionError):
        asyncio.run(send_unsent(sample_db, Recorder(fail=True)))
    assert not any(a.sent for a in sample_db.scalars(select(Alert)))

    send = Recorder()
    assert asyncio.run(send_unsent(sample_db, send)) == 4
    assert len(send.texts) == 1


def test_unsent_alerts_from_different_days_go_in_separate_messages(sample_db):
    run_check(sample_db, SPIKE)
    run_check(sample_db, date(2026, 3, 10))
    send = Recorder()
    asyncio.run(send_unsent(sample_db, send))
    assert [t.splitlines()[0] for t in send.texts] == ["LoadWise daily check · Sun 15 Feb",
                                                       "LoadWise daily check · Tue 10 Mar"]


# ---------- the scheduled job ----------

def test_daily_check_job_stores_sends_and_records_the_run(bot_state):
    sent = []

    async def send_message(chat_id, text):
        sent.append((chat_id, text))

    application = SimpleNamespace(bot_data={"state": bot_state, "allowed_chat_id": 42})
    context = SimpleNamespace(application=application, bot=SimpleNamespace(send_message=send_message))

    asyncio.run(daily_check(context))
    asyncio.run(daily_check(context))  # second run: nothing new

    assert len(sent) == 1 and sent[0][0] == 42
    assert "ACWR is 1.69" in sent[0][1]
    assert application.bot_data["last_check"]


def test_daily_check_job_survives_a_telegram_failure(bot_state):
    async def send_message(chat_id, text):
        raise ConnectionError("down")

    application = SimpleNamespace(bot_data={"state": bot_state, "allowed_chat_id": 42})
    context = SimpleNamespace(application=application, bot=SimpleNamespace(send_message=send_message))

    asyncio.run(daily_check(context))  # logs the error, does not raise

    with bot_state.session_factory() as db:
        assert alert_count(db) == 4
        assert not any(a.sent for a in db.scalars(select(Alert)))
