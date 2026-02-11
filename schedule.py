from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import asyncio
import logging
import re
import unicodedata
from typing import Iterable, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Compiled regex patterns
# ---------------------------------------------------------------------------
TIME_RE = re.compile(r"\b([01]\d|2[0-3])[:.][0-5]\d\b")
DATE_RE_NUM = re.compile(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}))?\b")
DATE_RE_WORD = re.compile(r"\b(\d{1,2})\s+([A-Za-z\u00C0-\u017F]+)\b")
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
ISO_DATETIME_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})")
UNIX_TS_RE = re.compile(r"\b(\d{10}|\d{13})\b")
CAPACITY_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

# ---------------------------------------------------------------------------
# Parsing limits
# ---------------------------------------------------------------------------
_MAX_EVENT_TEXT_LEN = 220
_DATE_CONTEXT_MAX_DEPTH = 50
_YEAR_BOUNDARY_MONTHS = 6

MONTHS_ASCII = {
    "stycznia": 1,
    "lutego": 2,
    "marca": 3,
    "kwietnia": 4,
    "maja": 5,
    "czerwca": 6,
    "lipca": 7,
    "sierpnia": 8,
    "wrzesnia": 9,
    "pazdziernika": 10,
    "listopada": 11,
    "grudnia": 12,
}

STATUS_KEYWORDS = {
    "zarezerwuj": "open",
    "rezerwuj": "open",
    "zapisz sie": "open",
    "brak miejsc": "full",
    "lista rezerwowa": "waitlist",
    "odwolane": "cancelled",
    "odwolana": "cancelled",
    "odwolany": "cancelled",
    "odwolane zajecia": "cancelled",
    "zapisy": "open",
    "zamkniete zapisy": "closed",
    "za wczesnie": "closed",
    "termin rejestracji minal": "closed",
}


@dataclass(frozen=True)
class Slot:
    name: str
    start: datetime
    status: Optional[str]
    trainer: Optional[str]
    raw: str
    url: Optional[str]
    capacity_used: Optional[int]
    capacity_total: Optional[int]


@dataclass(frozen=True)
class ScheduleResult:
    slots: list[Slot]
    raw_count: int


def _strip_accents(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _norm_ascii(text: str) -> str:
    return _strip_accents(_norm_text(text))


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _norm_ascii(text))


def _parse_time(text: str) -> Optional[time]:
    match = TIME_RE.search(text)
    if not match:
        return None
    value = match.group(0).replace(".", ":")
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        return None


def _parse_date(text: str, today: date) -> Optional[date]:
    match = DATE_RE_NUM.search(text)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else today.year
        year = _adjust_year(today, month, year)
        try:
            return date(year, month, day)
        except ValueError:
            return None

    match = DATE_RE_WORD.search(text)
    if match:
        day = int(match.group(1))
        month_name = _norm_ascii(match.group(2))
        month = MONTHS_ASCII.get(month_name)
        if not month:
            return None
        year = _adjust_year(today, month, today.year)
        try:
            return date(year, month, day)
        except ValueError:
            return None

    return None


def _iter_attr_values(tag) -> Iterable[tuple[str, str]]:
    for key, value in getattr(tag, "attrs", {}).items():
        if isinstance(value, (list, tuple)):
            for item in value:
                yield str(key), str(item)
        else:
            yield str(key), str(value)


def _parse_date_from_attrs(tag, today: date) -> Optional[date]:
    for key, value in _iter_attr_values(tag):
        if not value:
            continue
        match = ISO_DATE_RE.search(value)
        if match:
            year, month, day = map(int, match.groups())
            try:
                return date(year, month, day)
            except ValueError:
                pass
        found = _parse_date(value, today)
        if found:
            return found
    return None


def _parse_time_from_attrs(tag) -> Optional[time]:
    for key, value in _iter_attr_values(tag):
        if not value:
            continue
        parsed = _parse_time(value)
        if parsed:
            return parsed
    return None


def _parse_datetime_from_attrs(tag) -> Optional[datetime]:
    for key, value in _iter_attr_values(tag):
        if not value:
            continue
        match = ISO_DATETIME_RE.search(value)
        if match:
            year, month, day, hour, minute = map(int, match.groups())
            try:
                return datetime(year, month, day, hour, minute)
            except ValueError:
                pass

        # Unix timestamps (seconds or milliseconds) in time-related attrs
        if any(token in key.lower() for token in ["time", "date", "start", "begin", "datetime"]):
            ts_match = UNIX_TS_RE.search(value)
            if ts_match:
                raw = ts_match.group(1)
                ts = int(raw)
                if len(raw) == 13:
                    ts = ts // 1000
                try:
                    return datetime.fromtimestamp(ts)
                except (OverflowError, OSError, ValueError):
                    pass

    return None


def _adjust_year(today: date, month: int, year: int) -> int:
    # If the schedule crosses a year boundary (e.g., Dec -> Jan), adjust forward.
    if year == today.year and month < today.month and (today.month - month) > _YEAR_BOUNDARY_MONTHS:
        return year + 1
    return year


def _find_date_context(tag, today: date) -> Optional[date]:
    """Walk up the DOM (current tag -> previous siblings -> parent) looking for a date.

    Stops after ``_DATE_CONTEXT_MAX_DEPTH`` nodes to avoid runaway traversal.
    """
    checked = 0
    node = tag
    while node is not None and checked < _DATE_CONTEXT_MAX_DEPTH:
        text = node.get_text(" ", strip=True)
        found = _parse_date(text, today)
        if found:
            return found
        prev = node.previous_sibling
        while prev is not None and checked < _DATE_CONTEXT_MAX_DEPTH:
            checked += 1
            if hasattr(prev, "get_text"):
                text = prev.get_text(" ", strip=True)
                found = _parse_date(text, today)
                if found:
                    return found
            prev = prev.previous_sibling
        node = getattr(node, "parent", None)
        checked += 1
    return None


def _extract_status(text: str) -> Optional[str]:
    norm = _norm_ascii(text)
    for key, status in STATUS_KEYWORDS.items():
        if key in norm:
            return status
    return None


def _clean_text(text: str) -> str:
    cleaned = TIME_RE.sub(" ", text)
    for key in STATUS_KEYWORDS:
        cleaned = re.sub(
            rf"\b{re.escape(key)}\b",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -|/")
    return cleaned


def _extract_name_and_trainer(text: str) -> tuple[str, Optional[str]]:
    cleaned = _clean_text(text)
    # Split on common separators to guess trainer.
    for sep in [" - ", " / ", " | "]:
        if sep in cleaned:
            left, right = cleaned.split(sep, 1)
            left = left.strip()
            right = right.strip()
            if left and right:
                return left, right
    return cleaned, None


def _event_candidates(soup: BeautifulSoup, selector: Optional[str]) -> list:
    if selector:
        matches = soup.select(selector)
        if matches:
            return matches

    # Look for obvious data attributes
    for attr in ["data-event", "data-class", "data-lesson", "data-start", "data-id"]:
        matches = soup.find_all(attrs={attr: True})
        if matches:
            return matches

    # Heuristic: smallest elements containing time strings
    candidates: list = []
    for tag in soup.find_all(["li", "tr", "div", "article", "section", "td"]):
        text = tag.get_text(" ", strip=True)
        if not TIME_RE.search(text):
            continue
        if len(text) > _MAX_EVENT_TEXT_LEN:
            continue
        # Skip tags that contain smaller tags with their own time
        has_time_child = False
        for child in tag.find_all(["li", "tr", "div", "article", "section", "td"]):
            if child is tag:
                continue
            child_text = child.get_text(" ", strip=True)
            if TIME_RE.search(child_text):
                has_time_child = True
                break
        if not has_time_child:
            candidates.append(tag)

    return candidates


def _fetch_html_requests(url: str, user_agent: str, timeout_s: int) -> str:
    headers = {
        "User-Agent": user_agent,
        "Accept-Language": "pl,en;q=0.8",
    }
    response = requests.get(url, headers=headers, timeout=timeout_s)
    response.raise_for_status()
    return response.text


async def _click_first(page, selectors: Iterable[str]) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if await locator.count() == 0:
                continue
            await locator.first.click()
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def _maybe_accept_cookies(page) -> None:
    selectors = [
        "button:has-text(\"Zaakceptuj i zamknij\")",
        "button:has-text(\"Zaakceptuj\")",
        "button:has-text(\"Akceptuj\")",
        "button:has-text(\"Akcept\")",
        "button:has-text(\"Zgadzam\")",
        "button:has-text(\"Accept\")",
        "button:has-text(\"OK\")",
        "button:has-text(\"Rozumiem\")",
        "button:has-text(\"Zamknij\")",
        "[aria-label*=\"accept\" i]",
        "[aria-label*=\"zgadzam\" i]",
    ]
    clicked = await _click_first(page, selectors)
    if clicked:
        await page.wait_for_timeout(500)


async def _try_set_week_view(page) -> None:
    selectors = [
        ".fc-timeGridWeek-button",
        ".fc-dayGridWeek-button",
        ".fc-listWeek-button",
        "button:has-text(\"Tydzień\")",
        "button:has-text(\"Tydzien\")",
        "button:has-text(\"Week\")",
        "a:has-text(\"Tydzień\")",
        "a:has-text(\"Tydzien\")",
    ]
    clicked = await _click_first(page, selectors)
    if clicked:
        await page.wait_for_timeout(500)


async def _try_click_today(page) -> None:
    selectors = [
        ".fc-today-button",
        "button:has-text(\"Dziś\")",
        "button:has-text(\"Dzis\")",
        "button:has-text(\"Dzisiaj\")",
        "button:has-text(\"Today\")",
        "button:has-text(\"Teraz\")",
        "a:has-text(\"Dziś\")",
        "a:has-text(\"Dzis\")",
        "a:has-text(\"Dzisiaj\")",
        "[aria-label*=\"today\" i]",
        "[aria-label*=\"dzis\" i]",
    ]
    clicked = await _click_first(page, selectors)
    if clicked:
        await page.wait_for_timeout(500)


async def _click_prev(page) -> bool:
    selectors = [
        ".fc-prev-button",
        ".swiper-button-prev",
        "button:has-text(\"Poprzed\")",
        "button:has-text(\"Prev\")",
        "button:has-text(\"Previous\")",
        "button:has-text(\"‹\")",
        "button:has-text(\"<\")",
        "a:has-text(\"Poprzed\")",
        "a[rel=\"prev\"]",
        "[aria-label*=\"prev\" i]",
        "[aria-label*=\"previous\" i]",
    ]
    return await _click_first(page, selectors)


async def _click_next(page) -> bool:
    selectors = [
        ".fc-next-button",
        ".swiper-button-next",
        "button:has-text(\"Nast\")",
        "button:has-text(\"Next\")",
        "button:has-text(\"›\")",
        "button:has-text(\">\")",
        "a:has-text(\"Nast\")",
        "a[rel=\"next\"]",
        "[aria-label*=\"next\" i]",
    ]
    return await _click_first(page, selectors)


async def _wait_for_any_selector(page, selectors: Iterable[str], timeout_ms: int) -> bool:
    for selector in selectors:
        try:
            await page.wait_for_selector(selector, timeout=timeout_ms)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


DEFAULT_WAIT_SELECTORS = [
    ".fc-view-harness",
    ".fc-scrollgrid",
    ".fc-event",
    ".fc-timegrid-event",
    ".fc-daygrid-event",
    "[data-event]",
    "[data-lesson]",
    ".schedule",
    ".timetable",
    ".calendar",
]


def _parse_slots_from_html(
    html: str,
    selector: Optional[str],
    today: date,
    base_url: str,
) -> ScheduleResult:
    soup = BeautifulSoup(html, "lxml")
    # Preferred: structured schedule items rendered in HTML
    structured_slots, structured_count = _parse_club_schedule_items(soup, base_url)
    if structured_count > 0:
        return ScheduleResult(slots=structured_slots, raw_count=structured_count)

    candidates = _event_candidates(soup, selector)

    slots: list[Slot] = []

    for tag in candidates:
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        attr_dt = _parse_datetime_from_attrs(tag)
        if attr_dt:
            start_dt = attr_dt
        else:
            start_time = _parse_time(text) or _parse_time_from_attrs(tag)
            if not start_time:
                continue

            start_date = (
                _parse_date(text, today)
                or _parse_date_from_attrs(tag, today)
                or _find_date_context(tag, today)
            )
            if not start_date:
                logging.debug("Skipping event without date: %s", text)
                continue

            start_dt = datetime.combine(start_date, start_time)

        name, trainer = _extract_name_and_trainer(text)
        status = _extract_status(text)

        slots.append(
            Slot(
                name=name,
                start=start_dt,
                status=status,
                trainer=trainer,
                raw=text,
                url=None,
                capacity_used=None,
                capacity_total=None,
            )
        )

    return ScheduleResult(slots=slots, raw_count=len(candidates))


def _parse_club_schedule_items(soup: BeautifulSoup, base_url: str) -> tuple[list[Slot], int]:
    items = soup.select("li.club-schedule-item")
    if not items:
        return [], 0

    slots: list[Slot] = []
    for item in items:
        raw_text = item.get_text(" ", strip=True)

        day_value = item.get("data-day")
        day_date = None
        if day_value:
            try:
                day_date = datetime.strptime(day_value, "%Y-%m-%d").date()
            except ValueError:
                day_date = None

        time_tag = item.find("time")
        start_dt = None
        if time_tag is not None:
            dt_attr = time_tag.get("datetime")
            if dt_attr:
                # Example: 2026-02-10 09:00
                try:
                    start_dt = datetime.strptime(dt_attr, "%Y-%m-%d %H:%M")
                except ValueError:
                    pass
            if start_dt is None:
                time_text = time_tag.get_text(" ", strip=True)
                start_time = _parse_time(time_text)
                if start_time and day_date:
                    start_dt = datetime.combine(day_date, start_time)

        if start_dt is None and day_date:
            # Fall back to any time found in text
            start_time = _parse_time(raw_text)
            if start_time:
                start_dt = datetime.combine(day_date, start_time)

        if start_dt is None:
            continue

        activity_tag = item.find("a", class_="activity")
        name = activity_tag.get_text(" ", strip=True) if activity_tag else ""
        if not name:
            name = item.get("data-activity", "") or ""

        trainer = None
        trainer_tag = item.find("a", class_="trainer")
        if trainer_tag is not None:
            trainer_text = trainer_tag.get_text(" ", strip=True)
            if trainer_text:
                trainer = trainer_text
        if not trainer:
            trainer_slug = item.get("data-trainer", "")
            if trainer_slug:
                trainer = _slug_to_name(trainer_slug)

        reg = item.find("div", class_=re.compile(r"registration"))
        reg_text = reg.get_text(" ", strip=True) if reg else ""
        status = _extract_status(reg_text) or _extract_status(raw_text)

        item_url = item.get("data-url")
        absolute_url = urljoin(base_url, item_url) if item_url else None
        capacity_used, capacity_total = _parse_capacity(item)

        slots.append(
            Slot(
                name=name or raw_text,
                start=start_dt,
                status=status,
                trainer=trainer,
                raw=raw_text,
                url=absolute_url,
                capacity_used=capacity_used,
                capacity_total=capacity_total,
            )
        )

    return slots, len(items)


def _slug_to_name(value: str) -> str:
    cleaned = value.replace("_", " ").replace("-", " ").strip()
    if not cleaned:
        return value
    return " ".join(word.capitalize() for word in cleaned.split())


def _parse_capacity(item) -> tuple[Optional[int], Optional[int]]:
    users_tag = item.select_one(".users")
    if users_tag is None:
        users_tag = item.find("span", attrs={"data-icon-alt": re.compile("uczest", re.I)})
    if users_tag is None:
        return None, None

    text = users_tag.get_text(" ", strip=True)
    match = CAPACITY_RE.search(text)
    if not match:
        return None, None

    try:
        used = int(match.group(1))
        total = int(match.group(2))
    except ValueError:
        return None, None

    return used, total


async def _fetch_html_playwright(
    url: str,
    user_agent: str,
    timeout_s: int,
    wait_selector: Optional[str],
    headless: bool,
    selector: Optional[str],
    today: date,
    seek_week: bool,
    max_steps: int,
) -> str:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Playwright не установлен. Установи: pip3 install playwright "
            "и потом: python3 -m playwright install"
        ) from exc

    timeout_ms = timeout_s * 1000
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(
                user_agent=user_agent,
                locale="pl-PL",
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await _maybe_accept_cookies(page)
            await _try_set_week_view(page)
            await _try_click_today(page)
            # Avoid long waits: a short pause is usually enough for the schedule widget.
            await page.wait_for_timeout(1000)

            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=timeout_ms)
                except Exception:  # noqa: BLE001
                    logging.debug("Wait selector not found: %s", wait_selector)
            else:
                fast_timeout = min(5000, max(1500, timeout_ms // 3))
                await _wait_for_any_selector(page, DEFAULT_WAIT_SELECTORS, fast_timeout)

            if seek_week:
                now_dt = datetime.combine(today, time.min)
                week_start, week_end = week_range(now_dt)
                for _ in range(max_steps + 1):
                    content = await page.content()
                    result = _parse_slots_from_html(content, selector, today, url)
                    if result.slots:
                        week_slots = filter_slots_for_week(result.slots, now_dt)
                        if week_slots:
                            return content

                        earliest = min(result.slots, key=lambda s: s.start).start
                        latest = max(result.slots, key=lambda s: s.start).start
                        if earliest > week_end:
                            moved = await _click_prev(page)
                        elif latest < week_start:
                            moved = await _click_next(page)
                        else:
                            return content

                        if not moved:
                            return content

                        await page.wait_for_timeout(700)
                        continue

                    # No slots parsed; return whatever we have
                    break

            return await page.content()
        finally:
            await browser.close()


async def fetch_schedule(
    url: str,
    user_agent: str,
    selector: Optional[str] = None,
    timeout_s: int = 20,
    use_playwright: bool = False,
    playwright_wait_selector: Optional[str] = None,
    playwright_headless: bool = True,
    playwright_timeout_s: Optional[int] = None,
    now: Optional[datetime] = None,
    playwright_seek_week: bool = True,
    playwright_max_steps: int = 12,
) -> ScheduleResult:
    today = (now.date() if now else datetime.now().date())
    if not use_playwright:
        html = await asyncio.to_thread(_fetch_html_requests, url, user_agent, timeout_s)
        return _parse_slots_from_html(html, selector, today, url)

    # Try fast HTML fetch first; fall back to Playwright only if empty.
    html = await asyncio.to_thread(_fetch_html_requests, url, user_agent, timeout_s)
    result = _parse_slots_from_html(html, selector, today, url)
    if result.slots:
        return result

    html = await _fetch_html_playwright(
        url=url,
        user_agent=user_agent,
        timeout_s=playwright_timeout_s or timeout_s,
        wait_selector=playwright_wait_selector,
        headless=playwright_headless,
        selector=selector,
        today=today,
        seek_week=playwright_seek_week,
        max_steps=playwright_max_steps,
    )
    return _parse_slots_from_html(html, selector, today, url)


def week_range(now: datetime) -> tuple[datetime, datetime]:
    # Week: Monday 00:00 through Sunday 23:59:59
    weekday = now.weekday()
    start = datetime.combine((now - timedelta(days=weekday)).date(), time.min)
    end = start + timedelta(days=7) - timedelta(seconds=1)
    return start, end


def filter_slots_for_week(slots: Iterable[Slot], now: datetime) -> list[Slot]:
    start, end = week_range(now)
    return [slot for slot in slots if start <= slot.start <= end]


def filter_slots_by_name(slots: Iterable[Slot], query: str) -> list[Slot]:
    query_norm = _norm_ascii(query)
    query_compact = _compact(query)
    result: list[Slot] = []
    for slot in slots:
        name_norm = _norm_ascii(slot.name)
        name_compact = _compact(slot.name)
        raw_norm = _norm_ascii(slot.raw)
        if query_norm in name_norm or query_norm in raw_norm or query_compact in name_compact:
            result.append(slot)
    return result


def filter_slots_by_trainer(slots: Iterable[Slot], query: str) -> list[Slot]:
    query_norm = _norm_ascii(query)
    query_compact = _compact(query)
    result: list[Slot] = []
    for slot in slots:
        trainer_text = slot.trainer or ""
        combined = f"{trainer_text} {slot.raw} {slot.name}"
        combined_norm = _norm_ascii(combined)
        combined_compact = _compact(combined)
        if query_norm in combined_norm or query_compact in combined_compact:
            result.append(slot)
    return result
