"""The daily check: run the rules every morning and send new flags to Telegram.

Schedule: 08:00 Europe/Vienna, plus once shortly after startup (so a PC that was off at
08:00 still gets its alerts). Running the check more than once is safe; see below.

Which flags become alerts (alert_candidates):
- Load flags (spike, elevated, undertraining, monotony) only when all 4 weeks of the ACWR
  window contain logged sessions. Otherwise ACWR may compare against weeks that were
  simply not logged (after a gap, one session gives ACWR 4.0).
- No "undertraining" during a logging gap (3+ days without a session): that is far more
  likely forgotten logs than rest. A "no recent data" reminder is sent instead, once per gap.
- Sleep and soreness flags always: they do not depend on the load history.

Never the same alert twice (run_check): a flag type is not alerted again within 7 days
of its last alert. A spike that lasts 5 days gives one alert; an escalation from
"elevated" to "spike" is a different flag type and does alert. The rule only looks at
what was already alerted, so it also works after the bot was off for a few days.

Store first, send second (send_unsent): a new alert is saved with sent = false, sent to
Telegram, then marked sent. If Telegram is unreachable it stays unsent and is retried on
the next run. (Exactly-once delivery over a network is impossible in general: a crash
between sending and marking would send it again. That window is a few milliseconds.)

Alert texts come straight from the rules, no LLM: free, no invented numbers, and they
still work when the Claude API is down.

Usage (one check by hand):
  python -m src.scheduler --db data/demo.db --as-of 2026-02-15           # dry run, prints only
  python -m src.scheduler --db data/demo.db --as-of 2026-02-15 --send    # store and send
"""

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, time, timedelta
from itertools import groupby
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import get_settings
from src.models import Alert
from src.rules import COVERAGE_WEEKS, LOAD_FLAG_TYPES
from src.tools import ToolContext, get_active_flags

log = logging.getLogger("loadwise.scheduler")

CHECK_TIME = time(8, 0)
COOLDOWN_DAYS = 7     # the same flag type is alerted at most once per 7 days
STALE_DAYS = 3        # this many days without a session counts as a logging gap
NO_DATA = "no_recent_data"
ICONS = {"high": "🔴", "medium": "🟠", "low": "🟡", NO_DATA: "📝"}


def alert_candidates(status: dict) -> list[dict]:
    """Which active flags should become alerts. `status` is a get_active_flags() result."""
    days = status["days_since_last_session"]
    gap = days is not None and days >= STALE_DAYS
    full_coverage = status.get("weeks_with_sessions_28d") == COVERAGE_WEEKS

    candidates = []
    for flag in status["flags"]:
        if flag["flag_type"] in LOAD_FLAG_TYPES and not full_coverage:
            continue
        if flag["flag_type"] == "undertraining" and gap:
            continue
        candidates.append(flag)
    if gap:
        candidates.append({
            "flag_type": NO_DATA,
            "severity": "low",
            "message": (f"No session logged since {status['last_session_date']} ({days} days). Missing "
                        f"sessions count as rest, so log them with /log to keep the numbers accurate."),
        })
    return candidates


def _already_alerted(db: Session, candidate: dict, today: date, last_session: date | None) -> bool:
    query = select(Alert.id).where(Alert.flag_type == candidate["flag_type"], Alert.date <= today)
    if candidate["flag_type"] == NO_DATA:
        # One reminder per logging gap: any reminder after the last logged session counts.
        query = query.where(Alert.date > last_session)
    else:
        query = query.where(Alert.date > today - timedelta(days=COOLDOWN_DAYS))
    return db.scalar(query.limit(1)) is not None


def run_check(db: Session, today: date, store: bool = True) -> list[Alert]:
    """Evaluate `today` and return the new alerts. With store=True they are saved
    (sent = false); with store=False nothing is written (dry run)."""
    status = get_active_flags(ToolContext(db=db, today=today))
    last_session = date.fromisoformat(status["last_session_date"]) if status["last_session_date"] else None
    new = [
        Alert(date=today, flag_type=c["flag_type"], severity=c["severity"], message=c["message"], sent=False)
        for c in alert_candidates(status)
        if not _already_alerted(db, c, today, last_session)
    ]
    if store and new:
        db.add_all(new)
        db.commit()
    return new


def format_alert_message(alerts: list[Alert], day: date) -> str:
    lines = [f"LoadWise daily check · {day:%a} {day.day} {day:%b}"]
    lines += [f"{ICONS.get(a.flag_type, ICONS.get(a.severity, '•'))} {a.message}" for a in alerts]
    lines.append("/status for details. Monitoring only, not medical advice.")
    return "\n".join(lines)


async def send_unsent(db: Session, send: Callable[[str], Awaitable[object]]) -> int:
    """Send every stored, unsent alert: one message per day. Returns how many alerts were sent.
    If `send` fails, the alerts of that day stay unsent and the error propagates."""
    unsent = list(db.scalars(select(Alert).where(Alert.sent.is_(False)).order_by(Alert.date, Alert.id)))
    count = 0
    for day, group in groupby(unsent, key=lambda a: a.date):
        alerts = list(group)
        await send(format_alert_message(alerts, day))
        for alert in alerts:
            alert.sent = True
        db.commit()
        count += len(alerts)
    return count


# ---------- inside the bot: a python-telegram-bot JobQueue job (APScheduler underneath) ----------

async def daily_check(context) -> None:
    """The scheduled job. Uses the bot's shared state: database, today's date, chat id."""
    state = context.application.bot_data["state"]
    chat_id = context.application.bot_data["allowed_chat_id"]
    with state.session_factory() as db:
        today = state.today()
        new = run_check(db, today)
        try:
            sent = await send_unsent(db, lambda text: context.bot.send_message(chat_id, text))
        except Exception:
            db.rollback()
            log.exception("Sending alerts failed; they stay unsent and are retried on the next run")
            sent = 0
    context.application.bot_data["last_check"] = datetime.now(ZoneInfo(state.timezone)).isoformat(timespec="seconds")
    log.info("Daily check for %s: %d new alert(s), %d sent", today, len(new), sent)


def schedule_daily_check(application, timezone: str) -> None:
    """08:00 local time every day (time-zone aware, so daylight saving time is handled),
    plus one run 10 seconds after startup."""
    jobs = application.job_queue
    if jobs is None:  # pragma: no cover
        raise RuntimeError("JobQueue missing: install python-telegram-bot[job-queue]")
    jobs.run_daily(daily_check, time=CHECK_TIME.replace(tzinfo=ZoneInfo(timezone)), name="daily_check")
    jobs.run_once(daily_check, when=10, name="startup_check")


# ---------- one check by hand ----------

def main() -> None:
    from telegram import Bot

    from src.db import init_db, make_engine, make_session_factory

    settings = get_settings()
    parser = argparse.ArgumentParser(description="Run the daily check once.")
    parser.add_argument("--db", help="SQLite file (default: DB_PATH from .env)")
    parser.add_argument("--as-of", type=date.fromisoformat, help="Day to check (default: today), YYYY-MM-DD")
    parser.add_argument("--send", action="store_true",
                        help="Store new alerts and send unsent ones to Telegram (default: dry run)")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    today = args.as_of or datetime.now(ZoneInfo(settings.timezone)).date()
    engine = make_engine(args.db)
    init_db(engine)
    with make_session_factory(engine)() as db:
        new = run_check(db, today, store=args.send)
        print(f"{today}: {len(new)} new alert(s)" + ("" if args.send else " (dry run, nothing stored)"))
        if new:
            print(format_alert_message(new, today))
        if not args.send:
            return

        async def send_all() -> int:
            async with Bot(settings.telegram_bot_token) as bot:
                return await send_unsent(db, lambda text: bot.send_message(settings.telegram_chat_id, text))

        print(f"Sent {asyncio.run(send_all())} alert(s) to Telegram.")


if __name__ == "__main__":
    main()
