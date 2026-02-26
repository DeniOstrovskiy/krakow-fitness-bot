from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from io import BytesIO
import html
import uuid

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from zoneinfo import ZoneInfo

from config import Config, ClubSchedule, load_config
from schedule import (
    ScheduleResult,
    Slot,
    enrich_waitlist_slots,
    fetch_schedule,
    filter_slots_by_name,
    filter_slots_by_trainer,
    filter_slots_for_week,
)

_MIN_QUERY_LENGTH = 2


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send a class name (e.g. Yoga, Cross, Pilates) "
        "and I will return this week's slots across all configured clubs.\n"
        "For a specific trainer, type: `trainer: First Last`.",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Just send a class name. I will return this week's slots for all clubs.\n"
        "Example: `yoga` or `stretch`\n"
        "Trainer: `trainer: Sebastian Buczek`\n"
        "Добавить в календарь: кнопки после поиска или `/add 3`\n"
        "Diagnostics: `/debug`",
        parse_mode="Markdown",
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text("Usage: /add <number> (use after a search)")
        return

    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please provide a number. Example: /add 3")
        return

    last_slots = context.chat_data.get("last_slots") or []
    if not last_slots:
        await update.message.reply_text("No recent search results. Send a query first.")
        return

    if idx < 1 or idx > len(last_slots):
        await update.message.reply_text(
            f"Number out of range. Pick 1-{len(last_slots)}."
        )
        return

    item = last_slots[idx - 1]
    slot: Slot = item["slot"]
    club_name: str = item["club"]

    tz = context.bot_data["config"].timezone
    ics_bytes = _build_ics(slot, club_name, tz)
    bio = BytesIO(ics_bytes)
    bio.name = f"training-{idx}.ics"
    await update.message.reply_document(
        document=bio,
        filename=bio.name,
        caption="Open this .ics on your iPhone to add the event with 48h and 5m reminders.",
    )


def _build_add_keyboard(count: int, per_row: int = 4) -> InlineKeyboardMarkup | None:
    if count <= 0:
        return None
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i in range(1, count + 1):
        row.append(InlineKeyboardButton(f"Добавить {i}", callback_data=f"add:{i}"))
        if len(row) == per_row:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


async def add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    data = query.data or ""
    if not data.startswith("add:"):
        return

    try:
        idx = int(data.split(":", 1)[1])
    except ValueError:
        await query.message.reply_text("Invalid selection.")
        return

    last_slots = context.chat_data.get("last_slots") or []
    if not last_slots:
        await query.message.reply_text("No recent search results. Please search again.")
        return

    if idx < 1 or idx > len(last_slots):
        await query.message.reply_text(f"Number out of range. Pick 1-{len(last_slots)}.")
        return

    item = last_slots[idx - 1]
    slot: Slot = item["slot"]
    club_name: str = item["club"]

    tz = context.bot_data["config"].timezone
    ics_bytes = _build_ics(slot, club_name, tz)
    bio = BytesIO(ics_bytes)
    bio.name = f"training-{idx}.ics"
    await query.message.reply_document(
        document=bio,
        filename=bio.name,
        caption="Open this .ics on your iPhone to add the event with 48h and 5m reminders.",
    )


# ---------------------------------------------------------------------------
# Shared fetch helper
# ---------------------------------------------------------------------------

async def _fetch_club_schedule(
    club: ClubSchedule,
    cfg: Config,
    now: datetime,
) -> ScheduleResult:
    """Fetch schedule for a single club with a timeout budget."""
    timeout_budget = cfg.playwright_timeout_s + 10 + (cfg.playwright_max_steps * 3)
    return await asyncio.wait_for(
        fetch_schedule(
            club.url,
            user_agent=cfg.user_agent,
            selector=club.selector,
            timeout_s=cfg.playwright_timeout_s,
            use_playwright=cfg.use_playwright,
            playwright_wait_selector=cfg.playwright_wait_selector,
            playwright_headless=cfg.playwright_headless,
            playwright_timeout_s=cfg.playwright_timeout_s,
            now=now,
            playwright_seek_week=cfg.playwright_seek_week,
            playwright_max_steps=cfg.playwright_max_steps,
        ),
        timeout=timeout_budget,
    )


# ---------------------------------------------------------------------------
# Search handler
# ---------------------------------------------------------------------------

async def _handle_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query: str,
    mode: str,
) -> None:
    if not query or len(query) < _MIN_QUERY_LENGTH:
        await update.message.reply_text("Please enter at least a couple of characters.")
        return

    cfg = context.bot_data["config"]
    if cfg.use_playwright:
        await update.message.reply_text("One moment, fetching the schedule...")
    tz = cfg.timezone
    now = datetime.now(tz)

    any_success = False
    error_lines: list[str] = []
    lines: list[str] = []
    last_slots: list[dict] = []
    slot_index = 1

    for club in cfg.clubs:
        try:
            schedule = await _fetch_club_schedule(club, cfg, now)
        except Exception:  # noqa: BLE001
            logging.exception("Failed to fetch schedule for %s", club.url)
            error_lines.append(f"{club.name}: failed to load schedule.")
            continue

        any_success = True
        slots = filter_slots_for_week(schedule.slots, now)
        club_name_html = html.escape(club.name)
        if mode == "trainer":
            slots = filter_slots_by_trainer(slots, query)
            title = f"🏋️ <b>{club_name_html}</b>: trainer {html.escape(query)} (this week)"
        else:
            slots = filter_slots_by_name(slots, query)
            title = f"🏋️ <b>{club_name_html}</b>: {html.escape(query)} (this week)"
        slots.sort(key=lambda s: s.start)

        lines.append(title)
        lines.append("")

        if not slots:
            lines.append("No slots this week.")
            lines.append("")
            continue

        # Fetch waitlist details for full classes (only for shown slots)
        shown_slots = slots[: cfg.max_results]
        has_waitlist = any(s.status == "waitlist" and s.url for s in shown_slots)
        if has_waitlist:
            try:
                shown_slots = await enrich_waitlist_slots(
                    shown_slots, cfg.user_agent, timeout_s=10
                )
            except Exception:  # noqa: BLE001
                logging.debug("Failed to enrich waitlist slots")
        for idx, slot in enumerate(shown_slots):
            lines.append(_format_slot(slot, tz, html_mode=True, index=slot_index))
            last_slots.append({"slot": slot, "club": club.name})
            slot_index += 1
            if idx < len(shown_slots) - 1:
                lines.append("")

        if len(slots) > cfg.max_results:
            lines.append("")
            lines.append(f"Showing {cfg.max_results} of {len(slots)} slots.")

        lines.append("")

    if not any_success:
        combined = list(error_lines)
        combined.append("Failed to load the schedule. Please check the links and try again.")
        await update.message.reply_text("\n".join(combined))
        return

    if error_lines:
        lines.append("\n".join(error_lines))

    if last_slots:
        lines.append("")
        lines.append("To add a class to your iPhone calendar: /add <number>")

    context.chat_data["last_slots"] = last_slots

    while lines and not lines[-1].strip():
        lines.pop()

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    if last_slots:
        keyboard = _build_add_keyboard(len(last_slots))
        if keyboard is not None:
            await update.message.reply_text(
                "Добавить в календарь (выбери номер):",
                reply_markup=keyboard,
            )


STATUS_LABELS = {
    "open": "✅ Booking open",
    "full": "🚫 No spots",
    "waitlist": "🟡 Waitlist (you can sign up)",
    "cancelled": "❌ Cancelled",
    "closed": "⛔ Booking closed",
}


def _localize(dt: datetime, tz: ZoneInfo) -> datetime:
    """Convert a datetime to the target timezone.

    Naive datetimes (from HTML parsing) are treated as already being
    in the target timezone, so we attach tzinfo without shifting.
    Aware datetimes are converted normally.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _capacity_badge(free: int, status: str | None = None) -> str:
    if free <= 0:
        if status == "waitlist":
            return "🟡"
        return "🔴"
    if free <= 3:
        return "🔴"
    if free <= 8:
        return "🟡"
    return "🟢"


def _format_slot(slot: Slot, tz: ZoneInfo, html_mode: bool = False, index: int | None = None) -> str:
    date_str = _localize(slot.start, tz).strftime("%a %d.%m %H:%M")
    trainer = f" - {slot.trainer}" if slot.trainer else ""
    parts: list[str] = []

    _WAITLIST_LIMIT = 10

    if slot.capacity_total is not None and slot.capacity_used is not None:
        free = max(slot.capacity_total - slot.capacity_used, 0)
        badge = _capacity_badge(free, slot.status)
        if slot.waitlist_used is not None:
            # We have real waitlist data from the detail page
            if slot.waitlist_used >= _WAITLIST_LIMIT:
                parts.append(
                    f"Spots: 🔴 {slot.capacity_total}/{slot.capacity_total} - no spots, "
                    f"waitlist: {slot.waitlist_used} people (cannot sign up)"
                )
            else:
                parts.append(
                    f"Spots: {badge} {slot.capacity_total}/{slot.capacity_total} - "
                    f"waitlist: {slot.waitlist_used} people"
                )
        elif free == 0 and slot.status == "waitlist":
            parts.append(f"Spots: {badge} {slot.capacity_used}/{slot.capacity_total} - waitlist")
        elif free == 0:
            parts.append(f"Spots: {badge} {slot.capacity_used}/{slot.capacity_total} - no spots")
        else:
            parts.append(f"Available: {badge} {free}/{slot.capacity_total}")

    waitlist_overflow = (
        slot.waitlist_used is not None and slot.waitlist_used >= _WAITLIST_LIMIT
    )
    if slot.status and not waitlist_overflow:
        parts.append(STATUS_LABELS.get(slot.status, f"Статус: {slot.status}"))
    elif waitlist_overflow:
        parts.append("🚫 No spots")

    if parts:
        suffix = "\n" + "\n".join(parts)
    else:
        suffix = ""

    prefix = f"{index}." if index is not None else "-"
    line = f"{prefix} {date_str} - {slot.name}{trainer}{suffix}"
    if getattr(slot, "url", None):
        line = f"{line}\n{slot.url}"

    if not html_mode:
        return line

    date_html = html.escape(date_str)
    name_html = html.escape(slot.name)
    trainer_html = f" - {html.escape(slot.trainer)}" if slot.trainer else ""
    parts_html = "\n".join(html.escape(part) for part in parts)
    suffix_html = f"\n{parts_html}" if parts_html else ""
    url_html = f"\n{html.escape(slot.url)}" if getattr(slot, "url", None) else ""
    prefix_html = html.escape(prefix)
    return f"{prefix_html} <b>{date_html}</b> - <b>{name_html}</b>{trainer_html}{suffix_html}{url_html}"


def _ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _build_ics(slot: Slot, club_name: str, tz: ZoneInfo) -> bytes:
    start_local = _localize(slot.start, tz)
    duration_min = slot.duration_min or 60
    end_local = start_local + timedelta(minutes=duration_min)
    now_utc = datetime.now(timezone.utc)
    uid = f"{uuid.uuid4().hex}@krakow-fitness-bot"

    summary = f"{slot.name} — {club_name}"
    description_lines = []
    if slot.trainer:
        description_lines.append(f"Trainer: {slot.trainer}")
    description_lines.append(f"Club: {club_name}")
    if slot.url:
        description_lines.append(f"Booking: {slot.url}")
    description = "\n".join(description_lines)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Krakow Fitness Bot//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now_utc.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;TZID={tz.key}:{start_local.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND;TZID={tz.key}:{end_local.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        f"LOCATION:{_ics_escape(club_name)}",
        "BEGIN:VALARM",
        "TRIGGER:-PT48H",
        "ACTION:DISPLAY",
        "DESCRIPTION:Reminder to book",
        "END:VALARM",
        "BEGIN:VALARM",
        "TRIGGER:-PT5M",
        "ACTION:DISPLAY",
        "DESCRIPTION:Reminder to book",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _build_webhook_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path}"


async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    cfg = context.bot_data["config"]
    tz = cfg.timezone
    now = datetime.now(tz)
    lines: list[str] = []
    await update.message.reply_text("One moment, checking the schedule...")

    for club in cfg.clubs:
        try:
            schedule = await _fetch_club_schedule(club, cfg, now)
        except Exception:  # noqa: BLE001
            logging.exception("Failed to fetch schedule for %s", club.url)
            lines.append(f"{club.name}: failed to load schedule.")
            lines.append("")
            continue

        total_slots = len(schedule.slots)
        week_slots = filter_slots_for_week(schedule.slots, now)
        week_count = len(week_slots)
        lines.append(f"{club.name}:")
        lines.append(f"- Raw elements: {schedule.raw_count}")
        lines.append(f"- Classes with date: {total_slots}")
        lines.append(f"- This week: {week_count}")

        if schedule.slots:
            earliest = _localize(min(schedule.slots, key=lambda s: s.start).start, tz)
            latest = _localize(max(schedule.slots, key=lambda s: s.start).start, tz)
            lines.append(
                f"- Date range: {earliest.strftime('%d.%m.%Y')} - {latest.strftime('%d.%m.%Y')}"
            )
        elif getattr(schedule, "debug_note", None):
            lines.append(f"- Debug: {schedule.debug_note}")

        if week_slots:
            lines.append("- Examples (this week):")
            for slot in week_slots[: min(5, cfg.max_results)]:
                lines.append(_format_slot(slot, tz))

        lines.append("")

    while lines and not lines[-1].strip():
        lines.pop()

    await update.message.reply_text("\n".join(lines))


async def trainer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Please provide a trainer name. Example: /trainer Sebastian Buczek")
        return
    await _handle_search(update, context, query, mode="trainer")


async def handle_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    lower = text.lower()

    for prefix in ("trainer:", "trener:", "coach:"):
        if lower.startswith(prefix):
            query = text[len(prefix):].strip()
            await _handle_search(update, context, query, mode="trainer")
            return

    await _handle_search(update, context, text, mode="class")


def main() -> None:
    cfg = load_config()

    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    application = Application.builder().token(cfg.bot_token).build()
    application.bot_data["config"] = cfg

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CallbackQueryHandler(add_callback, pattern=r"^add:\d+$"))
    application.add_handler(CommandHandler("debug", debug_command))
    application.add_handler(CommandHandler("trainer", trainer_command))
    application.add_handler(CommandHandler("coach", trainer_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_query))

    logging.info("Bot started for %s clubs", len(cfg.clubs))
    if cfg.webhook_base_url:
        webhook_url = _build_webhook_url(cfg.webhook_base_url, cfg.webhook_path)
        url_path = cfg.webhook_path.lstrip("/")
        logging.info(
            "Starting webhook at %s:%s %s",
            cfg.webhook_listen_host,
            cfg.webhook_listen_port,
            webhook_url,
        )
        application.run_webhook(
            listen=cfg.webhook_listen_host,
            port=cfg.webhook_listen_port,
            url_path=url_path,
            webhook_url=webhook_url,
            drop_pending_updates=cfg.drop_pending_updates,
        )
    else:
        application.run_polling(drop_pending_updates=cfg.drop_pending_updates)


if __name__ == "__main__":
    main()
