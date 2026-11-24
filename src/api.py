"""HTTP API: health check, metrics, flags, proposals, the calorie tracker, and the phone app.

One process runs everything: uvicorn serves this FastAPI app, and the app's lifespan
starts the Telegram bot (polling) and its scheduler on the same event loop. That is why the
bot is started by hand here (initialize/start/start_polling) instead of run_polling(),
which would want to own the event loop itself.

Security: the API holds health data. It listens on 127.0.0.1 by default, and every
endpoint except /health needs the header X-API-Key matching API_KEY from .env. Without a
usable API_KEY those endpoints refuse every request (closed by default, not open).

POST /sessions and POST /wellness do not save anything: like the agent and /log, they create
a proposal. It is confirmed either with the Telegram buttons or, from the phone app, with
POST /actions/{id}/confirm. Both end in confirm_action(), which stays the only save path.

The phone app (Phase 9) is plain HTML/CSS/JS in web/, served at /app. Those files hold no
data, so they need no key; the app sends X-API-Key on every data request.
"""

import datetime as dt
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import Application

from src import food_log
from src.bot import BotState, proposal_buttons, proposal_text, set_command_menu
from src.models import Food, MealTemplate, PendingAction
from src.nutrition import SLOTS, NutritionError, search, suggested_slot
from src.tools import (
    SPORT_TYPES,
    ToolContext,
    ToolError,
    confirm_action,
    describe_payload,
    get_active_flags,
    get_load_summary,
    get_sessions,
    log_session,
    log_wellness,
    reject_action,
)

MIN_API_KEY_LENGTH = 16  # also rejects the short .env.example placeholder
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# The app only loads its own files: no inline scripts, nothing from other sites. If a food
# name or note ever contained HTML, the browser would refuse to run it.
APP_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class FoodIn(BaseModel):
    date: dt.date | None = None  # default: today
    slot: str
    food: str
    grams: float | None = None   # either grams...
    portion: str | None = None   # ...or a portion name and a count
    count: float = 1


class MealIn(BaseModel):
    date: dt.date | None = None
    slot: str | None = None      # default: the meal's own slot
    meal: str
    include_optional: list[str] = []
    grams: dict[str, float] = {}  # change a food's amount; 0 leaves it out


class FoodPreviewIn(BaseModel):
    food: str
    grams: float | None = None
    portion: str | None = None
    count: float = 1


class SessionIn(BaseModel):
    date: dt.date | None = None  # default: today (dt.date: a field named `date` would shadow the type)
    sport_type: str
    duration_min: int
    rpe: int
    notes: str | None = None


class WellnessIn(BaseModel):
    date: dt.date | None = None
    sleep_hours: float
    soreness: int
    fatigue: int
    stress: int
    resting_hr: int | None = None
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

    @app.middleware("http")
    async def app_security_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/app"):
            response.headers.update(APP_HEADERS)
        return response

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

    @app.get("/meta", dependencies=[Depends(require_api_key)])
    def meta() -> dict:
        """What the app needs before it can show a form: today's date in the server's time
        zone (the phone's clock may differ), the choices, and the meal slot for this hour."""
        return {"today": state.today().isoformat(), "sports": SPORT_TYPES, "slots": list(SLOTS),
                "suggested_slot": suggested_slot(datetime.now(ZoneInfo(state.timezone)).hour)}

    @app.get("/sessions/recent", dependencies=[Depends(require_api_key)])
    def recent_sessions(days: int = 14, db: Session = Depends(get_db)) -> dict:
        if not 1 <= days <= 56:
            raise HTTPException(422, "days must be between 1 and 56")
        today = state.today()
        result = get_sessions(context(db), (today - dt.timedelta(days=days - 1)).isoformat(),
                              today.isoformat())
        result["sessions"].reverse()  # newest first
        return result

    # ---------- calorie tracker ----------

    def nutrition_error(e: NutritionError) -> HTTPException:
        return HTTPException(422, str(e))

    @app.get("/foods", dependencies=[Depends(require_api_key)])
    def foods(q: str = "", db: Session = Depends(get_db)) -> list[dict]:
        all_foods = list(db.scalars(select(Food).order_by(Food.name)))
        found = search(q, all_foods, limit=20) if q.strip() else all_foods
        return [{"name": f.name, "kcal_per_100g": f.kcal_per_100g, "protein_per_100g": f.protein_per_100g,
                 "portions": f.portions, "aliases": f.aliases, "source": f.source} for f in found]

    @app.get("/meals", dependencies=[Depends(require_api_key)])
    def meals(db: Session = Depends(get_db)) -> list[dict]:
        out = []
        for m in db.scalars(select(MealTemplate).order_by(MealTemplate.slot, MealTemplate.name)):
            _, lines = food_log.preview_meal(db, m.name)
            out.append({"name": m.name, "slot": m.slot, "items": m.items,
                        "optional": [i["food"] for i in m.items if i.get("optional")],
                        "default": food_log.lines_dict(lines)})
        return out

    @app.post("/food-log/preview", dependencies=[Depends(require_api_key)])
    def preview_food(body: FoodPreviewIn, db: Session = Depends(get_db)) -> dict:
        try:
            line = food_log.preview_food(db, body.food, body.grams, body.portion, body.count)
        except NutritionError as e:
            raise nutrition_error(e)
        return line.__dict__

    @app.post("/food-log", status_code=201, dependencies=[Depends(require_api_key)])
    def log_food(body: FoodIn, db: Session = Depends(get_db)) -> dict:
        try:
            entry = food_log.log_food(db, body.date or state.today(), body.slot, body.food,
                                      body.grams, body.portion, body.count)
        except NutritionError as e:
            raise nutrition_error(e)
        return food_log.entry_dict(entry)

    @app.post("/food-log/meal/preview", dependencies=[Depends(require_api_key)])
    def preview_meal(body: MealIn, db: Session = Depends(get_db)) -> dict:
        try:
            meal, lines = food_log.preview_meal(db, body.meal, body.include_optional, body.grams)
        except NutritionError as e:
            raise nutrition_error(e)
        return {"meal": meal.name, **food_log.lines_dict(lines)}

    @app.post("/food-log/meal", status_code=201, dependencies=[Depends(require_api_key)])
    def log_meal(body: MealIn, db: Session = Depends(get_db)) -> dict:
        try:
            entries = food_log.log_meal(db, body.date or state.today(), body.slot, body.meal,
                                        body.include_optional, body.grams)
        except NutritionError as e:
            raise nutrition_error(e)
        return {"entries": [food_log.entry_dict(e) for e in entries],
                "kcal": round(sum(e.kcal for e in entries), 1),
                "protein": round(sum(e.protein for e in entries), 1)}

    @app.delete("/food-log/{entry_id}", dependencies=[Depends(require_api_key)])
    def delete_food(entry_id: int, whole_meal: bool = False, db: Session = Depends(get_db)) -> dict:
        try:
            return {"deleted": food_log.delete_entries(db, entry_id, whole_meal)}
        except NutritionError as e:
            raise HTTPException(404, str(e))

    @app.get("/nutrition/day", dependencies=[Depends(require_api_key)])
    def nutrition_day(date: date | None = None, db: Session = Depends(get_db)) -> dict:
        return food_log.day(db, date or state.today())

    @app.get("/nutrition/month", dependencies=[Depends(require_api_key)])
    def nutrition_month(month: str | None = None, db: Session = Depends(get_db)) -> dict:
        today = state.today()
        try:
            year, month_ = (int(p) for p in month.split("-")) if month else (today.year, today.month)
            dt.date(year, month_, 1)
        except ValueError:
            raise HTTPException(422, "month must be YYYY-MM")
        return food_log.month(db, year, month_, today)

    # ---------- proposals: training sessions and wellness ----------

    async def send_to_telegram(result: dict, notify_telegram: bool) -> bool:
        """notify_telegram=false: the phone app confirms the proposal itself, so Telegram
        doesn't also get buttons for it."""
        if not (telegram and notify_telegram):
            return False
        chat_id = telegram.bot_data["allowed_chat_id"]
        await telegram.bot.send_message(chat_id, proposal_text(result["summary"]),
                                        reply_markup=proposal_buttons(result["pending_action_id"]))
        return True

    @app.post("/sessions", status_code=202, dependencies=[Depends(require_api_key)])
    async def propose_session(body: SessionIn, notify_telegram: bool = True,
                              db: Session = Depends(get_db)) -> dict:
        ctx = context(db)
        try:
            result = log_session(ctx, (body.date or ctx.today).isoformat(), body.sport_type,
                                 body.duration_min, body.rpe, body.notes)
        except ToolError as e:
            db.rollback()
            raise HTTPException(422, str(e))
        return {**result, "telegram_sent": await send_to_telegram(result, notify_telegram)}

    @app.post("/wellness", status_code=202, dependencies=[Depends(require_api_key)])
    async def propose_wellness(body: WellnessIn, notify_telegram: bool = True,
                               db: Session = Depends(get_db)) -> dict:
        ctx = context(db)
        try:
            result = log_wellness(ctx, (body.date or ctx.today).isoformat(), body.sleep_hours,
                                  body.soreness, body.fatigue, body.stress, body.resting_hr, body.notes)
        except ToolError as e:
            db.rollback()
            raise HTTPException(422, str(e))
        return {**result, "telegram_sent": await send_to_telegram(result, notify_telegram)}

    def decide_action(db: Session, action_id: int, confirm: bool) -> dict:
        """Same path as the Telegram buttons: confirm_action() is the only way to save."""
        action = db.get(PendingAction, action_id)
        if action is None:
            raise HTTPException(404, f"pending action {action_id} does not exist")
        summary = describe_payload(action.action_type, action.payload)
        try:
            if confirm:
                result = confirm_action(db, action_id)
            else:
                reject_action(db, action_id)
                result = "rejected"
        except ToolError as e:  # already confirmed or rejected, e.g. in Telegram
            db.rollback()
            raise HTTPException(409, str(e))
        except IntegrityError:  # e.g. wellness for that day was saved in the meantime
            db.rollback()
            raise HTTPException(409, f"conflicts with data already logged: {summary}")
        return {"id": action_id, "status": "confirmed" if confirm else "rejected",
                "result": result, "summary": summary}

    @app.post("/actions/{action_id}/confirm", dependencies=[Depends(require_api_key)])
    def confirm(action_id: int, db: Session = Depends(get_db)) -> dict:
        return decide_action(db, action_id, confirm=True)

    @app.post("/actions/{action_id}/reject", dependencies=[Depends(require_api_key)])
    def reject(action_id: int, db: Session = Depends(get_db)) -> dict:
        return decide_action(db, action_id, confirm=False)

    # ---------- the phone app ----------

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/app/")

    if WEB_DIR.is_dir():
        app.mount("/app", StaticFiles(directory=WEB_DIR, html=True), name="app")

    return app
