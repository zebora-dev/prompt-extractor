from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_API_BASE_URL = "https://hmwgplzdzffivawkflci.supabase.co/functions/v1/api"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHROME_USER_DATA_DIR = PROJECT_ROOT / ".chrome-profile"


@dataclass(frozen=True)
class Settings:
    api_base_url: str
    anon_key: str
    chatgpt_url: str
    chrome_user_data_dir: str | None
    headless: bool
    login_wait_seconds: int
    response_timeout_seconds: int
    sources_panel_pause_seconds: int

    @classmethod
    def from_env(cls, *, require_api_key: bool = True) -> "Settings":
        load_dotenv_if_available()

        anon_key = os.getenv("BRANDSIGHT_SUPABASE_ANON_KEY", "").strip()
        if require_api_key and not anon_key:
            raise RuntimeError(
                "Missing BRANDSIGHT_SUPABASE_ANON_KEY. Copy .env.example to .env and set the key from chromeApp/extension-shared/background.js."
            )

        return cls(
            api_base_url=os.getenv("BRANDSIGHT_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/"),
            anon_key=anon_key,
            chatgpt_url=os.getenv("CHATGPT_URL", "https://chatgpt.com").strip(),
            chrome_user_data_dir=os.getenv("CHATGPT_CHROME_USER_DATA_DIR") or str(DEFAULT_CHROME_USER_DATA_DIR),
            headless=parse_bool(os.getenv("CHATGPT_HEADLESS"), default=False),
            login_wait_seconds=parse_int(os.getenv("CHATGPT_LOGIN_WAIT_SECONDS"), default=180),
            response_timeout_seconds=parse_int(os.getenv("CHATGPT_RESPONSE_TIMEOUT_SECONDS"), default=300),
            sources_panel_pause_seconds=parse_int(os.getenv("CHATGPT_SOURCES_PANEL_PAUSE_SECONDS"), default=0),
        )


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv()


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default
