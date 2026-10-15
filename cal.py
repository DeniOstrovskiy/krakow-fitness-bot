"""Календарь-подписка (iCalendar): занятия из списка «Записан» как события."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
from zoneinfo import ZoneInfo

import booker

TZ = ZoneInfo("Europe/Warsaw")
DEFAULT_MINUTES = 60


def feed_key() -> str:
    """Отдельный ключ для ссылки на календарь (не сам TICK_SECRET, чтобы тот не светился в календарных приложениях)."""
    base = os.getenv("TICK_SECRET", "") or os.getenv("BOT_TOKEN", "")
    return hmac.new(base.encode(), b"calendar-feed", hashlib.sha256).hexdigest()[:32]


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    raw = line.encode("utf-8")
    if len(raw) <= 74:
        return line
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (74 if not out else 73):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def _utc(iso: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(iso)
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ)
    return d.astimezone(dt.timezone.utc)


def build_ics(booked: list[dict]) -> str:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//zdrofit-booking-bot//RU",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Zdrofit",
             "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    for b in sorted(booked, key=lambda x: x["start"]):
        try:
            start = _utc(b["start"])
        except Exception:  # noqa: BLE001
            continue
        end = start + dt.timedelta(minutes=int(b.get("dur") or DEFAULT_MINUTES))
        parts = booker.norm_label(b["label"]).split("\n")
        club = parts[0] if parts else ""
        third = parts[2] if len(parts) > 2 else (parts[-1] if parts else "")
        name, _, trainer = third.partition(" - ")
        lines += ["BEGIN:VEVENT",
                  f"UID:{b['id']}@zdrofit-bot",
                  f"DTSTAMP:{now}",
                  f"DTSTART:{start:%Y%m%dT%H%M%SZ}",
                  f"DTEND:{end:%Y%m%dT%H%M%SZ}",
                  f"SUMMARY:{_esc(name or 'Zdrofit')}",
                  f"LOCATION:{_esc(f'Zdrofit {club}, Kraków')}"]
        if trainer:
            lines.append(f"DESCRIPTION:{_esc('Тренер: ' + trainer)}")
        lines += ["END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"
