"""Tools the agent can call, plus the confirm/reject flow for proposed changes.

Each tool is a plain function `fn(ctx, **input) -> dict` with a JSON schema in TOOLS.
Read tools query the database and reuse src/metrics.py and src/rules.py.
Write tools never write: they create a pending action that the user must confirm
(human in the loop). confirm_action() is the only place proposed data gets saved.

Schemas use strict mode (`strict: true`), so the API guarantees the model's arguments
match them. Strict mode does not support numeric ranges (minimum/maximum), so ranges are
expressed as enums where small (RPE 1-10) and validated in Python otherwise.
"""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.import_csv import add_session_if_new
from src.metrics import compute_load_metrics, daily_loads, session_load
from src.models import PendingAction, TrainingSession, Wellness
from src.rules import evaluate_flags, weeks_with_sessions

SPORT_TYPES = [
    "gym", "table_tennis", "table_tennis_match", "padel",
    "running", "cycling", "hiking", "football", "other",
]
SCALE_1_10 = list(range(1, 11))
PROPOSAL_TOOLS = {"log_session", "log_wellness", "propose_plan_change"}


class ToolError(Exception):
    """A problem the model should see and can fix (bad date, value out of range...)."""


@dataclass
class ToolContext:
    db: Session
    today: date  # the agent's "today": the real date, or a fixed date for demos/evals


# ---------- helpers ----------

def _parse_date(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a date in YYYY-MM-DD format, got {value!r}")


def _as_of(ctx: ToolContext, value: str | None) -> date:
    return ctx.today if value is None else _parse_date(value, "as_of")


def _check_range(value, name: str, low, high):
    if not low <= value <= high:
        raise ToolError(f"{name} must be between {low} and {high}, got {value}")
    return value


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _history_start(db: Session) -> date | None:
    starts = [db.scalar(select(func.min(TrainingSession.date))), db.scalar(select(func.min(Wellness.date)))]
    starts = [d for d in starts if d is not None]
    return min(starts) if starts else None


def _freshness(db: Session, as_of: date) -> dict:
    """How recent the logged data is. Missing logs look like rest days to the metrics,
    so the agent needs this to avoid calling a forgotten week 'undertraining'."""
    last_session = db.scalar(select(func.max(TrainingSession.date)).where(TrainingSession.date <= as_of))
    last_wellness = db.scalar(select(func.max(Wellness.date)).where(Wellness.date <= as_of))
    return {
        "last_session_date": last_session.isoformat() if last_session else None,
        "days_since_last_session": (as_of - last_session).days if last_session else None,
        "last_wellness_date": last_wellness.isoformat() if last_wellness else None,
    }


def _sessions_between(db: Session, start: date, end: date) -> list[TrainingSession]:
    return list(db.scalars(
        select(TrainingSession)
        .where(TrainingSession.date >= start, TrainingSession.date <= end)
        .order_by(TrainingSession.date, TrainingSession.id)
    ))


# ---------- read tools ----------

def get_load_summary(ctx: ToolContext, days: int = 7, as_of: str | None = None) -> dict:
    day = _as_of(ctx, as_of)
    _check_range(days, "days", 1, 56)
    history_start = _history_start(ctx.db)
    # 28 days are enough for every metric; `days` only controls how many daily loads are listed.
    window = max(days, 28)
    daily = daily_loads(_sessions_between(ctx.db, day - timedelta(days=window - 1), day))

    if history_start is None:
        metrics, note = None, "No data logged yet."
    else:
        metrics = compute_load_metrics(daily, day, history_start)
        note = None
        if metrics.acwr is None:
            note = ("ACWR needs 28 days of history." if (day - history_start).days + 1 < 28
                    else "ACWR undefined: no training logged in the last 28 days.")

    return {
        "as_of": day.isoformat(),
        "daily_loads": [
            {"date": (day - timedelta(days=i)).isoformat(), "load": daily.get(day - timedelta(days=i), 0)}
            for i in range(days - 1, -1, -1)
        ],
        "acute_load_7d": metrics.acute if metrics else None,
        "chronic_load_weekly_avg_28d": _r(metrics.chronic, 0) if metrics else None,
        "acwr": _r(metrics.acwr) if metrics else None,
        "monotony_7d": _r(metrics.monotony) if metrics else None,
        "strain_7d": _r(metrics.strain, 0) if metrics else None,
        "weeks_with_sessions_28d": weeks_with_sessions(daily, day) if metrics else None,
        "note": note,
        **_freshness(ctx.db, day),
    }


def get_sessions(ctx: ToolContext, start_date: str, end_date: str) -> dict:
    start, end = _parse_date(start_date, "start_date"), _parse_date(end_date, "end_date")
    if start > end:
        raise ToolError("start_date must not be after end_date")
    if (end - start).days > 366:
        raise ToolError("date range is limited to one year")
    sessions = _sessions_between(ctx.db, start, end)
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "count": len(sessions),
        "sessions": [
            {"date": s.date.isoformat(), "sport_type": s.sport_type, "duration_min": s.duration_min,
             "rpe": s.rpe, "load": session_load(s.duration_min, s.rpe), "notes": s.notes}
            for s in sessions[:200]
        ],
        "truncated": len(sessions) > 200,
    }


def get_wellness_trend(ctx: ToolContext, days: int = 7, as_of: str | None = None) -> dict:
    day = _as_of(ctx, as_of)
    _check_range(days, "days", 1, 56)
    start = day - timedelta(days=days - 1)
    entries = list(ctx.db.scalars(
        select(Wellness).where(Wellness.date >= start, Wellness.date <= day).order_by(Wellness.date)
    ))

    def avg(attr: str) -> float | None:
        values = [getattr(w, attr) for w in entries if getattr(w, attr) is not None]
        return _r(mean(values), 1) if values else None

    return {
        "as_of": day.isoformat(),
        "days": days,
        "days_logged": len(entries),
        "days_missing": days - len(entries),
        "entries": [
            {"date": w.date.isoformat(), "sleep_hours": w.sleep_hours, "soreness": w.soreness,
             "fatigue": w.fatigue, "stress": w.stress, "resting_hr": w.resting_hr, "notes": w.notes}
            for w in entries
        ],
        "averages": {k: avg(k) for k in ("sleep_hours", "soreness", "fatigue", "stress", "resting_hr")},
        **_freshness(ctx.db, day),
    }


def get_active_flags(ctx: ToolContext, as_of: str | None = None) -> dict:
    day = _as_of(ctx, as_of)
    history_start = _history_start(ctx.db)
    if history_start is None:
        return {"as_of": day.isoformat(), "flags": [], "note": "No data logged yet.", **_freshness(ctx.db, day)}
    daily = daily_loads(_sessions_between(ctx.db, day - timedelta(days=27), day))
    wellness = {w.date: w for w in ctx.db.scalars(
        select(Wellness).where(Wellness.date >= day - timedelta(days=3), Wellness.date <= day))}
    metrics, flags = evaluate_flags(daily, wellness, day, history_start)
    return {
        "as_of": day.isoformat(),
        "acwr": _r(metrics.acwr),
        "monotony_7d": _r(metrics.monotony),
        "weeks_with_sessions_28d": weeks_with_sessions(daily, day),
        "flags": [{"flag_type": f.flag_type, "severity": f.severity, "message": f.message} for f in flags],
        **_freshness(ctx.db, day),
    }


# ---------- proposal tools (never write data directly) ----------

def _propose(ctx: ToolContext, action_type: str, payload: dict, summary: str) -> dict:
    action = PendingAction(action_type=action_type, payload=payload, status="proposed")
    ctx.db.add(action)
    ctx.db.commit()
    return {
        "pending_action_id": action.id,
        "status": "proposed",
        "summary": summary,
        "note": "Not saved yet. The user will be asked to confirm or reject it.",
    }


def _not_in_future(ctx: ToolContext, day: date) -> date:
    if day > ctx.today:
        raise ToolError(f"date {day} is in the future (today is {ctx.today})")
    return day


def log_session(ctx: ToolContext, date: str, sport_type: str, duration_min: int, rpe: int,
                notes: str | None = None) -> dict:
    day = _not_in_future(ctx, _parse_date(date, "date"))
    if sport_type not in SPORT_TYPES:
        raise ToolError(f"sport_type must be one of {SPORT_TYPES}")
    _check_range(duration_min, "duration_min", 1, 600)
    _check_range(rpe, "rpe", 1, 10)
    payload = {"date": day.isoformat(), "sport_type": sport_type, "duration_min": duration_min,
               "rpe": rpe, "notes": notes or None}
    # The load is returned so the agent can quote it instead of calculating it itself
    # (the Phase 6 evals caught it doing "75 x 5 = 375" when the tool gave no load).
    return {**_propose(ctx, "log_session", payload, describe_payload("log_session", payload)),
            "session_load": session_load(duration_min, rpe)}


def log_wellness(ctx: ToolContext, date: str, sleep_hours: float, soreness: int, fatigue: int,
                 stress: int, resting_hr: int | None = None, notes: str | None = None) -> dict:
    day = _not_in_future(ctx, _parse_date(date, "date"))
    _check_range(sleep_hours, "sleep_hours", 0, 24)
    for name, value in (("soreness", soreness), ("fatigue", fatigue), ("stress", stress)):
        _check_range(value, name, 1, 10)
    if resting_hr is not None:
        _check_range(resting_hr, "resting_hr", 25, 220)
    if ctx.db.scalar(select(Wellness.id).where(Wellness.date == day)):
        raise ToolError(f"wellness for {day} is already logged; changing it is not supported")
    payload = {"date": day.isoformat(), "sleep_hours": sleep_hours, "soreness": soreness,
               "fatigue": fatigue, "stress": stress, "resting_hr": resting_hr, "notes": notes or None}
    return _propose(ctx, "log_wellness", payload, describe_payload("log_wellness", payload))


def propose_plan_change(ctx: ToolContext, date: str, suggestion: str, reason: str) -> dict:
    day = _parse_date(date, "date")
    if not suggestion.strip() or not reason.strip():
        raise ToolError("suggestion and reason must not be empty")
    payload = {"date": day.isoformat(), "suggestion": suggestion.strip(), "reason": reason.strip()}
    return _propose(ctx, "plan_change", payload, describe_payload("plan_change", payload))


# ---------- confirming and rejecting proposals ----------

def describe_payload(action_type: str, p: dict) -> str:
    """One human-readable line, shown to the user before they confirm."""
    if action_type == "log_session":
        note = f" ({p['notes']})" if p.get("notes") else ""
        load = session_load(p["duration_min"], p["rpe"])
        return f"Log {p['sport_type']}, {p['duration_min']} min, RPE {p['rpe']} (load {load}) on {p['date']}{note}"
    if action_type == "log_wellness":
        hr = f", resting HR {p['resting_hr']}" if p.get("resting_hr") is not None else ""
        return (f"Log wellness for {p['date']}: sleep {p['sleep_hours']:g} h, soreness {p['soreness']}, "
                f"fatigue {p['fatigue']}, stress {p['stress']}{hr}")
    if action_type == "plan_change":
        return f"Plan change for {p['date']}: {p['suggestion']} (reason: {p['reason']})"
    return f"{action_type}: {p}"


def _get_proposed(db: Session, action_id: int) -> PendingAction:
    action = db.get(PendingAction, action_id)
    if action is None:
        raise ToolError(f"pending action {action_id} does not exist")
    if action.status != "proposed":
        raise ToolError(f"pending action {action_id} is already {action.status}")
    return action


def confirm_action(db: Session, action_id: int) -> str:
    """Apply a proposed action. The ONLY code path that saves agent-proposed data."""
    action = _get_proposed(db, action_id)
    p = action.payload
    result = "confirmed"
    if action.action_type == "log_session":
        data = {**p, "date": date.fromisoformat(p["date"])}
        if not add_session_if_new(db, data):
            result = "confirmed (an identical session already existed, nothing added)"
    elif action.action_type == "log_wellness":
        db.add(Wellness(**{**p, "date": date.fromisoformat(p["date"])}))
    # plan_change: confirming only records the decision; there is no training plan table.
    action.status = "confirmed"
    db.commit()
    return result


def reject_action(db: Session, action_id: int) -> None:
    action = _get_proposed(db, action_id)
    action.status = "rejected"
    db.commit()


# ---------- tool definitions for the API ----------

def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


DATE = {"type": "string", "format": "date"}
AS_OF = {"type": "string", "format": "date",
         "description": "Day to evaluate (YYYY-MM-DD). Omit to use today."}
DAYS = {"type": "integer", "description": "Number of days to cover, 1-56. Default 7."}

TOOLS = [
    {
        "name": "get_load_summary",
        "description": "Training load metrics as of a day: daily loads (duration x RPE) for the last `days` days, "
                       "acute load (7-day sum), chronic load (28-day weekly average), ACWR, monotony and strain, "
                       "plus how recently data was logged.",
        "strict": True,
        "input_schema": _schema({"days": DAYS, "as_of": AS_OF}, []),
    },
    {
        "name": "get_sessions",
        "description": "List logged training sessions between two dates (inclusive), with each session's load.",
        "strict": True,
        "input_schema": _schema({"start_date": DATE, "end_date": DATE}, ["start_date", "end_date"]),
    },
    {
        "name": "get_wellness_trend",
        "description": "Daily wellness entries (sleep, soreness, fatigue, stress, resting HR) for the last `days` "
                       "days, with averages and the number of days not logged.",
        "strict": True,
        "input_schema": _schema({"days": DAYS, "as_of": AS_OF}, []),
    },
    {
        "name": "get_active_flags",
        "description": "Run the monitoring rules for a day and return active flags (load spike, elevated load, "
                       "undertraining, high monotony, poor sleep, high soreness) with their values.",
        "strict": True,
        "input_schema": _schema({"as_of": AS_OF}, []),
    },
    {
        "name": "log_session",
        "description": "Propose logging a training session. Does NOT save it: creates a pending action the user "
                       "confirms or rejects. Only call it when the user reported a session.",
        "strict": True,
        "input_schema": _schema({
            "date": DATE,
            "sport_type": {"type": "string", "enum": SPORT_TYPES},
            "duration_min": {"type": "integer", "description": "Whole session length in minutes, 1-600."},
            "rpe": {"type": "integer", "enum": SCALE_1_10,
                    "description": "Session RPE 1-10 for the whole session."},
            "notes": {"type": "string", "description": "The athlete's own words only, never your reasoning."},
        }, ["date", "sport_type", "duration_min", "rpe"]),
    },
    {
        "name": "log_wellness",
        "description": "Propose logging the daily wellness entry. Does NOT save it: creates a pending action the "
                       "user confirms or rejects. One entry per day.",
        "strict": True,
        "input_schema": _schema({
            "date": DATE,
            "sleep_hours": {"type": "number", "description": "Hours slept, 0-24."},
            "soreness": {"type": "integer", "enum": SCALE_1_10},
            "fatigue": {"type": "integer", "enum": SCALE_1_10},
            "stress": {"type": "integer", "enum": SCALE_1_10},
            "resting_hr": {"type": "integer", "description": "Resting heart rate in bpm, if known."},
            "notes": {"type": "string"},
        }, ["date", "sleep_hours", "soreness", "fatigue", "stress"]),
    },
    {
        "name": "propose_plan_change",
        "description": "Propose a change to upcoming training (for example a lighter session). Does NOT apply "
                       "anything: the user confirms or rejects it.",
        "strict": True,
        "input_schema": _schema({
            "date": DATE,
            "suggestion": {"type": "string"},
            "reason": {"type": "string", "description": "The data behind the suggestion, with values."},
        }, ["date", "suggestion", "reason"]),
    },
]

HANDLERS = {
    "get_load_summary": get_load_summary,
    "get_sessions": get_sessions,
    "get_wellness_trend": get_wellness_trend,
    "get_active_flags": get_active_flags,
    "log_session": log_session,
    "log_wellness": log_wellness,
    "propose_plan_change": propose_plan_change,
}


def execute_tool(ctx: ToolContext, name: str, tool_input: dict) -> tuple[str, bool]:
    """Run a tool. Returns (JSON text, is_error). Never raises: errors go back to the model."""
    handler = HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"unknown tool {name!r}"}), True
    try:
        return json.dumps(handler(ctx, **tool_input)), False
    except ToolError as e:
        ctx.db.rollback()
        return json.dumps({"error": str(e)}), True
    except TypeError as e:  # wrong or missing arguments
        ctx.db.rollback()
        return json.dumps({"error": f"invalid arguments: {e}"}), True
