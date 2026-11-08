"""LoadWise Telegram bot: log sessions and wellness, check status, chat with the agent.

The bot uses long polling (it asks Telegram for new messages), so it needs no public URL
or open port: it runs on a laptop or in a container as is.

Security: only one chat is served, TELEGRAM_CHAT_ID. A guard handler runs before every
other handler and drops updates from any other chat, so nobody else can read the data or
spend API credit. If TELEGRAM_CHAT_ID is not set yet, the bot starts in setup mode: it only
replies with the sender's chat id (so you can copy it into .env) and touches no data.

Every write is a proposal with Confirm/Reject buttons, also for /log and /wellness, so there
is exactly one code path that saves data (tools.confirm_action). Button data holds the
pending action id and the proposal lives in the database, so buttons survive a restart.

The logic lives in plain functions (parsing, formatting, deciding) that the tests call
directly; the async handlers at the bottom are thin glue around them.

Usage:
  python -m src.bot                                        # real data (DB_PATH from .env)
  python -m src.bot --db data/demo.db --as-of 2026-02-15   # demo data on a fixed date
"""

import argparse
import asyncio
import html
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from src.agent import Agent, AgentReply
from src.config import get_settings
from src.models import PendingAction
from src.tools import (
    SPORT_TYPES,
    ToolContext,
    ToolError,
    confirm_action,
    describe_payload,
    get_active_flags,
    get_load_summary,
    log_session,
    log_wellness,
    reject_action,
)

log = logging.getLogger("loadwise.bot")

IDLE_RESET_SECONDS = 60 * 60  # a new agent conversation after an hour of silence
# Telegram allows 4096 characters per message. Chunks are cut from the raw text before
# HTML conversion, and escaping (& -> &amp;) makes the text longer, so leave a margin.
MAX_CHUNK = 3500

# Short names that are easy to type on a phone. Full sport names always work too.
SPORT_ALIASES = {
    "tt": "table_tennis",
    "match": "table_tennis_match",
    "ttm": "table_tennis_match",
    "run": "running",
    "bike": "cycling",
    "hike": "hiking",
}

LOG_USAGE = ("Usage: /log [today|yesterday|YYYY-MM-DD] <sport> <minutes> <rpe> [notes]\n"
             "Example: /log yesterday padel 75 6")
WELLNESS_USAGE = ("Usage: /wellness [today|yesterday|YYYY-MM-DD] <sleep hours> <soreness> <fatigue> "
                  "<stress> [resting HR]\n"
                  "Example: /wellness 7.5 3 4 3 52  (soreness, fatigue, stress on 1-10)")
HELP_TEXT = f"""\
LoadWise: training load and recovery monitoring. Informational only, not medical advice.

/log: propose a training session
{LOG_USAGE}
Sports: {", ".join(SPORT_TYPES)}
Short names: tt = table_tennis, match = table_tennis_match

/wellness: propose today's wellness entry
{WELLNESS_USAGE}

/status: load metrics and active flags (free, no AI)
/reset: start a new conversation with the assistant
/help: this message

Anything else you write goes to the AI assistant, e.g. "played padel 75 min, felt hard" \
or "how was my week?". Nothing is saved until you tap Confirm."""

SEVERITY_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}


class CommandError(Exception):
    """A malformed command; the message is shown to the user as is."""


# ---------- authorization ----------

def is_setup_mode(chat_id_setting: str | None) -> bool:
    """True while TELEGRAM_CHAT_ID is missing or still the .env.example placeholder."""
    return not chat_id_setting or not chat_id_setting.strip().lstrip("-").isdigit()


def is_authorized(chat_id: int | None, allowed_chat_id: int) -> bool:
    return chat_id is not None and chat_id == allowed_chat_id


# ---------- parsing /log and /wellness ----------

def parse_day(word: str, today: date) -> date | None:
    """'today', 'yesterday' or YYYY-MM-DD; None if the word is not a date."""
    lowered = word.lower()
    if lowered == "today":
        return today
    if lowered == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(word)
    except ValueError:
        return None


def _take_day(args: list[str], today: date) -> tuple[date, list[str]]:
    """The date is optional and comes first; without it the entry is for today."""
    day = parse_day(args[0], today) if args else None
    return (day, args[1:]) if day else (today, args)


def _whole_number(value: str, name: str, usage: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise CommandError(f"{name} must be a whole number, got {value!r}.\n\n{usage}")


def parse_log_args(args: list[str], today: date) -> dict:
    """Turn '/log yesterday tt 90 7 serve practice' into log_session arguments.

    Only the format is checked here; value ranges (RPE 1-10, no future dates...) are
    checked by tools.log_session, the same validation the agent's proposals go through.
    """
    day, rest = _take_day(args, today)
    if len(rest) < 3:
        raise CommandError(LOG_USAGE)
    sport = SPORT_ALIASES.get(rest[0].lower(), rest[0].lower())
    if sport not in SPORT_TYPES:
        raise CommandError(f"Unknown sport {rest[0]!r}. Use one of: {', '.join(SPORT_TYPES)} "
                           f"(or tt, match).")
    return {
        "date": day.isoformat(),
        "sport_type": sport,
        "duration_min": _whole_number(rest[1].lower().removesuffix("min"), "Minutes", LOG_USAGE),
        "rpe": _whole_number(rest[2], "RPE", LOG_USAGE),
        "notes": " ".join(rest[3:]) or None,
    }


def parse_wellness_args(args: list[str], today: date) -> dict:
    """Turn '/wellness 7,5 3 4 3 52' into log_wellness arguments."""
    day, rest = _take_day(args, today)
    if len(rest) not in (4, 5):
        raise CommandError(WELLNESS_USAGE)
    try:
        sleep = float(rest[0].replace(",", "."))  # accept the German decimal comma
    except ValueError:
        raise CommandError(f"Sleep must be a number of hours, got {rest[0]!r}.\n\n{WELLNESS_USAGE}")
    soreness, fatigue, stress = (_whole_number(value, name, WELLNESS_USAGE)
                                 for value, name in zip(rest[1:4], ("Soreness", "Fatigue", "Stress")))
    return {
        "date": day.isoformat(),
        "sleep_hours": sleep,
        "soreness": soreness,
        "fatigue": fatigue,
        "stress": stress,
        "resting_hr": _whole_number(rest[4], "Resting HR", WELLNESS_USAGE) if len(rest) == 5 else None,
    }


def propose_from_command(ctx: ToolContext, command: str, args: list[str]) -> dict:
    """Create a pending action for /log or /wellness. Returns the tool result
    ({pending_action_id, summary, ...}); raises CommandError with a message for the user."""
    if command == "log":
        tool, params = log_session, parse_log_args(args, ctx.today)
    else:
        tool, params = log_wellness, parse_wellness_args(args, ctx.today)
    try:
        return tool(ctx, **params)
    except ToolError as e:
        ctx.db.rollback()
        raise CommandError(f"Not proposed: {e}.")


# ---------- /status ----------

def _load(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f}"


def _ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def format_status(summary: dict, flags: dict) -> str:
    """HTML for /status, built from the get_load_summary and get_active_flags tool results."""
    lines = [
        f"<b>Status for {summary['as_of']}</b>",
        f"Acute load (7 days): {_load(summary['acute_load_7d'])}",
        f"Chronic load (weekly avg, 28 days): {_load(summary['chronic_load_weekly_avg_28d'])}",
        f"ACWR: {_ratio(summary['acwr'])}",
        f"Monotony: {_ratio(summary['monotony_7d'])} · Strain: {_load(summary['strain_7d'])}",
    ]
    if summary["note"]:
        lines.append(f"<i>{html.escape(summary['note'])}</i>")

    days = summary["days_since_last_session"]
    if days is not None:
        when = "today" if days == 0 else f"{days} day{'s' if days != 1 else ''} ago"
        lines.append(f"Last session: {summary['last_session_date']} ({when})")
        if days >= 3:
            lines.append(f"⚠️ Nothing logged for {days} days. Missing sessions count as rest, "
                         f"so the numbers may be lower than your real load.")

    lines.append("")
    if flags["flags"]:
        lines.append("<b>Flags</b>")
        lines += [f"{SEVERITY_ICON.get(f['severity'], '•')} {html.escape(f['message'])}" for f in flags["flags"]]
    else:
        lines.append("No flags.")
    return "\n".join(lines)


def status_text(ctx: ToolContext) -> str:
    return format_status(get_load_summary(ctx), get_active_flags(ctx))


# ---------- Confirm / Reject buttons ----------

def callback_data(confirm: bool, action_id: int) -> str:
    return f"{'confirm' if confirm else 'reject'}:{action_id}"


def parse_callback(data: str) -> tuple[bool, int]:
    verb, _, raw_id = data.partition(":")
    if verb not in ("confirm", "reject") or not raw_id.isdigit():
        raise ValueError(f"unexpected button data {data!r}")
    return verb == "confirm", int(raw_id)


@dataclass
class Decision:
    text: str         # replaces the proposal message
    note: str | None  # passed on to the agent; None when nothing changed


def decide(db: Session, action_id: int, confirm: bool) -> Decision:
    action = db.get(PendingAction, action_id)
    if action is None:
        return Decision("This proposal no longer exists.", None)
    summary = describe_payload(action.action_type, action.payload)
    try:
        if confirm:
            result = confirm_action(db, action_id)
            verb = "Accepted" if action.action_type == "plan_change" else "Saved"
            extra = "" if result == "confirmed" else " (an identical session already existed, nothing added)"
            return Decision(f"✅ {verb}: {summary}{extra}",
                            f"User confirmed pending action {action_id}: {summary}")
        reject_action(db, action_id)
        return Decision(f"❌ Rejected, nothing saved: {summary}",
                        f"User rejected pending action {action_id}: {summary}")
    except ToolError:  # tapped twice, or confirmed in another message
        db.rollback()
        return Decision(f"Already {action.status}: {summary}", None)
    except IntegrityError:  # e.g. two wellness proposals for the same day, both confirmed
        db.rollback()
        return Decision(f"Could not save, it conflicts with data already logged: {summary}", None)


# ---------- agent replies ----------

def markdown_to_html(text: str) -> str:
    """The model writes light Markdown; Telegram's Markdown mode needs special characters
    escaped, so convert the few things the model uses to Telegram HTML instead."""
    lines = []
    for line in html.escape(text, quote=False).splitlines():
        heading = re.match(r"#{1,6}\s+(.*)", line)
        if heading:
            line = f"**{heading.group(1).replace('**', '')}**"
        lines.append(re.sub(r"^(\s*)[-*]\s+", r"\1• ", line))
    out = "\n".join(lines)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    return re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)


def split_message(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split at line breaks into chunks of at most `limit` characters."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:  # one very long line: cut it hard
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return [c for c in chunks if c.strip()]


def api_error_message(error: Exception) -> str:
    if isinstance(error, anthropic.AuthenticationError):
        return "The Claude API key was rejected. Check ANTHROPIC_API_KEY in .env."
    if isinstance(error, anthropic.RateLimitError):
        return "The Claude API is busy (rate limit). Try again in a minute."
    if isinstance(error, anthropic.APIConnectionError):
        return "I could not reach the Claude API. Check the internet connection."
    if isinstance(error, anthropic.APIStatusError):
        return f"The Claude API returned an error ({error.status_code}). Try again later."
    return "Something went wrong. Check the bot logs."


@dataclass
class BotState:
    """Everything the handlers share. One authorized chat, so one agent conversation."""
    session_factory: sessionmaker[Session]
    agent: Agent
    timezone: str = "Europe/Vienna"
    as_of: date | None = None  # fixed "today" for demo data
    # One message at a time: the agent's history is a plain list, two concurrent turns
    # would interleave their messages and break the conversation.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_active: float = field(default_factory=time.monotonic)

    def today(self) -> date:
        return self.as_of or datetime.now(ZoneInfo(self.timezone)).date()

    def expire_if_idle(self, now: float | None = None) -> bool:
        """Start a new conversation after an hour of silence: an old history makes every
        request longer (and more expensive) and is rarely what a new question is about."""
        now = time.monotonic() if now is None else now
        expired = bool(self.agent.messages) and now - self.last_active > IDLE_RESET_SECONDS
        if expired:
            self.agent.reset()
        self.last_active = now
        return expired


def run_agent_turn(state: BotState, text: str) -> tuple[AgentReply, list[tuple[int, str]]]:
    """Blocking: one agent turn plus the summaries of any proposals it made.
    Runs in a worker thread with its own database session."""
    with state.session_factory() as db:
        reply = state.agent.ask(text, ToolContext(db=db, today=state.today()))
        proposals = []
        for action_id in reply.pending_action_ids:
            action = db.get(PendingAction, action_id)
            proposals.append((action_id, describe_payload(action.action_type, action.payload)))
    return reply, proposals


# ---------- Telegram glue (checked by hand; the logic above is unit tested) ----------

def _state(context: ContextTypes.DEFAULT_TYPE) -> BotState:
    return context.application.bot_data["state"]


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs first (group -1) for every update; stops anything not from the allowed chat."""
    chat = update.effective_chat
    if not is_authorized(chat.id if chat else None, context.application.bot_data["allowed_chat_id"]):
        log.warning("Ignored an update from unauthorized chat %s", chat.id if chat else None)
        raise ApplicationHandlerStop


async def setup_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    print(f"Message from chat id {chat_id}")
    await update.effective_message.reply_text(
        f"Your chat id is {chat_id}.\nPut TELEGRAM_CHAT_ID={chat_id} in .env and restart the bot.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    with state.session_factory() as db:
        text = status_text(ToolContext(db=db, today=state.today()))
    await update.effective_message.reply_html(text)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    async with state.lock:
        state.agent.reset()
    await update.effective_message.reply_text("Started a new conversation.")


async def _send_proposal(update: Update, action_id: int, summary: str) -> None:
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=callback_data(True, action_id)),
        InlineKeyboardButton("❌ Reject", callback_data=callback_data(False, action_id)),
    ]])
    await update.effective_message.reply_text(f"Confirm?\n{summary}", reply_markup=buttons)


async def _propose(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    state = _state(context)
    try:
        with state.session_factory() as db:
            result = propose_from_command(ToolContext(db=db, today=state.today()), command, context.args)
    except CommandError as e:
        await update.effective_message.reply_text(str(e))
        return
    await _send_proposal(update, result["pending_action_id"], result["summary"])


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _propose(update, context, "log")


async def cmd_wellness(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _propose(update, context, "wellness")


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Unknown command. Send /help for the list.")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        confirm, action_id = parse_callback(query.data)
    except ValueError:
        await query.answer("Unknown button.")
        return
    state = _state(context)
    async with state.lock:  # add_note must not race with an agent turn
        with state.session_factory() as db:
            decision = decide(db, action_id, confirm)
        if decision.note:
            state.agent.add_note(decision.note)
    await query.answer()
    await query.edit_message_text(decision.text)  # also removes the buttons


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Telegram shows 'typing…' for about 5 seconds, so repeat until cancelled."""
    while True:
        try:
            await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        except TelegramError:
            pass
        await asyncio.sleep(4)


async def _send_long(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    for chunk in split_message(text):
        try:
            await context.bot.send_message(chat_id, markdown_to_html(chunk), parse_mode=ParseMode.HTML)
        except BadRequest:  # Telegram rejected the HTML: send the raw text instead
            await context.bot.send_message(chat_id, chunk)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    chat_id = update.effective_chat.id
    async with state.lock:
        if state.expire_if_idle():
            log.info("Conversation reset after %s s idle", IDLE_RESET_SECONDS)
        typing = asyncio.create_task(_keep_typing(context, chat_id))
        try:
            # The Claude call blocks for seconds; in a thread, the bot stays responsive
            # (e.g. /status still answers while the agent is thinking).
            reply, proposals = await asyncio.to_thread(run_agent_turn, state, update.effective_message.text)
        except anthropic.APIError as e:
            log.warning("Claude API error: %r", e)
            await update.effective_message.reply_text(api_error_message(e))
            return
        finally:
            typing.cancel()
        await _send_long(context, chat_id, reply.text)
        for action_id, summary in proposals:
            await _send_proposal(update, action_id, summary)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Error while handling an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Something went wrong. Check the bot logs.")


async def _set_command_menu(application: Application) -> None:
    """The list Telegram shows when you type '/'."""
    await application.bot.set_my_commands([
        BotCommand("log", "Propose a training session"),
        BotCommand("wellness", "Propose today's wellness entry"),
        BotCommand("status", "Load metrics and flags"),
        BotCommand("reset", "New conversation with the assistant"),
        BotCommand("help", "How to use LoadWise"),
    ])


def build_application(token: str, state: BotState, allowed_chat_id: int) -> Application:
    app = ApplicationBuilder().token(token).post_init(_set_command_menu).build()
    app.bot_data.update(state=state, allowed_chat_id=allowed_chat_id)
    app.add_handler(TypeHandler(Update, guard), group=-1)
    # New messages only: editing an old "/log ..." message must not create a second proposal.
    new = filters.UpdateType.MESSAGE
    for name, callback in (("start", cmd_help), ("help", cmd_help), ("status", cmd_status),
                           ("reset", cmd_reset), ("log", cmd_log), ("wellness", cmd_wellness)):
        app.add_handler(CommandHandler(name, callback, filters=new))
    app.add_handler(MessageHandler(filters.COMMAND & new, cmd_unknown))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & new, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    from src.db import init_db, make_engine, make_session_factory

    settings = get_settings()
    parser = argparse.ArgumentParser(description="Run the LoadWise Telegram bot.")
    parser.add_argument("--db", help="SQLite file (default: DB_PATH from .env)")
    parser.add_argument("--as-of", type=date.fromisoformat,
                        help="Pretend today is this date (for demo data), YYYY-MM-DD")
    parser.add_argument("--model", default=settings.llm_model)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = settings.telegram_bot_token
    if not token or ":" not in token:  # real tokens look like 123456789:AA...
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env (create a bot with @BotFather in Telegram).")

    if is_setup_mode(settings.telegram_chat_id):
        print("Setup mode: TELEGRAM_CHAT_ID is not set. Send any message to your bot to get your chat id.")
        app = ApplicationBuilder().token(token).build()
        app.add_handler(MessageHandler(filters.ALL, setup_reply))
    else:
        try:
            client = anthropic.Anthropic()
        except anthropic.AnthropicError as e:
            sys.exit(f"Could not create the Claude client: {e}\nSet ANTHROPIC_API_KEY in .env.")
        engine = make_engine(args.db)
        init_db(engine)
        state = BotState(make_session_factory(engine), Agent(client, args.model),
                         settings.timezone, args.as_of)
        app = build_application(token, state, int(settings.telegram_chat_id))
        print(f"LoadWise bot running ({args.model}, {engine.url.database}"
              + (f", today = {args.as_of}" if args.as_of else "") + "). Ctrl+C to stop.")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
