from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from config import load_config
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

    lines: list[str] = []
    any_success = False

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

        any_success = True
        slots = filter_slots_for_week(schedule.slots, now)
        if mode == "trainer":
            slots = filter_slots_by_trainer(slots, query)
            title = f"{club.name}: тренер {query} (эта неделя)"
        else:
            slots = filter_slots_by_name(slots, query)
            title = f"{club.name}: {query} (эта неделя)"
        slots.sort(key=lambda s: s.start)

        lines.append(title)
        lines.append("")

        if not slots:
            lines.append("Нет слотов на этой неделе.")
            lines.append("")
            continue

        for slot in slots[: cfg.max_results]:
            lines.append(_format_slot(slot, tz))

        if len(slots) > cfg.max_results:
            lines.append("")
            lines.append(f"Показано {cfg.max_results} из {len(slots)} слотов.")

        lines.append("")

    if not any_success:
        await update.message.reply_text(
            "Не удалось загрузить расписание. Проверь ссылки и попробуй еще раз."
        )
        return

    while lines and not lines[-1].strip():
        lines.pop()

    await update.message.reply_text("\n".join(lines))


def _format_slot(slot, tz) -> str:
    date_str = slot.start.astimezone(tz).strftime("%a %d.%m %H:%M")
    status = f" [{slot.status}]" if slot.status else ""
    trainer = f" - {slot.trainer}" if slot.trainer else ""
    line = f"- {date_str} - {slot.name}{trainer}{status}"
    if getattr(slot, "url", None):
        line = f"{line} | {slot.url}"
    return line


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


def _build_webhook_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path}"
