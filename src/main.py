"""Run everything in one process: HTTP API + Telegram bot + daily check.

  python -m src.main                                        # real data (DB_PATH from .env)
  python -m src.main --db data/demo.db --as-of 2026-02-15   # demo data on a fixed date

The first time, run `python -m src.bot` once to get your Telegram chat id (setup mode).
"""

import argparse
import logging
import sys
from datetime import date

import anthropic
import uvicorn

from src.agent import Agent
from src.api import create_app
from src.bot import BotState, build_application, is_setup_mode
from src.config import get_settings
from src.db import init_db, make_engine, make_session_factory


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Run the LoadWise API, Telegram bot and daily check.")
    parser.add_argument("--db", help="SQLite file (default: DB_PATH from .env)")
    parser.add_argument("--as-of", type=date.fromisoformat,
                        help="Pretend today is this date (for demo data), YYYY-MM-DD")
    parser.add_argument("--model", default=settings.llm_model)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = settings.telegram_bot_token
    if not token or ":" not in token:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env (create a bot with @BotFather in Telegram).")
    if is_setup_mode(settings.telegram_chat_id):
        sys.exit("Set TELEGRAM_CHAT_ID in .env first: run `python -m src.bot` and message your bot to get it.")
    try:
        client = anthropic.Anthropic()
    except anthropic.AnthropicError as e:
        sys.exit(f"Could not create the Claude client: {e}\nSet ANTHROPIC_API_KEY in .env.")

    engine = make_engine(args.db)
    init_db(engine)
    state = BotState(make_session_factory(engine), Agent(client, args.model), settings.timezone, args.as_of)
    telegram = build_application(token, state, int(settings.telegram_chat_id))
    app = create_app(state, settings.api_key, telegram)

    print(f"LoadWise on {engine.url.database} ({args.model}"
          + (f", today = {args.as_of}" if args.as_of else "")
          + f"). API: http://{settings.api_host}:{settings.api_port}/health  Ctrl+C to stop.")
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
