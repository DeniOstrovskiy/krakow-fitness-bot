from __future__ import annotations

from dataclasses import dataclass
import os
import re
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

_DEFAULT_CLUB_PREFIX = "MyFitnessPlace"
_DEFAULT_TIMEZONE = "Europe/Warsaw"
_DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; TgScheduleBot/1.0; +https://t.me/)"


@dataclass(frozen=True)
class ClubSchedule:
    name: str
    url: str
    selector: str | None


@dataclass(frozen=True)
class Config:
    bot_token: str
    timezone: ZoneInfo
    clubs: list[ClubSchedule]
    max_results: int
    user_agent: str
    log_level: str
    use_playwright: bool
    playwright_wait_selector: str | None
    playwright_timeout_s: int
    playwright_headless: bool
    playwright_seek_week: bool
    playwright_max_steps: int
    webhook_base_url: str | None
    webhook_path: str
    webhook_listen_host: str
    webhook_listen_port: int
    drop_pending_updates: bool


def load_config() -> Config:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    schedule_url = os.getenv("SCHEDULE_URL", "").strip()
    schedule_urls = os.getenv("SCHEDULE_URLS", "").strip()
    timezone_name = os.getenv("TIMEZONE", _DEFAULT_TIMEZONE).strip()
    club_name_raw = os.getenv("CLUB_NAME")
    club_name = (club_name_raw or _DEFAULT_CLUB_PREFIX).strip()
    club_names = os.getenv("CLUB_NAMES", "").strip()
    max_results = int(os.getenv("MAX_RESULTS", "20"))
    user_agent = os.getenv("USER_AGENT", _DEFAULT_USER_AGENT).strip()
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    event_selector = os.getenv("EVENT_SELECTOR", "").strip() or None
    event_selectors = os.getenv("EVENT_SELECTORS", "").strip()
    use_playwright = _parse_bool(os.getenv("USE_PLAYWRIGHT", "1"))
    playwright_wait_selector = os.getenv("PLAYWRIGHT_WAIT_SELECTOR", "").strip() or None
    playwright_timeout_s = int(os.getenv("PLAYWRIGHT_TIMEOUT_S", "25"))
    playwright_headless = _parse_bool(os.getenv("PLAYWRIGHT_HEADLESS", "1"))
    playwright_seek_week = _parse_bool(os.getenv("PLAYWRIGHT_SEEK_WEEK", "1"))
    playwright_max_steps = int(os.getenv("PLAYWRIGHT_MAX_STEPS", "12"))
    webhook_url_raw = os.getenv("WEBHOOK_URL", "").strip()
    render_external_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    webhook_path_env = os.getenv("WEBHOOK_PATH", "").strip()
    webhook_listen_host = os.getenv("WEBHOOK_LISTEN", "0.0.0.0").strip()
    webhook_listen_port = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", "8080")))
    drop_pending_updates = _parse_bool(os.getenv("DROP_PENDING_UPDATES", "1"))

    if not bot_token:
        raise ValueError("BOT_TOKEN is required")

    urls = _split_env_list(schedule_urls) if schedule_urls else []
    if not urls:
        if not schedule_url:
            raise ValueError("SCHEDULE_URL or SCHEDULE_URLS is required")
        urls = [schedule_url]

    names = _split_env_list(club_names) if club_names else []
    if not names:
        if club_name_raw and club_name:
            names = [club_name]
        elif len(urls) == 1:
            names = [club_name]

    selectors = _split_env_list(event_selectors) if event_selectors else []
    if not selectors and event_selector:
        selectors = [event_selector]

    names = _align_or_generate_names(names, urls)
    selectors = _align_optional_list(selectors, len(urls), "EVENT_SELECTORS")

    clubs = [
        ClubSchedule(name=name, url=url, selector=selector)
        for name, url, selector in zip(names, urls, selectors)
    ]

    webhook_base_url, webhook_path = _resolve_webhook(
        webhook_url_raw, render_external_url, webhook_path_env
    )

    return Config(
        bot_token=bot_token,
        timezone=ZoneInfo(timezone_name),
        clubs=clubs,
        max_results=max_results,
        user_agent=user_agent,
        log_level=log_level,
        use_playwright=use_playwright,
        playwright_wait_selector=playwright_wait_selector,
        playwright_timeout_s=playwright_timeout_s,
        playwright_headless=playwright_headless,
        playwright_seek_week=playwright_seek_week,
        playwright_max_steps=playwright_max_steps,
        webhook_base_url=webhook_base_url,
        webhook_path=webhook_path,
        webhook_listen_host=webhook_listen_host,
        webhook_listen_port=webhook_listen_port,
        drop_pending_updates=drop_pending_updates,
    )


def _split_env_list(value: str) -> list[str]:
    parts = re.split(r"[|,;\n]+", value)
    return [part.strip() for part in parts if part.strip()]


def _align_optional_list(values: list[str], target_len: int, var_name: str) -> list[str | None]:
    if not values:
        return [None] * target_len
    if len(values) == 1 and target_len > 1:
        return [values[0]] * target_len
    if len(values) != target_len:
        raise ValueError(f"{var_name} must have 1 value or match SCHEDULE_URLS length")
    return values


def _align_or_generate_names(names: list[str], urls: list[str]) -> list[str]:
    if not names:
        return [_derive_name_from_url(url) for url in urls]
    if len(names) == 1 and len(urls) > 1:
        return [names[0]] * len(urls)
    if len(names) != len(urls):
        raise ValueError("CLUB_NAMES must have 1 value or match SCHEDULE_URLS length")
    return names


def _derive_name_from_url(url: str) -> str:
    """Extract a human-readable club name from a schedule URL path."""
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "grafik-zajec" in parts:
        idx = parts.index("grafik-zajec")
        slug = parts[idx - 1] if idx > 0 else parts[-1]
    else:
        slug = parts[-1] if parts else "Schedule"
    name = slug.replace("-", " ").title()
    return f"{_DEFAULT_CLUB_PREFIX} {name}"


def _parse_bool(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _resolve_webhook(
    webhook_url_raw: str,
    render_external_url: str,
    webhook_path_env: str,
) -> tuple[str | None, str]:
    base_url = ""
    path = ""
    if webhook_url_raw:
        parsed = urlparse(webhook_url_raw)
        if parsed.scheme and parsed.netloc:
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            path = parsed.path or ""
        else:
            base_url = webhook_url_raw
    elif render_external_url:
        base_url = render_external_url

    if not path:
        path = webhook_path_env or "/telegram"

    if not path.startswith("/"):
        path = f"/{path}"

    return (base_url.strip() or None), path
