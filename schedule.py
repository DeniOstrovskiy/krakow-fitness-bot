"""Расписание клубов через API PerfectGym (без сайта, без Chromium) и фильтры для поиска."""
from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

_CLUB_URL = re.compile(r"/kluby-fitness/([^/?#]+)/grafik-zajec/?$")
_PG_STATUS = {"open": "open", "full": "full", "waitlist": "waitlist", "closed": "closed", "early": "early", "booked": "booked"}

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
    pg_id: Optional[int] = None
    club_slug: Optional[str] = None


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


def _fetch_pg_slots(url: str, today: date) -> ScheduleResult:
    """Расписание клуба из API. Бросает исключение, если клуб не распознан или API недоступен."""
    import pg
    m = _CLUB_URL.search(url.split("?")[0])
    if not m:
        raise ValueError(f"не похоже на ссылку клуба: {url}")
    slug = m.group(1)
    cid = pg.club_id(slug)
    if not cid:
        raise ValueError(f"клуб {slug} не найден в PerfectGym (задайте PG_CLUB_IDS)")
    weeks = int(os.getenv("SEARCH_DAYS", "7")) // 7 + 1
    slots: list[Slot] = []
    seen: set = set()
    for k in range(weeks):
        items, _, _ = pg.weekly(cid, today + timedelta(days=7 * k))
        for raw in items:
            if raw["Id"] in seen:
                continue
            seen.add(raw["Id"])
            it = pg.to_item(slug, raw)
            lim = (raw.get("BookingIndicator") or {}).get("Limit")
            av = (raw.get("BookingIndicator") or {}).get("Available")
            tr = it["trainer"].title() if it["trainer"].isupper() else it["trainer"]
            slots.append(Slot(
                name=it["name"], start=it["start"], status=_PG_STATUS.get(it["state"]), trainer=tr or None,
                raw=f"{it['name']} {it['start']:%Y-%m-%d %H:%M}", url=None,
                capacity_used=(lim - av) if isinstance(lim, int) and isinstance(av, int) else None,
                capacity_total=lim if isinstance(lim, int) else None, pg_id=it["pg_id"], club_slug=slug))
    return ScheduleResult(slots=slots, raw_count=len(slots))


async def fetch_schedule(url: str, user_agent: Optional[str] = None, selector: Optional[str] = None, timeout_s: int = 20,
                         now: Optional[datetime] = None, **_ignored) -> ScheduleResult:
    today = now.date() if now else datetime.now().date()
    return await asyncio.to_thread(_fetch_pg_slots, url, today)


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
