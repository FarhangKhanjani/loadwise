"""Settings loaded from environment variables (and a local .env file, if present).

Secrets never live in code: everything comes from the environment.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    db_path: str
    anthropic_api_key: str | None
    llm_model: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    timezone: str


def get_settings() -> Settings:
    return Settings(
        db_path=os.getenv("DB_PATH", "data/loadwise.db"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        llm_model=os.getenv("LLM_MODEL") or "claude-haiku-5-5",
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
        timezone=os.getenv("TIMEZONE", "Europe/Vienna"),
    )
