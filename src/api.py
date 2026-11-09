"""HTTP API: health check, metrics, flags, and session proposals.

One process runs everything: uvicorn serves this FastAPI app, and the app's lifespan
starts the Telegram bot (polling) and its scheduler on the same event loop. That is why the
bot is started by hand here (initialize/start/start_polling) instead of run_polling(),
which would want to own the event loop itself.

Security: the API holds health data. It listens on 127.0.0.1 by default, and every
endpoint except /health needs the header X-API-Key matching API_KEY from .env. Without a
usable API_KEY those endpoints refuse every request (closed by default, not open).

POST /sessions does not save anything: like the agent and /log, it creates a proposal and
sends Confirm/Reject buttons to Telegram. confirm_action() stays the only save path.
"""

import datetime as dt
import secrets
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import Application

from src.bot import BotState, proposal_buttons, proposal_text, set_command_menu
from src.tools import ToolContext, ToolError, get_active_flags, get_load_summary, log_session

MIN_API_KEY_LENGTH = 16  # also rejects the short .env.example placeholder


class SessionIn(BaseModel):
    date: dt.date | None = None  # default: today (dt.date: a field named `date` would shadow the type)
    sport_type: str
    duration_min: int
    rpe: int
    notes: str | None = None


def create_app(state: BotState, api_key: str | None, telegram: Application | None = None) -> FastAPI:
    """`telegram` is the bot application; None runs the API alone (tests, no Telegram)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if telegram:
            await telegram.initialize()
            await set_command_menu(telegram)
            await telegram.start()  # also starts the JobQueue (daily check)
            await telegram.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        yield
        if telegram:
            await telegram.updater.stop()
            await telegram.stop()
            await telegram.shutdown()

    app = FastAPI(title="LoadWise API", lifespan=lifespan)

    def get_db():
        with state.session_factory() as db:
            yield db

    def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        if not api_key or len(api_key) < MIN_API_KEY_LENGTH:
            raise HTTPException(503, "API_KEY is not configured on the server")
        # compare_digest takes the same time for any mismatch, so the key can't be guessed by timing
        if x_api_key is None or not secrets.compare_digest(x_api_key, api_key):
            raise HTTPException(401, "Missing or wrong X-API-Key header")

    def context(db: Session, as_of: date | None = None) -> ToolContext:
        return ToolContext(db=db, today=as_of or state.today())

    @app.get("/health")
    def health(db: Session = Depends(get_db)) -> dict:
        try:
            db.execute(text("SELECT 1"))
            database = "ok"
        except Exception:
            database = "error"
        bot = "disabled" if telegram is None else ("running" if telegram.running else "stopped")
        last_check = telegram.bot_data.get("last_check") if telegram else None
        return {"status": "ok" if database == "ok" else "degraded", "database": database,
                "bot": bot, "last_check": last_check}

    @app.get("/metrics", dependencies=[Depends(require_api_key)])
    def metrics(as_of: date | None = None, days: int = 7, db: Session = Depends(get_db)) -> dict:
        try:
            return get_load_summary(context(db, as_of), days=days)
        except ToolError as e:
            raise HTTPException(422, str(e))

    @app.get("/flags", dependencies=[Depends(require_api_key)])
    def flags(as_of: date | None = None, db: Session = Depends(get_db)) -> dict:
        return get_active_flags(context(db, as_of))

    @app.post("/sessions", status_code=202, dependencies=[Depends(require_api_key)])
    async def propose_session(body: SessionIn, db: Session = Depends(get_db)) -> dict:
        ctx = context(db)
        try:
            result = log_session(ctx, (body.date or ctx.today).isoformat(), body.sport_type,
                                 body.duration_min, body.rpe, body.notes)
        except ToolError as e:
            db.rollback()
            raise HTTPException(422, str(e))
        telegram_sent = False
        if telegram:
            chat_id = telegram.bot_data["allowed_chat_id"]
            await telegram.bot.send_message(chat_id, proposal_text(result["summary"]),
                                            reply_markup=proposal_buttons(result["pending_action_id"]))
            telegram_sent = True
        return {**result, "telegram_sent": telegram_sent}

    return app
