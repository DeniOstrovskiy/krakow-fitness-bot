"""Логика записи на занятия Zdrofit (HTML-расписание клуба + форма записи).

Вход на сайт защищён reCAPTCHA, поэтому бот использует cookie сессии, которую
пользователь присылает командой /cookie.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import pg
import store

TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Warsaw"))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
HORIZON_DAYS = int(os.getenv("MAX_DAYS_AHEAD", "14"))
OPEN_BEFORE_HOURS = float(os.getenv("OPEN_BEFORE_HOURS", "48"))  # запись открывается за N часов до начала
BURST_SECONDS = int(os.getenv("BURST_SECONDS", "55"))            # сколько длится частый опрос в «горячем» окне
BURST_EVERY = float(os.getenv("BURST_EVERY", "1"))               # интервал частого опроса, секунды
FAST_LEAD = float(os.getenv("FAST_LEAD", "10"))       # быстрая фаза начинается за N с до расчётного открытия
FAST_EVERY = float(os.getenv("FAST_EVERY", "0.25"))  # пауза между пробами формы, секунды
FAST_SECONDS = float(os.getenv("FAST_SECONDS", "20"))  # сколько длится быстрая фаза
FAST_POSTS = int(os.getenv("FAST_POSTS", "30"))      # максимум отправок формы на занятие за фазу
FAST_PARALLEL = int(os.getenv("FAST_PARALLEL", "2"))  # параллельных потоков на одно занятие в быстрой фазе
FAST_AFTER = float(os.getenv("FAST_AFTER", "30"))      # быстрая фаза работает до N с после расчётного открытия (дальше только обычный опрос)
KEEPALIVE_SECONDS = int(os.getenv("KEEPALIVE_SECONDS", "1200"))  # как часто поддерживать сессию, когда очередь пуста
DOWN_ALERT_SECONDS = int(os.getenv("DOWN_ALERT_SECONDS", "300"))   # писать о недоступности сайта, если сбой дольше N секунд
DOWN_REPEAT_SECONDS = int(os.getenv("DOWN_REPEAT_SECONDS", "7200"))  # и напоминать не чаще раза в N секунд
WARM_LEAD = 10     # за столько секунд до открытия делаем «прогревающий» запрос
HOT_LEAD = 70      # «горячее» окно начинается за 70 с до расчётного открытия
HOT_TAIL = 600     # и длится 10 минут после (если сайт открывает с задержкой)


LOCK = threading.Lock()
CARD_UPDATES: list[dict] = []  # карточки в Telegram, которые надо перерисовать как «записан»






def now_local() -> dt.datetime:
    return dt.datetime.now(TZ).replace(tzinfo=None)












def matches(it: dict, w: dict) -> bool:
    if it["club"] != w["club"] or it["activity"] != w["activity"]:
        return False
    if w.get("trainer") and w["trainer"].lower() not in it["trainer"].lower():
        return False
    if w.get("days") and it["start"].weekday() not in w["days"]:
        return False
    hhmm = it["start"].strftime("%H:%M")
    if w.get("time_from") and hhmm < w["time_from"]:
        return False
    if w.get("time_to") and hhmm > w["time_to"]:
        return False
    return True


CLUB_TITLES = {
    "krakow-galeria-kazimierz": "Galeria Kazimierz",
    "krakow-high5ive": "High5ive",
    "krakow-kapelanka": "Kapelanka",
    "krakow-garden-residence": "Garden Residence",
    "krakow-przybyszewskiego": "Przybyszewskiego",
    "krakow-dytmara": "Dytmara",
    "krakow-lindego": "Lindego 1c",
    "krakow-jasnogorska": "Jasnogórska 62",
    "krakow-galicyjska": "Atut Galicyjska 18",
}
_DOW_EN = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
_OLD_LABEL = re.compile(r"^(?P<name>.*?) (?P<d>" + _DOW_EN + r") (?P<dm>\d\d\.\d\d) (?P<t>\d\d:\d\d), "
                        r"(?P<club>[\w-]+) \((?P<tr>[^()]*)\)$")
_BLOCK = re.compile(r"^([^\n]+)\n(" + _DOW_EN + r" \d\d\.\d\d \d\d:\d\d)\n([^\n]+)$", re.M)


def club_title(slug: str) -> str:
    return CLUB_TITLES.get(slug) or slug.removeprefix("krakow-").replace("-", " ").title()


def _person(s: str) -> str:
    s = (s or "").replace("_", " ").strip()
    return s.title() if s and (s == s.lower() or s == s.upper()) else s


def label(it: dict) -> str:
    """Три строки: клуб / «Mon 12.10 10:00» / «Название - Тренер»."""
    tr = f" - {_person(it.get('trainer'))}" if it.get("trainer") else ""
    return f"{club_title(it['club'])}\n{it['start']:%a %d.%m %H:%M}\n{it['name']}{tr}"


def norm_label(s: str) -> str:
    """Старые подписи («Название Mon 05.10 08:00, клуб (тренер)») приводит к новому виду."""
    m = _OLD_LABEL.match(s or "")
    if not m:
        return s
    tr = m["tr"].strip()
    tr = f" - {_person(tr)}" if tr and tr != "—" else ""
    return f"{club_title(m['club'])}\n{m['d']} {m['dm']} {m['t']}\n{m['name']}{tr}"


def _block_html(m) -> str:
    third = m.group(3)
    name, sep, tr = third.partition(" - ")
    return f"📍 <b>{m.group(1)}</b>\n<b>{m.group(2)[:-6]} — {m.group(2)[-5:]}</b>\n💪 <b>{name}</b>{sep}{tr}"


def rich_label(lab: str) -> str:
    """Подпись занятия в HTML: клуб, дата со временем и название жирным."""
    import html as _h
    lab = _h.escape(norm_label(lab))
    out = _BLOCK.sub(_block_html, lab)
    return out


def rich(text: str) -> str:
    """Экранирует текст для Telegram HTML и оформляет подписи занятий внутри него."""
    import html as _h
    return _BLOCK.sub(_block_html, _h.escape(text))


def flat(s: str) -> str:
    return norm_label(s).replace("\n", ", ")


def club_of(lab: str):
    m = _OLD_LABEL.match(lab or "")
    if m:
        return m["club"]
    first = (lab or "").split("\n", 1)[0]
    for slug, title in CLUB_TITLES.items():
        if title == first:
            return slug
    return None










def tick() -> list[str]:
    with LOCK:
        return _tick_locked()






def _daily_alert(key: str, text: str):
    """Сообщение не чаще раза в сутки по ключу key. Возвращает текст или None."""
    today = now_local().strftime("%Y-%m-%d")

    def mark(x):
        if x.get(key) != today:
            x[key] = today
            return True
        return False
    return text if store.update(mark) else None


def _note_down(e, what: str):
    """Учитывает сбой источника; сообщение возвращается, только если сбой длится дольше DOWN_ALERT_SECONDS."""
    def mark_err(x):
        t = time.time()
        if not x.get("err_since"):
            x["err_since"] = t
        down = t - x["err_since"]
        if down < DOWN_ALERT_SECONDS:
            return None
        first = not x.get("down_alerted")
        if first or t - x.get("err_alert", 0) > DOWN_REPEAT_SECONDS:
            x["down_alerted"] = True
            x["err_alert"] = t
            return int(down // 60)
        return None

    mins = store.update(mark_err)
    if mins is None:
        return None
    return (f"⚠️ {what} не отвечает уже {mins} мин. Последняя ошибка: {e!r}"[:400]
            + "\nБот продолжает пробовать, очередь автозаписи сохранена.")


def _note_up(what: str):
    def mark_ok(x):
        was = bool(x.get("down_alerted"))
        x["err_since"] = 0
        x["down_alerted"] = False
        return was
    return f"✅ {what} снова отвечает, проверка расписания идёт в обычном режиме." if store.update(mark_ok) else None


def matches_pg(it: dict, w: dict) -> bool:
    """Как matches(), но название занятия сравнивается в нормализованном виде (слаг сайта и название API)."""
    if pg.norm(w["activity"]) != it["activity"]:
        return False
    return matches(dict(it, activity=w["activity"]), w)


def _fetch_pg(ids: dict, clubs: list[str], dates: dict | None = None) -> list[dict]:
    """Расписание клубов через API (параллельно). dates: {клуб: {даты}} — только эти недели (горячий опрос)."""
    today = dt.datetime.now(TZ).date()
    jobs = []
    for c in clubs:
        ds = (dates or {}).get(c) or [today + dt.timedelta(days=7 * k) for k in range(HORIZON_DAYS // 7 + 1)]
        jobs += [(c, d) for d in ds]
    out: list[dict] = []
    seen: set = set()
    with ThreadPoolExecutor(max_workers=6) as ex:
        for (c, _), (items, _n, _f) in zip(jobs, ex.map(lambda j: pg.weekly(ids[j[0]], j[1]), jobs)):
            for raw in items:
                if raw["Id"] not in seen:
                    seen.add(raw["Id"])
                    out.append(pg.to_item(c, raw))
    return out


def _tick_pg(st: dict):
    """Тик через API PerfectGym. Возвращает (True, сообщения)."""
    msgs: list[str] = []
    try:
        pg.login()
    except pg.PGAuthError as e:
        m = _daily_alert("pg_alert", f"🔑 Вход в PerfectGym не удался: {e}. Проверьте PG_LOGIN и PG_PASSWORD в Render.")
        return True, ([m] if m else [])
    except Exception as e:  # noqa: BLE001 — сеть
        m = _note_down(e, "API PerfectGym")
        return True, ([m] if m else [])

    watches, targets = st["watches"], st["targets"]
    clubs = sorted({w["club"] for w in watches} | {t["club"] for t in targets})
    try:
        ids = {c: pg.club_id(c) for c in clubs}
    except Exception as e:  # noqa: BLE001
        m = _note_down(e, "API PerfectGym")
        return True, ([m] if m else [])
    missing = [c for c in clubs if not ids[c]]
    if missing:
        m = _daily_alert("pg_alert2", "⚠️ Не нашёл в PerfectGym клуб(ы): " + ", ".join(club_title(c) for c in missing)
                         + ". Задайте PG_CLUB_IDS в Render, например krakow-dytmara=299 (список: /pgtest).")
        msgs += [m] if m else []
    work = [c for c in clubs if ids[c]]
    if not work:
        return True, msgs

    started = time.time()
    booked_ids = {b["id"] for b in st["booked"]}
    booked_pg = {b.get("pg_id") for b in st["booked"] if b.get("pg_id")}
    new_booked: list[dict] = []
    alerts: dict = {}
    pg_of: dict = {}          # id цели на сайте -> id занятия в PerfectGym
    fails = dict(st.get("pg_fail") or {})   # pg_id (строкой) -> текст последней ошибки, чтобы не сообщать повторно
    fails_now: dict = {}
    next_hot = 0.0
    seen_early: set = set()
    observed = None
    last_resp = None
    failed = False
    rec_lock = threading.Lock()

    def target_of(it):
        return next((t for t in targets if pg.same_class(t, it)), None)

    def record(it, t) -> bool:
        with rec_lock:
            if it["pg_id"] in booked_pg:
                return False
            booked_pg.add(it["pg_id"])
            site_id = t["id"] if t else it["id"]
            booked_ids.add(site_id)
            lab = t["label"] if t and t.get("label") else label(it)
            new_booked.append({"id": site_id, "pg_id": it["pg_id"], "label": lab, "start": it["start"].isoformat(),
                               "club": it["club"], "dur": it.get("dur")})
            msgs.append(f"✅ Записан\n{lab}")
            if t and t.get("message_id"):
                CARD_UPDATES.append({"chat_id": t["chat_id"], "message_id": t["message_id"], "html": t.get("card_html", "")})
            return True

    def refuse(it, msg):
        key = str(it["pg_id"])
        fails_now[key] = msg
        if fails.get(key) != msg:
            msgs.append(f"⚠️ Не удалось записаться\n{label(it)}\n{msg}")

    def attempt(it, t) -> bool:
        nonlocal last_resp
        ok, msg, raw = pg.book(it["pg_id"], it["pg_club"])
        last_resp = raw
        if ok:
            record(it, t)
            fails.pop(str(it["pg_id"]), None)
        else:
            refuse(it, msg)
        return ok

    def fast_phase(due: list) -> None:
        """Быстрая фаза: потоки опрашивают детали занятия и записывают, как только статус стал «Bookable»."""
        nonlocal last_resp
        t_end = time.time() + FAST_SECONDS
        state = {it["pg_id"]: {"done": threading.Event(), "posts": 0, "lock": threading.Lock(), "msg": ""} for it, _ in due}
        auth_err: list = []

        def worker(it, t, idx):
            nonlocal last_resp
            ss = state[it["pg_id"]]
            time.sleep(idx * FAST_EVERY / max(1, FAST_PARALLEL))
            while time.time() < t_end and not ss["done"].is_set() and not auth_err:
                try:
                    status = pg.details(it["pg_id"]).get("Status")
                except Exception:  # noqa: BLE001 — сеть: пробуем дальше
                    status = None
                if status in ("FullBooked", "Awaitable"):
                    return  # мест нет: дальше разберётся обычный опрос
                if status and pg.state_of(status, None) == "booked":
                    ss["done"].set()
                    return
                if status == "Bookable":
                    with ss["lock"]:
                        if ss["done"].is_set() or ss["posts"] >= FAST_POSTS:
                            return
                        try:
                            ok, msg, raw = pg.book(it["pg_id"], it["pg_club"])
                        except pg.PGAuthError as e:
                            auth_err.append(e)
                            return
                        except Exception as e:  # noqa: BLE001
                            ok, msg, raw = False, None, ""
                        if raw:
                            last_resp = raw
                        if ok:
                            record(it, t)
                            ss["done"].set()
                            return
                        if msg is not None:
                            ss["posts"] += 1
                            ss["msg"] = msg
                time.sleep(FAST_EVERY)

        threads = [threading.Thread(target=worker, args=(it, t, k), daemon=True)
                   for it, t in due for k in range(max(1, FAST_PARALLEL))]
        for th in threads:
            th.start()
        for th in threads:
            th.join(max(0.1, t_end + 8 - time.time()))
        if auth_err:
            raise auth_err[0]
        for it, _ in due:
            ss = state[it["pg_id"]]
            if not ss["done"].is_set() and ss["posts"] >= FAST_POSTS:
                refuse(it, ss["msg"] or "сервер отклонил запись")

    try:
        clubs_now = work
        only_dates = None
        while True:
            now = now_local()
            horizon = now + dt.timedelta(days=HORIZON_DAYS)
            items = _fetch_pg(ids, clubs_now, only_dates)
            hot: dict = {}
            wait_hot = None
            fast_due: list = []
            for it in items:
                if not (now < it["start"] <= horizon) or it["pg_id"] in booked_pg:
                    continue
                t = target_of(it)
                if not (t or any(matches_pg(it, w) for w in watches)):
                    continue
                if t:
                    pg_of[t["id"]] = it["pg_id"]
                stt = it["state"]
                if stt == "booked":
                    continue
                if stt == "early":
                    seen_early.add(it["pg_id"])
                    secs = (it["start"] - dt.timedelta(hours=OPEN_BEFORE_HOURS) - now).total_seconds()
                    if -HOT_TAIL <= secs <= HOT_LEAD:
                        hot.setdefault(it["club"], set()).add(it["start"].date())
                        if -FAST_AFTER <= secs <= FAST_LEAD:
                            fast_due.append((it, t))
                    elif secs > HOT_LEAD:
                        w = secs - HOT_LEAD
                        wait_hot = w if wait_hot is None else min(wait_hot, w)
                    continue
                if stt != "open":
                    if t:
                        if stt == "full":
                            reason = "Запись открылась, но мест уже нет (скорее всего, заняли в первые секунды). Продолжу следить: если кто-то отпишется, запишу."
                        elif stt == "waitlist":
                            reason = "Мест нет, есть лист ожидания. Продолжу следить: если место освободится, запишу."
                        elif stt == "closed":
                            reason = "Запись уже закрыта."
                        else:
                            reason = f"Статус «{it['status']} / {it['reason']}», бот не умеет с ним работать."
                        if t.get("alerted") != reason and t["id"] not in alerts:
                            alerts[t["id"]] = reason
                            msgs.append(f"⚠️ Не записал\n{label(it)}\n{reason}")
                    continue
                if it["pg_id"] in seen_early and observed is None:
                    observed = {"label": label(it), "hours_before": round((it["start"] - now).total_seconds() / 3600, 2),
                                "at": now.isoformat(timespec="seconds")}
                attempt(it, t)
            if fast_due:
                fast_phase(fast_due)
            if wait_hot is not None:
                next_hot = time.time() + wait_hot
            if hot and time.time() - started < BURST_SECONDS:
                clubs_now = sorted(hot)
                only_dates = {c: sorted(ds) for c, ds in hot.items()}
                time.sleep(BURST_EVERY)
                continue
            break
    except pg.PGAuthError as e:
        m = _daily_alert("pg_alert", f"🔑 PerfectGym отклонил вход: {e}. Проверьте PG_LOGIN и PG_PASSWORD в Render.")
        if m:
            msgs.append(m)
    except Exception as e:  # noqa: BLE001
        failed = True
        m = _note_down(e, "API PerfectGym")
        if m:
            msgs.append(m)
    if not failed:
        m = _note_up("API PerfectGym")
        if m:
            msgs.append(m)

    now = now_local()

    def save_all(x):
        x["booked"] = [b for b in x["booked"] + new_booked if b["start"] > (now - dt.timedelta(days=30)).isoformat()]
        x["targets"] = [t for t in x["targets"] if t["id"] not in booked_ids and t["start"] > now.isoformat()]
        for t in x["targets"]:
            if t["id"] in alerts:
                t["alerted"] = alerts[t["id"]]
            if t["id"] in pg_of:
                t["pg_id"] = pg_of[t["id"]]
        x["pg_fail"] = {k: v for k, v in {**fails, **fails_now}.items() if k in fails_now}
        x["next_hot"] = next_hot
        if observed:
            x["open_observed"] = observed
        if last_resp:
            x["last_response"] = last_resp
    store.update(save_all)
    return True, msgs


def _tick_locked() -> list[str]:
    st = store.load()
    if st["paused"] or not (st["watches"] or st["targets"]):
        return []
    if time.time() - st.get("last_tick", 0) < 20:  # защита от двойных пингов
        return []
    if not pg.enabled():
        m = _daily_alert("pg_alert", "🔑 Не заданы PG_LOGIN и PG_PASSWORD в Render: без них бот не может записывать.")
        return [m] if m else []
    store.update(lambda x: x.update(last_tick=time.time()))
    return _tick_pg(st)[1]


def _book_class(st: dict, c: dict, site_id: str, club: str, lab: str, fallback: bool = False):
    """Записывает на занятие PerfectGym сразу или ставит в очередь автозаписи."""
    if c["state"] == "booked" or c["pg_id"] in {b.get("pg_id") for b in st["booked"]}:
        return f"Вы уже записаны\n{lab}"
    if c["state"] == "closed" or c["start"] <= now_local():
        return f"⛔ Запись закрыта\n{lab}"
    if c["state"] == "open":
        try:
            ok, msg, raw = pg.book(c["pg_id"], c["pg_club"])
        except pg.PGAuthError as e:
            return None if fallback else f"🔑 Вход в PerfectGym не удался: {e}"
        except Exception as e:  # noqa: BLE001
            return f"⚠️ Ошибка записи: {e!r}"

        def save(x):
            x["last_response"] = raw
            if ok:
                x["booked"].append({"id": site_id, "pg_id": c["pg_id"], "label": lab, "start": c["start"].isoformat(),
                                    "club": club, "dur": c.get("dur")})
                x["targets"] = [t for t in x["targets"] if t["id"] != site_id and t.get("pg_id") != c["pg_id"]]
        store.update(save)
        return f"✅ Записан\n{lab}" if ok else f"⚠️ Не удалось записаться\n{lab}\n{msg}"
    if c["state"] in ("early", "full", "waitlist"):
        def queue(x):
            if not any(t["id"] == site_id or t.get("pg_id") == c["pg_id"] for t in x["targets"]):
                x["targets"].append({"id": site_id, "club": club, "label": lab, "start": c["start"].isoformat(),
                                     "pg_id": c["pg_id"]})
        store.update(queue)
        why = ("запись ещё не открыта (обычно открывается за ~48 ч до начала)" if c["state"] == "early"
               else "мест нет" + (", есть лист ожидания" if c["state"] == "waitlist" else ""))
        return f"⏳ {lab}\n{why}. Поставил на автозапись: запишу, как только станет возможно."
    return f"Статус занятия «{c['status']} / {c['reason']}», записаться пока нельзя\n{lab}"


def _book_now_api(club: str, pg_id: int) -> str:
    """Кнопка «Записаться» для карточки из API: занятие известно по номеру PerfectGym, сайт не нужен."""
    with LOCK:
        st = store.load()
        if not pg.enabled():
            return "Для записи через API задайте PG_LOGIN и PG_PASSWORD в Render."
        try:
            pg.login()
            c = pg.get_class(club, pg_id)
        except pg.PGAuthError as e:
            return f"🔑 Вход в PerfectGym не удался: {e}"
        except Exception as e:  # noqa: BLE001
            return f"⚠️ PerfectGym не отвечает: {e!r}"
        if c is None:
            return "Это занятие уже не найдено в расписании."
        return _book_class(st, c, c["id"], club, label(c))


def book_now(club: str, item_id: str) -> str:
    """Кнопка «Записаться»: записывает сразу, либо ставит занятие в очередь автозаписи."""
    if item_id.startswith("pg") and item_id[2:].isdigit():
        return _book_now_api(club, int(item_id[2:]))
    return "⚠️ Эта кнопка устарела. Повторите поиск, и появятся новые карточки."
