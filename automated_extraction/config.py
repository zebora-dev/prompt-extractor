from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .chatgpt_auth import AccountsDeserializer


DEFAULT_API_BASE_URL = "https://hmwgplzdzffivawkflci.supabase.co/functions/v1/api"
DEFAULT_PROMPT_OUTPUTS_TABLE = "prompts_outputs"
DEFAULT_PROMPT_OUTPUT_PRODUCTS_TABLE = "prompts_outputs_products"
DEFAULT_PROMPT_OUTPUT_ENTITIES_TABLE = "prompts_outputs_entities"
DEFAULT_SCORE_WORKFLOW_URL = "https://workflow.zebora.io/api/workflows/score-single-output"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHROME_USER_DATA_DIR = PROJECT_ROOT / ".chrome-profile"
DEFAULT_LOGGED_IN_ACCOUNTS_DIR = PROJECT_ROOT / ".chrome-accounts"


@dataclass(frozen=True)
class Settings:
    api_base_url: str
    supabase_url: str
    anon_key: str
    prompt_outputs_table: str
    prompt_output_products_table: str
    prompt_output_entities_table: str
    chatgpt_url: str
    chrome_user_data_dir: str | None
    logged_in_accounts_dir: str
    headless: bool
    login_wait_seconds: int
    response_timeout_seconds: int
    sources_panel_pause_seconds: int
    score_workflow_url: str
    workflow_api_key: str | None
    score_workflow_force_run: bool
    score_workflow_scorer_types: list[str]
    auto_login: bool
    login_email: str | None
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_env(cls, *, require_api_key: bool = True, require_auto_login_credentials: bool | None = None) -> "Settings":
        load_dotenv_if_available()

        anon_key = os.getenv("BRANDSIGHT_SUPABASE_ANON_KEY", "").strip()
        if require_api_key and not anon_key:
            raise RuntimeError(
                "Missing BRANDSIGHT_SUPABASE_ANON_KEY. Copy .env.example to .env and set the key from chromeApp/extension-shared/background.js."
            )

        auto_login = parse_bool(os.getenv("CHATGPT_AUTO_LOGIN"), default=False)
        login_email = (os.getenv("CHATGPT_LOGIN_EMAIL") or "").strip() or None
        accounts = AccountsDeserializer(os.getenv("CHATGPT_ACCOUNTS_B64")).get_all_accounts()

        # Default: enforce auto-login credentials whenever require_api_key is true
        # (i.e. real extraction runs). Callers like `--login-only` set
        # require_auto_login_credentials=False so they can warm the manual flow
        # without configuring CHATGPT_ACCOUNTS_B64.
        if require_auto_login_credentials is None:
            require_auto_login_credentials = require_api_key
        if auto_login and require_auto_login_credentials:
            if not login_email:
                raise RuntimeError(
                    "CHATGPT_AUTO_LOGIN=true but CHATGPT_LOGIN_EMAIL is not set."
                )
            if not accounts:
                raise RuntimeError(
                    "CHATGPT_AUTO_LOGIN=true but CHATGPT_ACCOUNTS_B64 is empty or not set."
                )
            if login_email not in accounts:
                raise RuntimeError(
                    f"CHATGPT_LOGIN_EMAIL={login_email!r} is not present in CHATGPT_ACCOUNTS_B64. "
                    f"Available emails: {sorted(accounts.keys())}"
                )

        return cls(
            api_base_url=os.getenv("BRANDSIGHT_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/"),
            supabase_url=resolve_supabase_url(
                os.getenv("BRANDSIGHT_SUPABASE_URL"),
                os.getenv("BRANDSIGHT_API_BASE_URL", DEFAULT_API_BASE_URL),
            ),
            anon_key=anon_key,
            prompt_outputs_table=os.getenv("BRANDSIGHT_PROMPT_OUTPUTS_TABLE", DEFAULT_PROMPT_OUTPUTS_TABLE).strip()
            or DEFAULT_PROMPT_OUTPUTS_TABLE,
            prompt_output_products_table=os.getenv(
                "BRANDSIGHT_PROMPT_OUTPUT_PRODUCTS_TABLE", DEFAULT_PROMPT_OUTPUT_PRODUCTS_TABLE
            ).strip()
            or DEFAULT_PROMPT_OUTPUT_PRODUCTS_TABLE,
            prompt_output_entities_table=os.getenv(
                "BRANDSIGHT_PROMPT_OUTPUT_ENTITIES_TABLE", DEFAULT_PROMPT_OUTPUT_ENTITIES_TABLE
            ).strip()
            or DEFAULT_PROMPT_OUTPUT_ENTITIES_TABLE,
            chatgpt_url=os.getenv("CHATGPT_URL", "https://chatgpt.com").strip(),
            chrome_user_data_dir=os.getenv("CHATGPT_CHROME_USER_DATA_DIR") or str(DEFAULT_CHROME_USER_DATA_DIR),
            logged_in_accounts_dir=os.getenv("CHATGPT_LOGGED_IN_ACCOUNTS_DIR")
            or str(DEFAULT_LOGGED_IN_ACCOUNTS_DIR),
            headless=parse_bool(os.getenv("CHATGPT_HEADLESS"), default=False),
            login_wait_seconds=parse_int(os.getenv("CHATGPT_LOGIN_WAIT_SECONDS"), default=180),
            response_timeout_seconds=parse_int(os.getenv("CHATGPT_RESPONSE_TIMEOUT_SECONDS"), default=300),
            sources_panel_pause_seconds=parse_int(os.getenv("CHATGPT_SOURCES_PANEL_PAUSE_SECONDS"), default=0),
            score_workflow_url=os.getenv("BRANDSIGHT_SCORE_WORKFLOW_URL", DEFAULT_SCORE_WORKFLOW_URL).strip()
            or DEFAULT_SCORE_WORKFLOW_URL,
            workflow_api_key=os.getenv("WORKFLOW_API_KEY", "").strip() or None,
            score_workflow_force_run=parse_bool(os.getenv("BRANDSIGHT_SCORE_WORKFLOW_FORCE_RUN"), default=False),
            score_workflow_scorer_types=parse_csv(os.getenv("BRANDSIGHT_SCORE_WORKFLOW_SCORER_TYPES")),
            auto_login=auto_login,
            login_email=login_email,
            accounts=accounts,
        )


def resolve_supabase_url(explicit_url: str | None, api_base_url: str) -> str:
    if explicit_url and explicit_url.strip():
        return explicit_url.strip().rstrip("/")

    parsed = urlsplit(api_base_url)
    if parsed.scheme and parsed.netloc:
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")

    return api_base_url.rstrip("/").replace("/functions/v1/api", "")


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


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def resolve_logged_in_account_profile(settings: Settings, account_name: str) -> str:
    safe_name = sanitize_profile_name(account_name)
    return str(Path(settings.logged_in_accounts_dir).expanduser() / safe_name)


def sanitize_profile_name(value: str) -> str:
    normalized = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value.strip().lower())
    normalized = "-".join(part for part in normalized.split("-") if part)
    return normalized or "default"
