"""Telegram-команды для автозаписи: /watch /list /remove /pause /resume /status /cookie /myid."""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import os
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import booker
import store
import waf

DAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
DAY_MAP = {**{n: i for i, n in enumerate(DAY_NAMES)},
           **{n: i for i, n in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}}
FAVORITES = [c.strip() for c in os.getenv(
    "FAVORITE_CLUBS",
    "krakow-galeria-kazimierz,krakow-high5ive,krakow-kapelanka,krakow-garden-residence,krakow-przybyszewskiego",
).split(",") if c.strip()]

CLUB_TITLES = {
    "krakow-galeria-kazimierz": "Galeria Kazimierz",
    "krakow-high5ive": "High5ive",
    "krakow-kapelanka": "Kapelanka",
    "krakow-garden-residence": "Garden Residence",
    "krakow-przybyszewskiego": "Przybyszewskiego",
    "krakow-dytmara": "Dytmara",
}


def club_title(slug: str) -> str:
    return CLUB_TITLES.get(slug) or slug.removeprefix("krakow-").replace("-", " ").title()


DONE_MARKUP = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Записан", callback_data="noop")]])


HELP = (
    "Автозапись на занятия.\n\n"
    "/watch клуб | занятие | дни | время | тренер(необязательно)\n"
    "Пример:\n/watch kazimierz | bodypump | пн,ср | 18:00-20:00 | Buczek\n"
    "Дни: пн,ср или пн-пт или «любой». Время: 18:00-20:00, 18:00 или «любое».\n"
    f"Клубы: {', '.join(FAVORITES)}\n\n"
    "/list — что отслеживаю и куда записан\n"
    "/remove N — убрать отслеживание №N\n"
    "/pause, /resume — пауза и возобновление\n"
    "/status — состояние бота и сессии\n"
    "/cookie <строка> — обновить сессию сайта (сообщение будет удалено)"
)


def _owner_id() -> int:
    try:
        return int(os.getenv("ALLOWED_USER_ID", "0"))
    except ValueError:
        return 0


def owner_only(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        if not _owner_id():
            await update.effective_message.reply_text(
                f"Задайте ALLOWED_USER_ID={uid} в переменных окружения и перезапустите бота.")
            return
        if uid != _owner_id():
            return
        await fn(update, context)
    return wrapper


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_days(text: str):
    t = text.lower().strip()
    if t in ("любой", "все", "any", "*", "ежедневно"):
        return []
    out = set()
    for part in re.split(r"[,\s]+", t):
        if not part:
            continue
        m = re.fullmatch(r"([a-zа-я]+)-([a-zа-я]+)", part)
        if m:
            a, b = DAY_MAP.get(m.group(1)), DAY_MAP.get(m.group(2))
            if a is None or b is None:
                return None
            i = a
            while True:
                out.add(i)
                if i == b:
                    break
                i = (i + 1) % 7
        elif part in DAY_MAP:
            out.add(DAY_MAP[part])
        else:
            return None
    return sorted(out) or None


def parse_time(text: str):
    t = text.strip().lower()
    if t in ("любое", "any", "*", ""):
        return "", ""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?:\s*-\s*(\d{1,2}):(\d{2}))?", t)
    if not m:
        return None
    a = f"{int(m[1]):02d}:{m[2]}"
    b = f"{int(m[3]):02d}:{m[4]}" if m[3] else a
    return a, b


def resolve_club(q: str):
    n = _norm(q)
    hits = [c for c in FAVORITES if n and n in _norm(c)]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, f"Неоднозначно: {', '.join(hits)}"
    if re.fullmatch(r"[a-z0-9-]+", q.strip().lower()):
        return q.strip().lower(), None
    return None, f"Клуб не найден. Доступны: {', '.join(FAVORITES)}"


def resolve_activity(club: str, q: str):
    items = booker.fetch_club(booker.make_session({}), club, require_auth=False)
    slugs = sorted({i["activity"] for i in items})
    n = _norm(q)
    exact = [s for s in slugs if _norm(s) == n]
    hits = exact or [s for s in slugs if n and n in _norm(s)]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, "Такого занятия в расписании клуба нет. Есть: " + ", ".join(slugs)
    return None, "Уточните, их несколько: " + ", ".join(hits)


def describe(w: dict) -> str:
    days = ",".join(DAY_NAMES[d] for d in w["days"]) if w["days"] else "любой день"
    tm = (f"{w['time_from']}-{w['time_to']}" if w["time_from"] != w["time_to"] else w["time_from"]) \
        if w["time_from"] else "любое время"
    tr = f", тренер {w['trainer']}" if w.get("trainer") else ""
    return f"{w['activity']} @ {w['club']}, {days}, {tm}{tr}"


@owner_only
async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text.partition(" ")[2]
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 4:
        await update.effective_message.reply_text(HELP)
        return
    club, err = resolve_club(parts[0])
    if err:
        await update.effective_message.reply_text(err)
        return
    try:
        activity, err = await asyncio.to_thread(resolve_activity, club, parts[1])
    except Exception as e:
        await update.effective_message.reply_text(f"Не удалось получить расписание клуба: {e}")
        return
    if err:
        await update.effective_message.reply_text(err)
        return
    days = parse_days(parts[2])
    if days is None:
        await update.effective_message.reply_text("Не понял дни. Пример: пн,ср или пн-пт или «любой»")
        return
    tm = parse_time(parts[3])
    if tm is None:
        await update.effective_message.reply_text("Не понял время. Пример: 18:00-20:00 или «любое»")
        return
    w = {"club": club, "activity": activity, "trainer": parts[4] if len(parts) > 4 else "",
         "days": days, "time_from": tm[0], "time_to": tm[1]}
    store.update(lambda st: st["watches"].append(w))
    warn = "\n⚠️ Дни и время не ограничены: бот запишет на ВСЕ такие занятия." if not days and not tm[0] else ""
    await update.effective_message.reply_text(f"Добавлено: {describe(w)}{warn}")


def _build_list(st):
    lines = ["Отслеживаю:" if (st["watches"] or st["targets"]) else "Отслеживание пусто. Добавьте через /watch"]
    lines += [f"{i}. {describe(w)}" for i, w in enumerate(st["watches"], 1)]
    rows = []
    for i, t in enumerate(st["targets"], len(st["watches"]) + 1):
        lines.append(f"{i}. ⏳ {t['label']} (ждёт, когда можно будет записаться)")
        rows.append([InlineKeyboardButton(f"❌ Отменить {i}", callback_data=f"lr:{t['id']}")])
    if st["booked"]:
        lines += ["", "Записан:"] + [f"• {b['label']}" for b in sorted(st["booked"], key=lambda b: b["start"])]
        rows.append([InlineKeyboardButton("🔄 Сверить «Записан» с сайтом", callback_data="sync")])
    if st["paused"]:
        lines.append("\n⏸ Бот на паузе")
    return "\n".join(lines), (InlineKeyboardMarkup(rows) if rows else None)


@owner_only
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = await asyncio.to_thread(store.load)
    text, markup = _build_list(st)
    await update.effective_message.reply_text(text, reply_markup=markup)


async def list_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    owner = _owner_id()
    if not owner or q.from_user.id != owner:
        await q.answer("Нет доступа", show_alert=True)
        return
    item_id = q.data.split(":", 1)[1]

    def rm(st):
        for t in list(st["targets"]):
            if t["id"] == item_id:
                st["targets"].remove(t)
        return _build_list(st)

    text, markup = await asyncio.to_thread(store.update, rm)
    await q.answer("Отменено")
    try:
        await q.edit_message_text(text, reply_markup=markup)
    except Exception:
        pass


def _sync_booked() -> str:
    """Сверяет «Записан» с сайтом: убирает занятия, на которые на сайте вы уже не записаны."""
    st = store.load()
    now = booker.now_local().isoformat()
    todo = [b for b in st["booked"] if b["start"] > now]
    if not todo:
        return "Сверять нечего."
    s = booker.make_session(st["cookies"])
    cache: dict[str, dict] = {}
    removed, kept, unknown = [], 0, []
    for b in todo:
        club = b.get("club") or (re.search(r", ([\w-]+) \([^()]*\)$", b["label"]) or [None, None])[1]
        if not club:
            unknown.append(b["label"])
            continue
        if club not in cache:
            cache[club] = {it["id"]: it for it in booker.fetch_club(s, club)}
        it = cache[club].get(b["id"])
        if it is None:
            unknown.append(b["label"])
        elif booker.state_of(it) == "booked":
            kept += 1
        else:
            removed.append(b)
    if removed:
        ids = {b["id"] for b in removed}
        store.update(lambda x: x.update(booked=[b for b in x["booked"] if b["id"] not in ids]))
    out = [f"Сверка с сайтом: подтверждено {kept}, снято {len(removed)}."]
    out += [f"✖ Снято (на сайте не записан): {b['label']}" for b in removed]
    out += [f"? Не нашёл на сайте: {l}" for l in unknown]
    return "\n".join(out)


async def sync_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    owner = _owner_id()
    if not owner or q.from_user.id != owner:
        await q.answer("Нет доступа", show_alert=True)
        return
    await q.answer("Сверяю…")
    try:
        text = await asyncio.to_thread(_sync_booked)
    except Exception as e:  # noqa: BLE001
        text = f"Не удалось сверить: {e!r}"
    await q.message.reply_text(text)
    st = await asyncio.to_thread(store.load)
    t, markup = _build_list(st)
    try:
        await q.edit_message_text(t, reply_markup=markup)
    except Exception:
        pass


@owner_only
async def sync_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = await asyncio.to_thread(_sync_booked)
    except Exception as e:  # noqa: BLE001
        text = f"Не удалось сверить: {e!r}"
    await update.effective_message.reply_text(text)


@owner_only
async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(context.args[0])
    except Exception:
        await update.effective_message.reply_text("Использование: /remove N (номер из /list)")
        return

    def rm(st):
        if 1 <= n <= len(st["watches"]):
            return describe(st["watches"].pop(n - 1))
        k = n - len(st["watches"]) - 1
        if 0 <= k < len(st["targets"]):
            return st["targets"].pop(k)["label"]

    w = await asyncio.to_thread(store.update, rm)
    await update.effective_message.reply_text(f"Удалено: {w}" if w else "Нет такого номера")


def _set_paused(value: bool):
    @owner_only
    async def cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await asyncio.to_thread(store.update, lambda st: st.update(paused=value))
        await update.effective_message.reply_text("⏸ Пауза" if value else "▶️ Возобновлено")
    return cmd


@owner_only
async def cookie_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    raw = msg.text.partition(" ")[2].strip()
    try:
        await msg.delete()  # не оставляем cookie в чате
    except Exception:
        pass
    cookies = booker.parse_cookie_string(raw)
    if not cookies:
        await context.bot.send_message(msg.chat_id, "Пришлите так: /cookie name=value; name2=value2 ...")
        return
    ok = await asyncio.to_thread(_check_session, cookies)
    if ok is None:
        await context.bot.send_message(msg.chat_id, "⚠️ Сайт не отдаёт страницы с сервера (защита от ботов). Запустите /probe.")
        return
    if ok:
        def save(st):
            st["cookies"], st["auth_alert"] = cookies, ""
        await asyncio.to_thread(store.update, save)
    await context.bot.send_message(
        msg.chat_id, "✅ Сессия сохранена и работает." if ok else
        "❌ Сайт не считает вас залогиненным по этому cookie. Проверьте, что скопировали заголовок Cookie после входа.")


def _check_session(cookies: dict) -> bool:
    try:
        booker.fetch_club(booker.make_session(cookies), FAVORITES[0], require_auth=True)
        return True
    except booker.AuthError:
        return False
    except booker.BlockedError:
        return None  # сайт не отдаёт страницу серверу (защита от ботов)


@owner_only
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = await asyncio.to_thread(store.load)
    sess = "нет cookie"
    if st["cookies"]:
        chk = await asyncio.to_thread(_check_session, st["cookies"])
        sess = "сайт блокирует запросы с сервера" if chk is None else ("OK" if chk else "истекла")
    last = f"{int(time.time() - st['last_tick'])} с назад" if st["last_tick"] else "ещё не было"
    text = (f"Сессия: {sess}\nОтслеживаний: {len(st['watches'])}\n"
            f"Пауза: {'да' if st['paused'] else 'нет'}\nПоследняя проверка: {last}")
    if st.get("next_hot"):
        left = int(st["next_hot"] - time.time())
        if left > 0:
            text += f"\nЧастый опрос начнётся через {left // 3600} ч {left % 3600 // 60} мин"
    if st.get("open_observed"):
        o = st["open_observed"]
        text += f"\nПоследнее наблюдённое открытие: {o['label']} за {o['hours_before']} ч до начала"
    if st["last_response"]:
        text += f"\n\nОтвет сайта на последнюю запись:\n{st['last_response'][:600]}"
    await update.effective_message.reply_text(text)


def _probe_one(session, url, extra=None):
    try:
        r = session.get(url, timeout=25, headers=extra or {})
        body = r.text
        h = r.headers
        hd = {k: h[k] for k in h if k.lower() in (
            "server", "x-amzn-waf-action", "cf-mitigated", "x-cache", "via", "content-type",
            "x-amz-cf-pop", "retry-after")}
        cookie_names = ",".join(sorted({c.name for c in r.cookies}))[:80]
        title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        title = " ".join(title.group(1).split())[:80] if title else "-"
        return (f"HTTP {r.status_code}, {len(body)} байт, элементов: {body.count('club-schedule-item')}\n"
                f"заголовки: {hd}\ncookie от сайта: {cookie_names or '-'}\ntitle: {title}")
    except Exception as exc:  # noqa: BLE001
        return f"ошибка: {type(exc).__name__}: {str(exc)[:150]}"


_FULL_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0", "Sec-Ch-Ua-Platform": '"macOS"',
}


@owner_only
async def probe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/probe — что сайт отдаёт серверу: обычный запрос и «как браузер»."""
    club = FAVORITES[0]
    url = f"https://zdrofit.pl/kluby-fitness/{club}/grafik-zajec"
    st = await asyncio.to_thread(store.load)
    plain = await asyncio.to_thread(_probe_one, booker.make_session(st["cookies"]), url)
    full = await asyncio.to_thread(_probe_one, booker.make_session(st["cookies"]), url, _FULL_HEADERS)
    home = await asyncio.to_thread(_probe_one, booker.make_session({}), "https://zdrofit.pl/", _FULL_HEADERS)
    await update.effective_message.reply_text(
        f"{club}\n\n1) Обычный запрос:\n{plain}\n\n2) Как браузер:\n{full}\n\n3) Главная страница:\n{home}\n\n"
        f"Браузер (WAF): {waf.info}")


async def book_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    owner = _owner_id()
    if not owner or q.from_user.id != owner:
        await q.answer("Нет доступа", show_alert=True)
        return
    _, club, item_id = q.data.split(":", 2)
    await q.answer("Записываю…")
    text = await asyncio.to_thread(booker.book_now, club, item_id)
    msg = q.message
    card_html = msg.text_html or ""
    if text.startswith("⏳"):  # поставлено в очередь: запоминаем карточку, чтобы позже перерисовать
        def remember(st):
            for t in st["targets"]:
                if t["id"] == item_id:
                    t.update(chat_id=msg.chat_id, message_id=msg.message_id, card_html=card_html)
        await asyncio.to_thread(store.update, remember)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Жду открытия записи · отменить", callback_data=f"uq:{club}:{item_id}")]])
    elif text.startswith(("✅", "Вы уже записаны")):
        markup = DONE_MARKUP
    elif text.startswith(("🔑", "⚠️")):  # при ошибке кнопка остаётся
        markup = msg.reply_markup
    else:
        markup = None
    try:
        await q.edit_message_text(card_html + "\n\n" + html.escape(text), parse_mode="HTML",
                                  disable_web_page_preview=True, reply_markup=markup)
    except Exception:
        await msg.reply_text(text)


async def unqueue_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    owner = _owner_id()
    if not owner or q.from_user.id != owner:
        await q.answer("Нет доступа", show_alert=True)
        return
    _, club, item_id = q.data.split(":", 2)

    def rm(st):
        for t in list(st["targets"]):
            if t["id"] == item_id:
                st["targets"].remove(t)
                return t.get("card_html") or (q.message.text_html or "")
        return q.message.text_html or ""

    card_html = await asyncio.to_thread(store.update, rm)
    await q.answer("Автозапись отменена")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Записаться", callback_data=f"bk:{club}:{item_id}")]])
    try:
        await q.edit_message_text(card_html, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)
    except Exception:
        pass


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("Вы записаны на это занятие")


async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Ваш Telegram id: {update.effective_user.id}")


async def help_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP)


BTN_LIST, BTN_STATUS, BTN_PAUSE, BTN_RESUME, BTN_HELP = "📋 Список", "📊 Статус", "⏸ Пауза", "▶️ Продолжить", "❓ Помощь"
MENU = ReplyKeyboardMarkup(
    [[BTN_LIST, BTN_STATUS], [BTN_PAUSE, BTN_RESUME, BTN_HELP]],
    resize_keyboard=True, is_persistent=True)
_BUTTONS: dict = {}


@owner_only
async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает постоянные кнопки внизу экрана (/menu, а также после /start)."""
    await update.effective_message.reply_text("Кнопки включены ⬇️", reply_markup=MENU)


async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fn = _BUTTONS.get((update.effective_message.text or "").strip())
    if fn:
        await fn(update, context)


def register(app: Application) -> None:
    _BUTTONS.update({BTN_LIST: list_cmd, BTN_STATUS: status_cmd, BTN_PAUSE: _set_paused(True),
                     BTN_RESUME: _set_paused(False), BTN_HELP: help_booking})
    pattern = "^(" + "|".join(re.escape(b) for b in _BUTTONS) + ")$"
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(pattern), button_router))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("start", menu_cmd), group=1)  # после обычного ответа /start
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("pause", _set_paused(True)))
    app.add_handler(CommandHandler("resume", _set_paused(False)))
    app.add_handler(CommandHandler("cookie", cookie_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("probe", probe_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("booking", help_booking))
    app.add_handler(CallbackQueryHandler(book_callback, pattern=r"^bk:"))
    app.add_handler(CallbackQueryHandler(unqueue_callback, pattern=r"^uq:"))
    app.add_handler(CallbackQueryHandler(list_remove_callback, pattern=r"^lr:"))
    app.add_handler(CallbackQueryHandler(sync_callback, pattern=r"^sync$"))
    app.add_handler(CommandHandler("sync", sync_cmd))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))


async def run_tick(app: Application) -> list[str]:
    msgs = await asyncio.to_thread(booker.tick)
    while booker.CARD_UPDATES:  # перерисовываем карточки занятий, на которые бот только что записал
        u = booker.CARD_UPDATES.pop(0)
        try:
            await app.bot.edit_message_text(
                chat_id=u["chat_id"], message_id=u["message_id"],
                text=(u["html"] + "\n\n" if u["html"] else "") + "✅ Записан автоматически",
                parse_mode="HTML", disable_web_page_preview=True, reply_markup=DONE_MARKUP)
        except Exception:
            pass
    owner = _owner_id()
    if owner:
        for m in msgs:
            try:
                await app.bot.send_message(owner, m)
            except Exception:
                pass
    return msgs


async def tick_loop(app: Application, every: int = 60) -> None:
    """Для локального режима (polling): проверка по таймеру."""
    while True:
        try:
            await run_tick(app)
        except Exception:
            pass
        wait = every
        try:
            nh = (await asyncio.to_thread(store.load)).get("next_hot", 0)
            if nh:  # ближе к расчётному открытию записи просыпаемся чаще
                wait = min(every, max(5, nh - time.time()))
        except Exception:
            pass
        await asyncio.sleep(wait)
