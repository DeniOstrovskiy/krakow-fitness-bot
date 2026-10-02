from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
import html
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from booking_commands import _owner_id
from config import load_config

_SLOT_URL = re.compile(r"/kluby-fitness/([^/]+)/grafik-zajec/(\d+)")


def _upcoming(slots, now, tz, days: int = 7):
    """Занятия от сейчас до now+days (а не календарная неделя)."""
    end = now + timedelta(days=days)
    out = []
    for s in slots:
        st = s.start.replace(tzinfo=tz) if s.start.tzinfo is None else s.start.astimezone(tz)
        if now <= st <= end:
            out.append(s)
    return out
from schedule import (
    fetch_schedule,
    filter_slots_by_name,
    filter_slots_by_trainer,
    filter_slots_for_week,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Отправь название тренировки (например: Yoga, Cross, Pilates), "
        "и я пришлю слоты на эту неделю по всем выбранным клубам.\n"
        "Если нужен конкретный тренер, напиши: `trainer: Имя Фамилия`.",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Просто отправь название тренировки. Я верну слоты на эту неделю по всем клубам.\n"
        "Пример: `yoga` или `stretch`\n"
        "Тренер: `trainer: Sebastian Buczek`\n"
        "Диагностика: `/debug`",
        parse_mode="Markdown",
    )

async def _handle_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query: str,
    mode: str,
) -> None:
    if not query or len(query) < 2:
        await update.message.reply_text("Нужна хотя бы пара букв в запросе.")
        return

    cfg = context.bot_data["config"]
    if cfg.use_playwright:
        await update.message.reply_text("Секунду, собираю расписание...")
    tz = cfg.timezone
    now = datetime.now(tz)

    any_success = False
    error_lines: list[str] = []
    lines: list[str] = []
    cards: list[tuple] = []

    for club in cfg.clubs:
        try:
            timeout_budget = cfg.playwright_timeout_s + 10 + (cfg.playwright_max_steps * 3)
            schedule = await asyncio.wait_for(
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
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to fetch schedule for %s", club.url)
            error_lines.append(f"{club.name}: ошибка загрузки расписания.")
            continue

        any_success = True
        slots = _upcoming(schedule.slots, now, tz)
        if mode == "trainer":
            slots = filter_slots_by_trainer(slots, query)
            title = f"{club.name}: тренер {query} (ближайшие 7 дней)"
        else:
            slots = filter_slots_by_name(slots, query)
            title = f"{club.name}: {query} (ближайшие 7 дней)"
        slots.sort(key=lambda s: s.start)

        lines.append(title)
        lines.append("")

        if not slots:
            lines.append("Нет слотов на этой неделе.")
            lines.append("")
            continue

        shown_slots = slots[: cfg.max_results]
        for idx, slot in enumerate(shown_slots):
            lines.append(_format_slot(slot, tz, html_mode=True))
            m = _SLOT_URL.search(slot.url or "")
            if m:
                short = re.search(r"\((.+)\)", club.name)
                short = short.group(1) if short else club.name
                cards.append((short, slot, f"bk:{m.group(1)}:{m.group(2)}"))
            if idx < len(shown_slots) - 1:
                lines.append("")

        if len(slots) > cfg.max_results:
            lines.append("")
            lines.append(f"Показано {cfg.max_results} из {len(slots)} слотов.")
            lines.append("")

    if not any_success:
        if error_lines:
            await update.message.reply_text("\n".join(error_lines))
        await update.message.reply_text(
            "Не удалось загрузить расписание. Проверь ссылки и попробуй еще раз."
        )
        return

    if error_lines:
        lines.append("\n".join(error_lines))

    while lines and not lines[-1].strip():
        lines.pop()

    owner = _owner_id()
    if cards and owner and update.effective_user and update.effective_user.id == owner:
        limit = int(os.getenv("MAX_CARDS", "12"))
        head = f"Нашёл {len(cards)} ближайших занятий по запросу «{query}»"
        if len(cards) > limit:
            head += f", показываю первые {limit} (уточните запрос, чтобы увидеть остальные)"
        await update.message.reply_text(head + ":")
        for short, slot, data in cards[:limit]:
            await update.message.reply_text(
                f"<b>{html.escape(short)}</b>\n" + _format_slot(slot, tz, html_mode=True),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Записаться", callback_data=data)]]),
            )
            await asyncio.sleep(0.3)
        if error_lines:
            await update.message.reply_text("\n".join(error_lines))
        return

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


STATUS_LABELS = {
    "open": "✅ Запись открыта",
    "full": "🚫 Нет мест",
    "waitlist": "🟡 Лист ожидания",
    "cancelled": "❌ Отменено",
    "closed": "⛔ Запись закрыта",
    "early": "🕒 Запись ещё не открыта",
}


def _capacity_badge(free: int) -> str:
    if free <= 3:
        return "🔴"
    if free <= 8:
        return "🟡"
    return "🟢"


def _format_slot(slot, tz, html_mode: bool = False) -> str:
    start_dt = slot.start
    if start_dt.tzinfo is None:
        # Treat naive times as local club time (Europe/Warsaw) to avoid shifting on servers in UTC.
        start_local = start_dt.replace(tzinfo=tz)
    else:
        start_local = start_dt.astimezone(tz)
    date_str = start_local.strftime("%a %d.%m %H:%M")
    trainer = f" - {slot.trainer}" if slot.trainer else ""
    parts: list[str] = []

    if slot.capacity_total is not None and slot.capacity_used is not None:
        free = max(slot.capacity_total - slot.capacity_used, 0)
        badge = _capacity_badge(free)
        parts.append(f"Свободно: {badge} {free}/{slot.capacity_total}")

    if slot.status:
        parts.append(STATUS_LABELS.get(slot.status, f"Статус: {slot.status}"))

    if parts:
        suffix = "\n" + "\n".join(parts)
    else:
        suffix = ""

    line = f"- {date_str} - {slot.name}{trainer}{suffix}"
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
    return f"- <b>{date_html}</b> - <b>{name_html}</b>{trainer_html}{suffix_html}{url_html}"


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
    await update.message.reply_text("Секунду, проверяю расписание...")

    for club in cfg.clubs:
        try:
            timeout_budget = cfg.playwright_timeout_s + 10 + (cfg.playwright_max_steps * 3)
            schedule = await asyncio.wait_for(
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
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to fetch schedule for %s", club.url)
            lines.append(f"{club.name}: ошибка загрузки расписания.")
            lines.append("")
            continue

        total_slots = len(schedule.slots)
        week_slots = filter_slots_for_week(schedule.slots, now)
        week_count = len(week_slots)
        lines.append(f"{club.name}:")
        lines.append(f"- Сырых элементов: {schedule.raw_count}")
        lines.append(f"- Найдено занятий с датой: {total_slots}")
        lines.append(f"- На этой неделе: {week_count}")

        if schedule.slots:
            earliest = min(schedule.slots, key=lambda s: s.start).start.astimezone(tz)
            latest = max(schedule.slots, key=lambda s: s.start).start.astimezone(tz)
            lines.append(
                f"- Диапазон дат: {earliest.strftime('%d.%m.%Y')} - {latest.strftime('%d.%m.%Y')}"
            )

        if week_slots:
            lines.append("- Примеры (эта неделя):")
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
        await update.message.reply_text("Напиши имя тренера после команды. Например: /trainer Sebastian Buczek")
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
