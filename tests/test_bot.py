"""Tests for the bot's logic. The Telegram glue (async handlers) is checked by hand."""

from datetime import date

import anthropic
import httpx
import pytest
from sqlalchemy import func, select

from src.agent import Agent
from src.bot import (
    IDLE_RESET_SECONDS,
    BotState,
    CommandError,
    api_error_message,
    build_application,
    callback_data,
    decide,
    is_authorized,
    is_setup_mode,
    markdown_to_html,
    parse_callback,
    parse_day,
    parse_log_args,
    parse_wellness_args,
    propose_from_command,
    run_agent_turn,
    split_message,
    status_text,
)
from src.db import make_engine, make_session_factory
from src.models import PendingAction, TrainingSession, Wellness
from src.tools import ToolContext
from tests.fakes import response, text, tool_use

TODAY = date(2026, 2, 15)  # the spike day in the sample data


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


@pytest.fixture
def ctx(sample_db):
    return ToolContext(db=sample_db, today=TODAY)


# ---------- authorization ----------

@pytest.mark.parametrize("setting, expected", [
    (None, True),
    ("", True),
    ("your-telegram-chat-id", True),  # the .env.example placeholder
    ("123456789", False),
    ("-1001234567890", False),        # group chats have negative ids
])
def test_setup_mode_until_a_real_chat_id_is_set(setting, expected):
    assert is_setup_mode(setting) is expected


def test_only_the_configured_chat_is_authorized():
    assert is_authorized(42, 42)
    assert not is_authorized(43, 42)
    assert not is_authorized(None, 42)


# ---------- /log and /wellness parsing ----------

@pytest.mark.parametrize("word, expected", [
    ("today", TODAY),
    ("Yesterday", date(2026, 2, 14)),
    ("2026-02-01", date(2026, 2, 1)),
    ("padel", None),
    ("90", None),
])
def test_parse_day(word, expected):
    assert parse_day(word, TODAY) == expected


def test_parse_log_defaults_to_today():
    assert parse_log_args(["gym", "90", "7"], TODAY) == {
        "date": "2026-02-15", "sport_type": "gym", "duration_min": 90, "rpe": 7, "notes": None}


def test_parse_log_with_date_alias_minutes_suffix_and_notes():
    args = parse_log_args(["yesterday", "TT", "90min", "6", "serve", "practice"], TODAY)
    assert args["date"] == "2026-02-14"
    assert args["sport_type"] == "table_tennis"
    assert args["duration_min"] == 90
    assert args["notes"] == "serve practice"
    assert parse_log_args(["match", "60", "8"], TODAY)["sport_type"] == "table_tennis_match"


@pytest.mark.parametrize("args, message", [
    ([], "Usage: /log"),
    (["gym", "90"], "Usage: /log"),
    (["chess", "60", "5"], "Unknown sport"),
    (["gym", "an hour", "7"], "Minutes must be a whole number"),
    (["gym", "60", "7.5"], "RPE must be a whole number"),
])
def test_parse_log_errors(args, message):
    with pytest.raises(CommandError, match=message):
        parse_log_args(args, TODAY)


def test_parse_wellness_with_comma_decimal_and_resting_hr():
    assert parse_wellness_args(["7,5", "3", "4", "2", "52"], TODAY) == {
        "date": "2026-02-15", "sleep_hours": 7.5, "soreness": 3, "fatigue": 4, "stress": 2,
        "resting_hr": 52}


def test_parse_wellness_with_date_and_without_resting_hr():
    args = parse_wellness_args(["yesterday", "6", "5", "5", "5"], TODAY)
    assert args["date"] == "2026-02-14" and args["resting_hr"] is None


@pytest.mark.parametrize("args, message", [
    (["7", "3", "4"], "Usage: /wellness"),
    (["7", "3", "4", "3", "52", "extra"], "Usage: /wellness"),
    (["seven", "3", "4", "3"], "Sleep must be a number"),
    (["7", "3", "high", "3"], "Fatigue must be a whole number"),
])
def test_parse_wellness_errors(args, message):
    with pytest.raises(CommandError, match=message):
        parse_wellness_args(args, TODAY)


def test_log_command_only_proposes(ctx):
    sessions_before = count(ctx.db, TrainingSession)
    result = propose_from_command(ctx, "log", ["padel", "75", "6"])
    assert result["summary"] == "Log padel, 75 min, RPE 6 (load 450) on 2026-02-15"
    assert ctx.db.get(PendingAction, result["pending_action_id"]).status == "proposed"
    assert count(ctx.db, TrainingSession) == sessions_before


@pytest.mark.parametrize("command, args, message", [
    ("log", ["gym", "60", "11"], "rpe must be between 1 and 10"),  # range checked by the tool
    ("log", ["2026-02-16", "gym", "60", "7"], "in the future"),
    ("log", ["gym", "900", "7"], "duration_min"),
    ("wellness", ["7", "3", "4", "3"], "already logged"),  # the sample has wellness for today
])
def test_commands_reuse_tool_validation(ctx, command, args, message):
    with pytest.raises(CommandError, match=message):
        propose_from_command(ctx, command, args)
    assert count(ctx.db, PendingAction) == 0


# ---------- /status ----------

def test_status_on_spike_day(ctx):
    out = status_text(ctx)
    assert "Status for 2026-02-15" in out
    assert "Acute load (7 days): 4,253" in out
    assert "Chronic load (weekly avg, 28 days): 2,514" in out
    assert "ACWR: 1.69" in out
    assert "Monotony: 2.08 · Strain: 8,842" in out
    assert "Last session: 2026-02-15 (today)" in out
    assert "🔴 ACWR is 1.69, above the 1.5 threshold" in out  # the high load spike flag
    assert "Nothing logged" not in out


def test_status_warns_when_nothing_was_logged_recently(sample_db):
    out = status_text(ToolContext(db=sample_db, today=date(2026, 3, 20)))  # data ends 2026-03-05
    assert "Last session: 2026-03-05 (15 days ago)" in out
    assert "Nothing logged for 15 days" in out


def test_status_warns_when_weeks_of_the_acwr_window_are_empty(db):
    # Like the real history: logging stopped months ago, then one session today.
    db.add_all([TrainingSession(date=date(2025, 11, 1), sport_type="gym", duration_min=60, rpe=6),
                TrainingSession(date=TODAY, sport_type="gym", duration_min=60, rpe=7)])
    db.commit()
    out = status_text(ToolContext(db=db, today=TODAY))
    assert "ACWR: 4.00" in out
    assert "Only 1 of the last 4 weeks have logged sessions" in out
    assert "Load alerts are paused" in out


def test_status_on_empty_database(db):
    out = status_text(ToolContext(db=db, today=TODAY))
    assert "ACWR: n/a" in out
    assert "No data logged yet." in out
    assert "No flags." in out


# ---------- Confirm / Reject buttons ----------

def test_callback_data_round_trip():
    assert parse_callback(callback_data(True, 12)) == (True, 12)
    assert parse_callback(callback_data(False, 3)) == (False, 3)


@pytest.mark.parametrize("data", ["confirm", "delete:3", "confirm:abc", "confirm:-1"])
def test_unexpected_callback_data_is_rejected(data):
    with pytest.raises(ValueError):
        parse_callback(data)


def test_confirm_button_saves_and_tells_the_agent(ctx):
    action_id = propose_from_command(ctx, "log", ["gym", "60", "7", "legs"])["pending_action_id"]
    before = count(ctx.db, TrainingSession)

    decision = decide(ctx.db, action_id, confirm=True)

    assert decision.text == "✅ Saved: Log gym, 60 min, RPE 7 (load 420) on 2026-02-15 (legs)"
    assert decision.note == f"User confirmed pending action {action_id}: Log gym, 60 min, RPE 7 (load 420) on 2026-02-15 (legs)"
    assert count(ctx.db, TrainingSession) == before + 1


def test_reject_button_saves_nothing(ctx):
    action_id = propose_from_command(ctx, "log", ["gym", "60", "7"])["pending_action_id"]
    before = count(ctx.db, TrainingSession)
    decision = decide(ctx.db, action_id, confirm=False)
    assert decision.text.startswith("❌ Rejected, nothing saved")
    assert "rejected" in decision.note
    assert count(ctx.db, TrainingSession) == before


def test_second_tap_changes_nothing(ctx):
    action_id = propose_from_command(ctx, "log", ["gym", "60", "7"])["pending_action_id"]
    decide(ctx.db, action_id, confirm=True)
    before = count(ctx.db, TrainingSession)

    decision = decide(ctx.db, action_id, confirm=False)  # e.g. Reject on an old message

    assert decision.text.startswith("Already confirmed")
    assert decision.note is None
    assert count(ctx.db, TrainingSession) == before


def test_unknown_proposal(ctx):
    decision = decide(ctx.db, 9999, confirm=True)
    assert decision.text == "This proposal no longer exists." and decision.note is None


def test_two_wellness_proposals_for_one_day_cannot_both_be_saved(ctx):
    ctx.today = date(2026, 3, 10)
    first = propose_from_command(ctx, "wellness", ["7", "3", "3", "3"])["pending_action_id"]
    second = propose_from_command(ctx, "wellness", ["6", "4", "4", "4"])["pending_action_id"]

    assert decide(ctx.db, first, confirm=True).text.startswith("✅ Saved")
    decision = decide(ctx.db, second, confirm=True)

    assert decision.text.startswith("Could not save") and decision.note is None
    assert ctx.db.scalar(select(Wellness.sleep_hours).where(Wellness.date == date(2026, 3, 10))) == 7.0


def test_plan_change_is_accepted_not_saved(ctx):
    from src.tools import propose_plan_change
    action_id = propose_plan_change(ctx, "2026-02-16", "Easy spin", "ACWR 1.69")["pending_action_id"]
    assert decide(ctx.db, action_id, confirm=True).text.startswith("✅ Accepted")


# ---------- formatting agent replies ----------

def test_markdown_to_html_escapes_and_converts():
    out = markdown_to_html("## Load\nYour **ACWR is 1.69** (> 1.5 & rising)\n- sleep `5.5 h`\n* soreness 8")
    assert out == ("<b>Load</b>\n"
                   "Your <b>ACWR is 1.69</b> (&gt; 1.5 &amp; rising)\n"
                   "• sleep <code>5.5 h</code>\n"
                   "• soreness 8")


def test_markdown_to_html_leaves_html_like_text_harmless():
    assert markdown_to_html("<b>not bold</b>") == "&lt;b&gt;not bold&lt;/b&gt;"


def test_short_messages_are_not_split():
    assert split_message("hello") == ["hello"]
    assert split_message("") == []


def test_long_messages_split_at_line_breaks():
    lines = [f"line {i} " + "x" * 40 for i in range(200)]
    chunks = split_message("\n".join(lines), limit=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n".join(chunks).splitlines() == lines  # nothing lost, no line cut in half


def test_one_very_long_line_is_cut_hard():
    chunks = split_message("x" * 2500, limit=1000)
    assert [len(c) for c in chunks] == [1000, 1000, 500]


def test_api_errors_get_friendly_messages():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    auth = anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None)
    server = anthropic.InternalServerError("oops", response=httpx.Response(500, request=request), body=None)
    assert "ANTHROPIC_API_KEY" in api_error_message(auth)
    assert "internet" in api_error_message(anthropic.APIConnectionError(request=request))
    assert "(500)" in api_error_message(server)
    assert "Something went wrong" in api_error_message(RuntimeError())


# ---------- agent turns through the bot ----------

@pytest.fixture
def state(bot_state):
    return bot_state


def test_agent_proposal_comes_back_with_a_summary_and_can_be_confirmed(state):
    state.agent.client.responses = [
        response(tool_use("log_session", {"date": "2026-02-15", "sport_type": "padel",
                                          "duration_min": 75, "rpe": 6}, "a")),
        response(text("Proposed padel, 75 min at RPE 6. Waiting for your confirmation.")),
    ]

    reply, proposals = run_agent_turn(state, "played padel 75 min, felt like a 6")

    assert "Waiting for your confirmation" in reply.text
    [(action_id, summary)] = proposals
    assert summary == "Log padel, 75 min, RPE 6 (load 450) on 2026-02-15"
    with state.session_factory() as db:
        before = count(db, TrainingSession)
        assert decide(db, action_id, confirm=True).text.startswith("✅ Saved")
        assert count(db, TrainingSession) == before + 1


def test_daily_check_is_scheduled_at_8_vienna_time_and_after_startup():
    from zoneinfo import ZoneInfo

    state = BotState(make_session_factory(make_engine(":memory:")), Agent(None, "x", log_path=None))
    app = build_application("123456:TEST-TOKEN", state, allowed_chat_id=1)  # no network at build time

    jobs = {job.name: job for job in app.job_queue.jobs()}
    assert set(jobs) == {"daily_check", "startup_check"}
    trigger = jobs["daily_check"].job.trigger
    assert trigger.timezone == ZoneInfo("Europe/Vienna")
    assert "hour='8', minute='0'" in str(trigger)
    # A PC asleep at 08:00 must run the check on wake-up, once, instead of skipping the day.
    assert jobs["daily_check"].job.misfire_grace_time is None
    assert jobs["daily_check"].job.coalesce is True


def test_bot_uses_the_fixed_demo_date(state):
    assert state.today() == TODAY
    state.as_of = None
    assert state.today() != TODAY  # the real date in Europe/Vienna


def test_conversation_resets_after_an_hour_idle(state):
    state.agent.messages = [{"role": "user", "content": "old question"}]
    state.last_active = 0.0

    assert not state.expire_if_idle(now=600.0)  # 10 minutes later: same conversation
    assert state.agent.messages

    assert state.expire_if_idle(now=600.0 + IDLE_RESET_SECONDS + 1)
    assert state.agent.messages == []
