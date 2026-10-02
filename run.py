"""Точка входа: Telegram-бот (поиск расписания + автозапись).

Webhook-режим (Render): POST <WEBHOOK_PATH> от Telegram, GET /tick?key=... от cron-job.org, GET /health.
Локально (без WEBHOOK_URL/RENDER_EXTERNAL_URL): polling + проверка раз в минуту.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import os
import re

from aiohttp import web
from dotenv import load_dotenv

load_dotenv()  # до импорта модулей, которые читают окружение

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

import bot as legacy
import booking_commands as bc
from config import ClubSchedule, load_config


def build_app(cfg, webhook: bool) -> Application:
    builder = Application.builder().token(cfg.bot_token)
    if webhook:
        builder = builder.updater(None)
    app = builder.build()
    app.bot_data["config"] = cfg
    app.add_handler(CommandHandler("start", legacy.start))
    app.add_handler(CommandHandler("help", legacy.help_command))
    app.add_handler(CommandHandler("debug", legacy.debug_command))
    app.add_handler(CommandHandler("trainer", legacy.trainer_command))
    app.add_handler(CommandHandler("coach", legacy.trainer_command))
    bc.register(app)  # до общего текстового обработчика
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, legacy.handle_query))
    return app


async def serve(cfg) -> None:
    app = build_app(cfg, webhook=True)
    secret = hashlib.sha256(cfg.bot_token.encode()).hexdigest()[:32]
    tick_key = os.getenv("TICK_SECRET", "")
    path = "/" + cfg.webhook_path.lstrip("/")

    async def telegram_hook(request: web.Request) -> web.Response:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return web.Response(status=403)
        await app.update_queue.put(Update.de_json(await request.json(), app.bot))
        return web.Response(text="ok")

    async def tick(request: web.Request) -> web.Response:
        if not tick_key or request.query.get("key") != tick_key:
            return web.Response(status=403)
        if getattr(app, "_tick_task", None) and not app._tick_task.done():
            return web.json_response({"started": False, "reason": "already running"})
        app._tick_task = asyncio.create_task(bc.run_tick(app))
        return web.json_response({"started": True})

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    await app.initialize()
    await app.start()
    await app.bot.set_webhook(
        legacy._build_webhook_url(cfg.webhook_base_url, cfg.webhook_path),
        secret_token=secret,
        drop_pending_updates=cfg.drop_pending_updates,
        allowed_updates=Update.ALL_TYPES,
    )
    web_app = web.Application()
    web_app.add_routes([web.post(path, telegram_hook), web.get("/tick", tick), web.get("/health", health)])
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, cfg.webhook_listen_host, cfg.webhook_listen_port).start()
    logging.info("Webhook server on %s:%s%s", cfg.webhook_listen_host, cfg.webhook_listen_port, path)
    await asyncio.Event().wait()


def main() -> None:
    cfg = load_config()
    # Клубы из SCHEDULE_URLS остаются как есть; из FAVORITE_CLUBS добавляются только недостающие.
    have = []
    for c in cfg.clubs:
        m = re.search(r"/kluby-fitness/([^/]+)/", c.url)
        if m:
            have.append(m.group(1))
    bc.FAVORITES.extend(s for s in have if s not in bc.FAVORITES)  # для /watch тоже
    selector = cfg.clubs[0].selector if cfg.clubs else None
    extra = [ClubSchedule(name=f"Zdrofit Kraków ({bc.club_title(s)})",
                          url=f"https://zdrofit.pl/kluby-fitness/{s}/grafik-zajec", selector=selector)
             for s in bc.FAVORITES if s not in have]
    cfg = dataclasses.replace(cfg, clubs=list(cfg.clubs) + extra)
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(message)s")
    if cfg.webhook_base_url:
        asyncio.run(serve(cfg))
    else:
        app = build_app(cfg, webhook=False)

        async def post_init(a: Application) -> None:
            asyncio.create_task(bc.tick_loop(a))

        app.post_init = post_init
        app.run_polling(drop_pending_updates=cfg.drop_pending_updates)


if __name__ == "__main__":
    main()
