import json
from datetime import date

import pytest
from sqlalchemy import func, select

from src.models import PendingAction, TrainingSession, Wellness
from src.tools import (
    TOOLS,
    HANDLERS,
    ToolContext,
    ToolError,
    confirm_action,
    execute_tool,
    get_active_flags,
    get_load_summary,
    get_sessions,
    get_wellness_trend,
    log_session,
    log_wellness,
    propose_plan_change,
    reject_action,
)

SPIKE_DAY = date(2026, 2, 15)


@pytest.fixture
def ctx(sample_db):
    return ToolContext(db=sample_db, today=SPIKE_DAY)


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


# ---------- tool definitions ----------

def test_every_tool_has_a_handler_and_a_strict_schema():
    assert {t["name"] for t in TOOLS} == set(HANDLERS)
    for tool in TOOLS:
        schema = tool["input_schema"]
        assert tool["strict"] is True
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])


# ---------- read tools ----------

def test_load_summary_on_spike_day_has_real_numbers(ctx):
    result = get_load_summary(ctx)  # as_of defaults to ctx.today
    assert result["as_of"] == "2026-02-15"
    assert result["acwr"] == 1.69
    assert result["acute_load_7d"] == 4253
    assert result["chronic_load_weekly_avg_28d"] == 2514
    assert result["monotony_7d"] == 2.08
    assert len(result["daily_loads"]) == 7
    assert result["daily_loads"][-1] == {"date": "2026-02-15", "load": 805}
    assert result["days_since_last_session"] == 0
    assert result["note"] is None


def test_load_summary_explains_missing_acwr_with_short_history(ctx):
    result = get_load_summary(ctx, as_of="2026-01-20")
    assert result["acwr"] is None
    assert "28 days" in result["note"]


def test_load_summary_reports_stale_data(ctx):
    result = get_load_summary(ctx, as_of="2026-04-30")  # sample data ends 2026-03-05
    assert result["acwr"] is None
    assert result["last_session_date"] == "2026-03-05"
    assert result["days_since_last_session"] == 56
    assert "no training logged" in result["note"]


def test_load_summary_on_empty_database(db):
    result = get_load_summary(ToolContext(db=db, today=SPIKE_DAY))
    assert result["acwr"] is None and result["note"] == "No data logged yet."
    assert result["last_session_date"] is None


def test_load_summary_rejects_bad_days(ctx):
    with pytest.raises(ToolError):
        get_load_summary(ctx, days=0)


def test_get_sessions_lists_sessions_with_load(ctx):
    result = get_sessions(ctx, "2026-02-09", "2026-02-15")
    assert result["count"] == 8
    assert sum(s["load"] for s in result["sessions"]) == 4253
    assert not result["truncated"]


@pytest.mark.parametrize("start, end", [("2026-02-15", "2026-02-09"), ("2024-01-01", "2026-01-01"), ("feb", "2026-02-09")])
def test_get_sessions_rejects_bad_ranges(ctx, start, end):
    with pytest.raises(ToolError):
        get_sessions(ctx, start, end)


def test_wellness_trend_averages_and_missing_days(ctx):
    result = get_wellness_trend(ctx, days=4)
    assert result["days_logged"] == 4 and result["days_missing"] == 0
    assert result["averages"]["sleep_hours"] == round((5.7 + 5.5 + 6.0 + 5.6) / 4, 1)


def test_wellness_trend_counts_days_not_logged(ctx):
    result = get_wellness_trend(ctx, days=7, as_of="2026-03-10")  # data ends 2026-03-05
    assert result["days_missing"] == 5


def test_active_flags_on_spike_day(ctx):
    result = get_active_flags(ctx)
    assert {f["flag_type"] for f in result["flags"]} == {
        "high_load_spike", "high_monotony", "poor_sleep", "high_soreness"}
    assert result["acwr"] == 1.69


def test_active_flags_on_empty_database(db):
    assert get_active_flags(ToolContext(db=db, today=SPIKE_DAY))["flags"] == []


# ---------- proposals and confirmation ----------

def test_log_session_only_proposes_until_confirmed(ctx):
    sessions_before = count(ctx.db, TrainingSession)
    result = log_session(ctx, "2026-02-15", "padel", 75, 6, "evening game")

    assert result["status"] == "proposed"
    assert "padel, 75 min, RPE 6" in result["summary"]
    assert count(ctx.db, TrainingSession) == sessions_before  # nothing saved yet

    assert confirm_action(ctx.db, result["pending_action_id"]) == "confirmed"
    assert count(ctx.db, TrainingSession) == sessions_before + 1
    assert ctx.db.get(PendingAction, result["pending_action_id"]).status == "confirmed"


def test_rejected_proposal_saves_nothing(ctx):
    sessions_before = count(ctx.db, TrainingSession)
    action_id = log_session(ctx, "2026-02-15", "gym", 60, 7)["pending_action_id"]
    reject_action(ctx.db, action_id)
    assert count(ctx.db, TrainingSession) == sessions_before
    assert ctx.db.get(PendingAction, action_id).status == "rejected"


def test_an_action_cannot_be_confirmed_twice(ctx):
    action_id = log_session(ctx, "2026-02-15", "gym", 60, 7)["pending_action_id"]
    confirm_action(ctx.db, action_id)
    with pytest.raises(ToolError, match="already confirmed"):
        confirm_action(ctx.db, action_id)
    with pytest.raises(ToolError, match="does not exist"):
        reject_action(ctx.db, 9999)


def test_confirming_a_duplicate_session_adds_nothing(ctx):
    existing = get_sessions(ctx, "2026-02-15", "2026-02-15")["sessions"][0]
    action_id = log_session(ctx, "2026-02-15", "running", existing["duration_min"], existing["rpe"])["pending_action_id"]
    before = count(ctx.db, TrainingSession)
    assert "already existed" in confirm_action(ctx.db, action_id)
    assert count(ctx.db, TrainingSession) == before


@pytest.mark.parametrize("kwargs", [
    {"date": "2026-02-16"},          # future relative to today
    {"sport_type": "chess"},
    {"duration_min": 0},
    {"rpe": 11},
])
def test_log_session_validation(ctx, kwargs):
    args = {"date": "2026-02-15", "sport_type": "gym", "duration_min": 60, "rpe": 7, **kwargs}
    with pytest.raises(ToolError):
        log_session(ctx, **args)


def test_log_wellness_proposes_and_confirm_saves(ctx):
    ctx.today = date(2026, 3, 10)
    result = log_wellness(ctx, "2026-03-10", 7.5, 3, 4, 3, resting_hr=52)
    assert "sleep 7.5 h" in result["summary"]
    confirm_action(ctx.db, result["pending_action_id"])
    assert ctx.db.scalar(select(Wellness.sleep_hours).where(Wellness.date == date(2026, 3, 10))) == 7.5


@pytest.mark.parametrize("kwargs, message", [
    ({"date": "2026-02-15"}, "already logged"),
    ({"sleep_hours": 25}, "sleep_hours"),
    ({"soreness": 0}, "soreness"),
    ({"resting_hr": 10}, "resting_hr"),
])
def test_log_wellness_validation(ctx, kwargs, message):
    args = {"date": "2026-03-10", "sleep_hours": 7.0, "soreness": 3, "fatigue": 3, "stress": 3, **kwargs}
    ctx.today = date(2026, 3, 10)
    with pytest.raises(ToolError, match=message):
        log_wellness(ctx, **args)


def test_plan_change_is_only_recorded(ctx):
    sessions_before = count(ctx.db, TrainingSession)
    result = propose_plan_change(ctx, "2026-02-16", "Easy 30 min spin instead of intervals",
                                 "ACWR 1.69 is above 1.5")
    assert "Plan change for 2026-02-16" in result["summary"]
    confirm_action(ctx.db, result["pending_action_id"])
    assert count(ctx.db, TrainingSession) == sessions_before
    with pytest.raises(ToolError):
        propose_plan_change(ctx, "2026-02-16", " ", "reason")


# ---------- execute_tool: errors go back to the model, never raise ----------

def test_execute_tool_returns_json(ctx):
    output, is_error = execute_tool(ctx, "get_load_summary", {"days": 7})
    assert not is_error and json.loads(output)["acwr"] == 1.69


@pytest.mark.parametrize("name, tool_input, message", [
    ("does_not_exist", {}, "unknown tool"),
    ("log_session", {"date": "2026-02-15", "sport_type": "gym", "duration_min": 60, "rpe": 99}, "rpe"),
    ("get_sessions", {"start_date": "2026-02-01"}, "invalid arguments"),
])
def test_execute_tool_errors(ctx, name, tool_input, message):
    output, is_error = execute_tool(ctx, name, tool_input)
    assert is_error
    assert message in json.loads(output)["error"]
