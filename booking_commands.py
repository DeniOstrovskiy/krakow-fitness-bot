"""Telegram-команды для автозаписи: /watch /list /remove /pause /resume /status /myid."""
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
import pg
import store

DAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
DAY_MAP = {**{n: i for i, n in enumerate(DAY_NAMES)},
           **{n: i for i, n in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}}
FAVORITES = [c.strip() for c in os.getenv(
    "FAVORITE_CLUBS",
    "krakow-galeria-kazimierz,krakow-high5ive,krakow-kapelanka,krakow-garden-residence,krakow-przybyszewskiego,krakow-dytmara,krakow-lindego,krakow-jasnogorska,krakow-galicyjska",
).split(",") if c.strip()]

CLUB_TITLES = booker.CLUB_TITLES


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
    "/status — состояние бота\n"
    "/pgtest — проверка входа и клубов (ничего не записывает)"
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


def resolve_activity(club: str, q: str):
    """Название занятия для /watch берём из расписания PerfectGym (текущая и следующая недели)."""
    cid = pg.club_id(club)
    if not cid:
        return None, "Клуб не найден в PerfectGym."
    today = booker.now_local().date()
    names: dict[str, str] = {}
    for k in (0, 7):
        for c in pg.weekly(cid, today + dt.timedelta(days=k))[0]:
            if c.get("Name"):
                names.setdefault(pg.norm(c["Name"]), c["Name"])
    n = pg.norm(q)
    exact = [v for k, v in names.items() if k == n]
    hits = exact or [v for k, v in names.items() if n and n in k]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, "Такого занятия в расписании клуба нет. Есть: " + ", ".join(sorted(names.values()))
    return None, "Уточните, их несколько: " + ", ".join(sorted(hits))


def _sync_booked() -> str:
    """Сверяет «Записан» с PerfectGym: убирает занятия, на которые вы уже не записаны."""
    st = store.load()
    now = booker.now_local().isoformat()
    todo = [b for b in st["booked"] if b["start"] > now]
    if not todo:
        return "Сверять нечего."
    if not pg.enabled():
        return "Не заданы PG_LOGIN и PG_PASSWORD, сверять не с чем."
    try:
        return _sync_booked_pg(todo)
    except Exception as e:  # noqa: BLE001
        return f"⚠️ Сверка через PerfectGym не удалась: {e!r}"


@owner_only
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = await asyncio.to_thread(store.load)
    last = f"{int(time.time() - st['last_tick'])} с назад" if st["last_tick"] else "ещё не было"
    mode = f"PerfectGym API ({pg.mask_login()})" if pg.enabled() else "не задан вход (PG_LOGIN / PG_PASSWORD)"
    text = (f"Режим записи: {mode}\nОтслеживаний: {len(st['watches'])}\n"
            f"В очереди на автозапись: {len(st['targets'])}\n"
            f"Пауза: {'да' if st['paused'] else 'нет'}\nПоследняя проверка: {last}")
    if st.get("next_hot"):
        left = int(st["next_hot"] - time.time())
        if left > 0:
            text += f"\nЧастый опрос начнётся через {left // 3600} ч {left % 3600 // 60} мин"
    if st.get("open_observed"):
        o = st["open_observed"]
        text += f"\nПоследнее наблюдённое открытие: {booker.flat(o['label'])} за {o['hours_before']} ч до начала"
    if st["last_response"]:
        text += f"\n\nОтвет сервера на последнюю запись:\n{st['last_response'][:600]}"
    await update.effective_message.reply_text(text)


def _build_list(st):
    """Возвращает (HTML-текст, клавиатура)."""
    head = "<b>Отслеживаю:</b>" if (st["watches"] or st["targets"]) else "Отслеживание пусто. Добавьте через /watch"
    blocks = [head]
    blocks += [f"{i}. {booker.rich(describe(w))}" for i, w in enumerate(st["watches"], 1)]
    rows = []
    for i, t in enumerate(st["targets"], len(st["watches"]) + 1):
        blocks.append(f"{i}. ⏳ {booker.rich_label(t['label'])}\n(ждёт, когда можно будет записаться)")
        rows.append([InlineKeyboardButton(f"❌ Отменить {i}", callback_data=f"lr:{t['id']}")])
    future = [b for b in st["booked"] if b["start"] > booker.now_local().isoformat()]
    if future:
        blocks.append("<b>Записан:</b>")
        blocks += [f"📅 {booker.rich_label(b['label'])}"
                   for b in sorted(future, key=lambda b: b["start"])]
        rows.append([InlineKeyboardButton("🔄 Сверить «Записан» с сайтом", callback_data="sync")])
    if st["paused"]:
        blocks.append("⏸ Бот на паузе")
    return "\n\n".join(blocks), (InlineKeyboardMarkup(rows) if rows else None)


@owner_only
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notes = []
    st0 = await asyncio.to_thread(store.load)
    if st0["booked"]:  # каждый раз сверяем «Записан» с сайтом; при сбое просто показываем список как есть
        try:
            rep = await asyncio.wait_for(asyncio.to_thread(_sync_booked), 30)
            notes = [html.escape(l) for l in rep.splitlines() if l.startswith("✖")]
        except Exception:  # noqa: BLE001
            pass
    st = await asyncio.to_thread(store.load)
    text, markup = _build_list(st)
    if notes:
        text = "\n".join(notes) + "\n\n" + text
    await update.effective_message.reply_text(text, reply_markup=markup, parse_mode="HTML")


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
        await q.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        pass


def _sync_booked_pg(todo: list) -> str:
    """Сверка «Записан» через API PerfectGym: статус занятия смотрим с вашим токеном."""
    pg.login()
    removed, kept, unknown, upd = [], 0, [], {}
    for b in todo:
        pid = b.get("pg_id")
        if not pid:  # запись сделана через сайт: ищем это занятие в PerfectGym по клубу, времени и названию
            club = b.get("club") or booker.club_of(b["label"])
            lines = booker.norm_label(b["label"]).split("\n")
            name = lines[2].split(" - ")[0] if len(lines) >= 3 else ""
            c = pg.find_class(club, dt.datetime.fromisoformat(b["start"]), name) if club and name else None
            pid = c["pg_id"] if c else None
            if pid:
                upd[b["id"]] = {"pg_id": pid, "dur": c.get("dur")}
        if not pid:
            unknown.append(booker.flat(b["label"]))
            continue
        status = pg.details(pid).get("Status")
        if pg.state_of(status, None) == "booked":
            kept += 1
        elif status == "Bookable":  # занятие снова доступно для записи — значит, вы отписались
            removed.append(b)
        else:
            unknown.append(f"{booker.flat(b['label'])} (статус {status})")
    if removed or upd:
        ids = {b["id"] for b in removed}

        def apply(x):
            x["booked"] = [dict(b, **{k: v for k, v in upd.get(b["id"], {}).items() if v and not b.get(k)})
                           for b in x["booked"] if b["id"] not in ids]
        store.update(apply)
    out = [f"Сверка с PerfectGym: подтверждено {kept}, снято {len(removed)}."]
    out += [f"✖ Снято (вы не записаны): {booker.flat(b['label'])}" for b in removed]
    out += [f"? Не уверен: {l}" for l in unknown]
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
        await q.edit_message_text(t, reply_markup=markup, parse_mode="HTML")
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
async def calendar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import cal
    base = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if not base:
        await update.effective_message.reply_text("Адрес сервиса не задан (WEBHOOK_URL / RENDER_EXTERNAL_URL).")
        return
    url = f"{base}/calendar.ics?key={cal.feed_key()}"
    await update.effective_message.reply_text(
        "Календарь с вашими записями (обновляется сам, события появляются с задержкой от минут до нескольких часов).\n\n"
        f"Ссылка:\n{url}\n\n"
        "iPhone: Настройки → Календарь → Учётные записи → Добавить → Другое → Подписной календарь → вставьте ссылку.\n"
        "Google: calendar.google.com в браузере → «Другие календари» (+) → «По URL» → вставьте ссылку "
        "(в приложении Google Calendar этого пункта нет, но после добавления календарь появится и там).\n\n"
        "Ссылку не публикуйте: по ней видны все ваши записи.",
        disable_web_page_preview=True)


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
            return booker.flat(st["targets"].pop(k)["label"])

    w = await asyncio.to_thread(store.update, rm)
    await update.effective_message.reply_text(f"Удалено: {w}" if w else "Нет такого номера")


def _set_paused(value: bool):
    @owner_only
    async def cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await asyncio.to_thread(store.update, lambda st: st.update(paused=value))
        await update.effective_message.reply_text("⏸ Пауза" if value else "▶️ Возобновлено")
    return cmd














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
        await q.edit_message_text(card_html + "\n\n" + booker.rich(text), parse_mode="HTML",
                                  disable_web_page_preview=True, reply_markup=markup)
    except Exception:
        await msg.reply_text(booker.rich(text), parse_mode="HTML")


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
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("✍️ Записаться", callback_data=f"bk:{club}:{item_id}")]])
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
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("pgtest", pgtest_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("booking", help_booking))
    app.add_handler(CallbackQueryHandler(book_callback, pattern=r"^bk:"))
    app.add_handler(CallbackQueryHandler(unqueue_callback, pattern=r"^uq:"))
    app.add_handler(CallbackQueryHandler(list_remove_callback, pattern=r"^lr:"))
    app.add_handler(CallbackQueryHandler(sync_callback, pattern=r"^sync$"))
    app.add_handler(CommandHandler("sync", sync_cmd))
    app.add_handler(CommandHandler("calendar", calendar_cmd))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))


def _pgtest() -> str:
    """Проверка API PerfectGym без записи: вход, сопоставление клубов, образец расписания."""
    if not pg.enabled():
        return "PG_LOGIN и PG_PASSWORD не заданы (Render → Environment). Без них бот не может записывать."
    out = [f"Логин: {pg.mask_login()}"]
    try:
        pg.login(force=True)
        ti = pg.token_info()
        out.append(f"✅ Вход выполнен. Токен действует ещё ~{(ti['expires_in'] or 0) // 60} мин, номер пользователя найден ({ti['uid_src']}).")
    except pg.PGAuthError as e:
        out.append(f"❌ Вход не удался: {e}")
    except Exception as e:  # noqa: BLE001
        out.append(f"⚠️ PerfectGym не отвечает: {e!r}")
        return "\n".join(out)
    first = None
    out.append("\nКлубы:")
    for c in FAVORITES:
        try:
            cands = pg.club_candidates(c)
            cid = pg.club_id(c)
        except Exception as e:  # noqa: BLE001
            out.append(f"⚠️ {booker.club_title(c)}: {e!r}")
            continue
        if cid:
            first = first or (c, cid)
            name = next((x["FullName"] for x in cands if x["Id"] == cid), "из PG_CLUB_IDS")
            out.append(f"✅ {booker.club_title(c)} → {cid} ({name})")
        elif cands:
            out.append(f"⚠️ {booker.club_title(c)}: несколько вариантов, укажите PG_CLUB_IDS: " + "; ".join(f"{x['Id']} {x['FullName']}" for x in cands))
        else:
            out.append(f"❌ {booker.club_title(c)}: не найден")
    if first:
        try:
            raw, _, _ = pg.weekly(first[1], booker.now_local().date())
            from collections import Counter
            cnt = Counter(f"{x.get('Status')}" + (f"/{x['StatusReason']}" if x.get("StatusReason") else "") for x in raw)
            out.append(f"\nРасписание {booker.club_title(first[0])} на неделю: {len(raw)} занятий. Статусы: "
                       + ", ".join(f"{k} ×{v}" for k, v in cnt.most_common()))
        except Exception as e:  # noqa: BLE001
            out.append(f"⚠️ Расписание: {e!r}")
    st = store.load()
    mine = [b for b in st["booked"] if b.get("pg_id") and b["start"] > booker.now_local().isoformat()][:3]
    for b in mine:
        try:
            out.append(f"Мой статус «{booker.flat(b['label'])}»: {pg.details(b['pg_id']).get('Status')}")
        except Exception as e:  # noqa: BLE001
            out.append(f"⚠️ Детали занятия: {e!r}")
    out.append("\nЗаписей этой командой не делается.")
    return "\n".join(out)


@owner_only
async def pgtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Проверяю PerfectGym…")
    try:
        text = await asyncio.wait_for(asyncio.to_thread(_pgtest), 60)
    except Exception as e:  # noqa: BLE001
        text = f"⚠️ Проверка не завершилась: {e!r}"
    await update.effective_message.reply_text(text[:3900])


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
                await app.bot.send_message(owner, booker.rich(m), parse_mode="HTML")
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
