from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


KNOWN_TRUTHSOCIAL_ACCOUNT_IDS = {
    "realdonaldtrump": "107780257626128497",
}


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env_file(path: str | os.PathLike[str]) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_quotes(value.strip())
        os.environ.setdefault(key, value)


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _get_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    parts = tuple(part.strip() for part in value.split(",") if part.strip())
    return parts or default


def _get_rule_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    parts = tuple(part.strip() for part in value.split(";") if part.strip())
    return parts or default


def _normalize_truthsocial_auth_mode(value: str | None) -> str:
    normalized = (value or "auto").strip().lower()
    if normalized in {"auto", "public", "cookies"}:
        return normalized
    return "auto"


def _default_truthsocial_account_id(handle: str) -> str | None:
    return KNOWN_TRUTHSOCIAL_ACCOUNT_IDS.get(handle.strip().lower()) or None


@dataclass(slots=True)
class AppConfig:
    telegram_bot_token: str
    telegram_chat_id: str
    source_chat_routes: tuple[str, ...]
    source_keyword_filters: tuple[str, ...]
    source_category_filters: tuple[str, ...]
    enabled_sources: tuple[str, ...]
    rss_feed_urls: tuple[str, ...]
    truthsocial_handle: str
    truthsocial_account_id: str | None
    truthsocial_base_url: str
    truthsocial_cookies_file: Path | None
    truthsocial_reload_cookies: bool
    poll_interval_seconds: int
    request_timeout_seconds: int
    state_db_path: Path
    bootstrap_latest_only: bool
    initial_history_limit: int
    fetch_limit: int
    exclude_replies: bool
    exclude_reblogs: bool
    user_agent: str
    log_level: str
    telegram_alert_chat_id: str = ""
    source_failure_alert_threshold: int = 3
    source_retry_attempts: int = 3
    source_retry_backoff_seconds: int = 2
    continue_on_source_error: bool = True
    truthsocial_auth_mode: str = "auto"
    translation_target_language: str = "vi"
    translation_endpoint: str = "https://translate.googleapis.com/translate_a/single"
    translation_retry_attempts: int = 3
    translation_retry_backoff_seconds: int = 1
    translation_failure_placeholder: str = "Ban dich tam thoi chua san sang."
    image_summary_enabled: bool = False
    image_summary_provider: str = "openai"
    image_summary_model: str = "gpt-4.1-mini"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

    @classmethod
    def from_env(cls, env_file: str = ".env") -> "AppConfig":
        load_env_file(env_file)

        truthsocial_handle = os.getenv("TRUTHSOCIAL_HANDLE", "realDonaldTrump").strip()
        cookies_value = os.getenv("TRUTHSOCIAL_COOKIES_FILE", "").strip()
        account_id = (
            os.getenv("TRUTHSOCIAL_ACCOUNT_ID", "").strip()
            or _default_truthsocial_account_id(truthsocial_handle)
        )
        truthsocial_auth_mode = _normalize_truthsocial_auth_mode(
            os.getenv("TRUTHSOCIAL_AUTH_MODE")
        )

        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            telegram_alert_chat_id=os.getenv("TELEGRAM_ALERT_CHAT_ID", "").strip(),
            source_chat_routes=_get_rule_list("SOURCE_CHAT_ROUTES", ()),
            source_keyword_filters=_get_rule_list("SOURCE_KEYWORD_FILTERS", ()),
            source_category_filters=_get_rule_list("SOURCE_CATEGORY_FILTERS", ()),
            enabled_sources=_get_list("ENABLED_SOURCES", ("truthsocial_trump",)),
            rss_feed_urls=_get_list("RSS_FEED_URLS", ()),
            truthsocial_handle=truthsocial_handle,
            truthsocial_account_id=account_id,
            truthsocial_base_url=os.getenv(
                "TRUTHSOCIAL_BASE_URL", "https://truthsocial.com"
            ).rstrip("/"),
            truthsocial_cookies_file=Path(cookies_value) if cookies_value else None,
            truthsocial_reload_cookies=_get_bool("TRUTHSOCIAL_RELOAD_COOKIES", True),
            poll_interval_seconds=_get_int("POLL_INTERVAL_SECONDS", 60),
            request_timeout_seconds=_get_int("REQUEST_TIMEOUT_SECONDS", 20),
            state_db_path=Path(os.getenv("STATE_DB_PATH", "data/news_bot.sqlite3")),
            bootstrap_latest_only=_get_bool("BOOTSTRAP_LATEST_ONLY", True),
            initial_history_limit=_get_int("INITIAL_HISTORY_LIMIT", 5),
            fetch_limit=_get_int("FETCH_LIMIT", 10),
            exclude_replies=_get_bool("EXCLUDE_REPLIES", False),
            exclude_reblogs=_get_bool("EXCLUDE_REBLOGS", False),
            user_agent=os.getenv(
                "USER_AGENT",
                (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/135.0 Safari/537.36"
                ),
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip() or "INFO",
            source_failure_alert_threshold=max(0, _get_int("SOURCE_FAILURE_ALERT_THRESHOLD", 3)),
            source_retry_attempts=max(1, _get_int("SOURCE_RETRY_ATTEMPTS", 3)),
            source_retry_backoff_seconds=max(0, _get_int("SOURCE_RETRY_BACKOFF_SECONDS", 2)),
            continue_on_source_error=_get_bool("CONTINUE_ON_SOURCE_ERROR", True),
            truthsocial_auth_mode=truthsocial_auth_mode,
            translation_target_language=os.getenv("TRANSLATION_TARGET_LANGUAGE", "vi").strip(),
            translation_endpoint=os.getenv(
                "TRANSLATION_ENDPOINT",
                "https://translate.googleapis.com/translate_a/single",
            ).strip(),
            translation_retry_attempts=max(1, _get_int("TRANSLATION_RETRY_ATTEMPTS", 3)),
            translation_retry_backoff_seconds=max(
                0, _get_int("TRANSLATION_RETRY_BACKOFF_SECONDS", 1)
            ),
            translation_failure_placeholder=(
                os.getenv(
                    "TRANSLATION_FAILURE_PLACEHOLDER",
                    "Ban dich tam thoi chua san sang.",
                ).strip()
                or "Ban dich tam thoi chua san sang."
            ),
            image_summary_enabled=_get_bool("IMAGE_SUMMARY_ENABLED", False),
            image_summary_provider=os.getenv("IMAGE_SUMMARY_PROVIDER", "openai").strip()
            or "openai",
            image_summary_model=os.getenv("IMAGE_SUMMARY_MODEL", "gpt-4.1-mini").strip()
            or "gpt-4.1-mini",
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
            or "https://api.openai.com/v1",
        )
