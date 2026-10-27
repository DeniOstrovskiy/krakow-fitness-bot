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
from zoneinfo import ZoneInfo

import requests

import waf
from bs4 import BeautifulSoup

import store

BASE = "https://zdrofit.pl"
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
DOWN_ALERT_SECONDS = int(os.getenv("DOWN_ALERT_SECONDS", "300"))   # писать о недоступности сайта, если сбой дольше N секунд
DOWN_REPEAT_SECONDS = int(os.getenv("DOWN_REPEAT_SECONDS", "7200"))  # и напоминать не чаще раза в N секунд
WARM_LEAD = 10     # за столько секунд до открытия делаем «прогревающий» запрос
HOT_LEAD = 70      # «горячее» окно начинается за 70 с до расчётного открытия
HOT_TAIL = 600     # и длится 10 минут после (если сайт открывает с задержкой)


LOCK = threading.Lock()
CARD_UPDATES: list[dict] = []  # карточки в Telegram, которые надо перерисовать как «записан»


class AuthError(Exception):
    pass


class BlockedError(Exception):
    pass


def now_local() -> dt.datetime:
    return dt.datetime.now(TZ).replace(tzinfo=None)


def parse_cookie_string(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^cookie:\s*", "", raw, flags=re.I)
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k.strip()] = v.strip()
    return out


def make_session(cookies: dict) -> requests.Session:
    s = waf.new_session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "pl,en;q=0.8"})
    for k, v in cookies.items():
        s.cookies.set(k, v, domain="zdrofit.pl")
    return s


def _logged_out(html: str) -> bool:
    if os.getenv("SKIP_AUTH_CHECK") == "1":
        return False
    return 'name="member_login_form"' in html


def fetch_club(s: requests.Session, club: str, require_auth: bool = True) -> list[dict]:
    r = s.get(f"{BASE}/kluby-fitness/{club}/grafik-zajec", timeout=20)
    if r.status_code == 404:
        raise ValueError(f"клуб '{club}' не найден")
    r.raise_for_status()
    if r.status_code != 200 or len(r.text) < 2000:
        raise BlockedError(f"сайт вернул HTTP {r.status_code}, {len(r.text)} байт (возможна защита от ботов)")
    if require_auth and _logged_out(r.text):
        raise AuthError("session expired")
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for li in soup.select("li.club-schedule-item"):
        t = li.find("time")
        if not t or not t.get("datetime"):
            continue
        reg = li.select_one(".registration")
        dm = re.search(r"(\d+)\s*min", (li.select_one(".infos").get_text(" ", strip=True) if li.select_one(".infos") else ""))
        a = li.select_one("a.activity")
        items.append({
            "id": li["data-id"],
            "club": club,
            "url": li.get("data-url"),
            "activity": li.get("data-activity", ""),
            "name": a.get_text(strip=True) if a else li.get("data-activity", ""),
            "trainer": li.get("data-trainer", "").replace("_", " "),
            "start": dt.datetime.fromisoformat(t["datetime"]),
            "full": li.get("data-full") == "true",
            "status": reg.get_text(" ", strip=True) if reg else "",
            "dur": int(dm.group(1)) if dm else None,
        })
    return items


def state_of(it: dict) -> str:
    s = it["status"].lower()
    if "wypisz" in s or "zapisan" in s:
        return "booked"
    if "wcześnie" in s:
        return "early"
    if "minął" in s:
        return "closed"
    if "zapisz się" in s:
        return "open"
    return "unknown"


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
    return s.title() if s and s == s.lower() else s


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


def _xhr(it: dict) -> dict:
    return {"X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE}/kluby-fitness/{it['club']}/grafik-zajec"}


def get_form(s: requests.Session, it: dict):
    """GET страницы формы занятия. Возвращает данные формы (dict) или None, если формы нет."""
    r = s.get(BASE + it["url"], headers=_xhr(it), timeout=20)
    r.raise_for_status()
    form = BeautifulSoup(r.text, "html.parser").find("form", {"name": "schedule_register_form"})
    if not form:
        if _logged_out(r.text):
            raise AuthError("session expired")
        return None
    data = {i["name"]: i.get("value", "") for i in form.find_all("input") if i.get("name")}
    btn = form.find("button", {"type": "submit"})
    if btn and btn.get("name"):
        data[btn["name"]] = btn.get("value", "")
    return data


def post_form(s: requests.Session, it: dict, data: dict, quiet: bool = False) -> tuple[bool, str, str]:
    """Отправляет форму. Возвращает (успех, сообщение, текст ответа сервера)."""
    r2 = s.post(BASE + it["url"], data=data, headers=_xhr(it), timeout=20)
    if _logged_out(r2.text):
        raise AuthError("session expired")
    s2 = BeautifulSoup(r2.text, "html.parser")
    text = " ".join(s2.get_text(" ", strip=True).split())[:1500]
    still_form = s2.find("form", {"name": "schedule_register_form"}) is not None
    try:
        fresh = {i["id"]: i for i in fetch_club(s, it["club"])}
        new = state_of(fresh.get(it["id"], it))
    except Exception:
        new = "unknown"
    if quiet and new == "early" and not ("wypisz" in text.lower()):
        return False, None, text  # форму приняли, но запись ещё не открыта: пробуем дальше
    ok = new == "booked" or (not still_form and new not in ("open", "early"))
    return ok, ("подтверждено" if new == "booked" else text[:200]), text


def book(s: requests.Session, it: dict, quiet: bool = False) -> tuple[bool, str, str]:
    """Возвращает (успех, сообщение, текст ответа сервера для отладки)."""
    data = get_form(s, it)
    if data is None:
        if quiet:  # пробная попытка в момент открытия: формы ещё нет, это не ошибка
            return False, None, ""
        return False, "форма записи не найдена (возможно, уже записаны)", ""
    return post_form(s, it, data, quiet)


def tick() -> list[str]:
    with LOCK:
        return _tick_locked()


def _tick_locked() -> list[str]:
    msgs: list[str] = []
    st = store.load()
    if st["paused"] or not (st["watches"] or st["targets"]) or not st["cookies"]:
        return msgs
    if time.time() - st.get("last_tick", 0) < 20:  # защита от двойных пингов
        return msgs
    store.update(lambda x: x.update(last_tick=time.time()))

    s = make_session(st["cookies"])
    started = time.time()
    booked_ids = {b["id"] for b in st["booked"]}
    target_ids = {t["id"] for t in st["targets"]}
    all_clubs = sorted({w["club"] for w in st["watches"]} | {t["club"] for t in st["targets"]})
    new_booked: list[dict] = []
    observed = None
    last_resp = None
    next_hot = 0.0
    seen_early: set[str] = set()
    alerts: dict[str, str] = {}
    failed = False
    clubs = all_clubs
    rec_lock = threading.Lock()

    def record_ok(it: dict) -> bool:
        """Фиксирует успешную запись (потокобезопасно). False, если уже зафиксирована другим потоком."""
        with rec_lock:
            if it["id"] in booked_ids:
                return False
            booked_ids.add(it["id"])
            new_booked.append({"id": it["id"], "label": label(it), "start": it["start"].isoformat(), "club": it["club"], "dur": it.get("dur")})
            msgs.append(f"✅ Записан\n{label(it)}")
            tgt = next((t for t in st["targets"] if t["id"] == it["id"] and t.get("message_id")), None)
            if tgt:
                CARD_UPDATES.append({"chat_id": tgt["chat_id"], "message_id": tgt["message_id"],
                                     "html": tgt.get("card_html", "")})
            return True

    def attempt(it: dict, quiet: bool = False, final: bool = True):
        """True — записали, False — отказ сайта, None — формы ещё нет (только quiet)."""
        nonlocal last_resp
        ok, msg, text = book(s, it, quiet=quiet)
        if msg is None:
            return None
        last_resp = text or last_resp
        if ok:
            record_ok(it)
        elif final:
            msgs.append(f"⚠️ Не удалось записаться\n{label(it)}\n{msg}")
        return ok

    warmed: set[str] = set()

    def warm(it: dict) -> None:
        """Прогрев: один лёгкий запрос к странице формы, чтобы соединение и токен защиты были готовы."""
        if it["id"] in warmed:
            return
        warmed.add(it["id"])
        try:
            s.get(BASE + it["url"], headers={"X-Requested-With": "XMLHttpRequest"}, timeout=10)
        except Exception:  # noqa: BLE001
            pass

    def fast_phase(due: list) -> None:
        """Быстрая фаза: несколько параллельных потоков опрашивают страницу формы и отправляют запись,
        как только форма появилась. Если не сработает, ниже продолжается обычный опрос расписания."""
        nonlocal last_resp
        t_end = time.time() + FAST_SECONDS
        cookies = {**st["cookies"], **s.cookies.get_dict()}
        state = {it["id"]: {"done": threading.Event(), "posts": 0, "lock": threading.Lock()} for it in due}
        stop = threading.Event()
        auth_err: list = []
        sessions: list = []

        def worker(it: dict, idx: int) -> None:
            nonlocal last_resp
            stt_ = state[it["id"]]
            ws = make_session(cookies)
            sessions.append(ws)
            time.sleep(idx * FAST_EVERY / max(1, FAST_PARALLEL))  # потоки стартуют вразнобой
            while time.time() < t_end and not stop.is_set() and not stt_["done"].is_set() and not auth_err:
                try:
                    data = get_form(ws, it)
                except AuthError as e:
                    auth_err.append(e)
                    return
                except Exception:  # noqa: BLE001 — сеть: пробуем дальше
                    data = None
                if data is not None:
                    with stt_["lock"]:
                        if stt_["done"].is_set() or stt_["posts"] >= FAST_POSTS or stop.is_set():
                            return
                        try:
                            ok, msg, text = post_form(ws, it, data, quiet=True)
                        except AuthError as e:
                            auth_err.append(e)
                            return
                        except Exception:  # noqa: BLE001
                            ok, msg, text = False, None, ""
                        if text:
                            last_resp = text
                        if ok:
                            record_ok(it)
                            stt_["done"].set()
                            return
                        if msg is not None:  # сайт принял форму и отказал (мест нет и т.п.)
                            stt_["posts"] += 1
                time.sleep(FAST_EVERY)

        threads = [threading.Thread(target=worker, args=(it, k), daemon=True)
                   for it in due for k in range(max(1, FAST_PARALLEL))]
        for th in threads:
            th.start()
        for th in threads:
            th.join(max(0.1, t_end + 8 - time.time()))
        stop.set()
        for ws in sessions:  # обновлённые cookie сессии возвращаем в основную сессию
            for k, v in ws.cookies.get_dict().items():
                s.cookies.set(k, v)
        if auth_err:
            raise auth_err[0]
        for it in due:
            stt_ = state[it["id"]]
            if not stt_["done"].is_set() and stt_["posts"] >= FAST_POSTS:
                msgs.append(f"⚠️ Не удалось записаться\n{label(it)}\n{(last_resp or '')[:200]}")

    try:
        while True:
            now = now_local()
            horizon = now + dt.timedelta(days=HORIZON_DAYS)
            items = []
            for club in clubs:
                items += fetch_club(s, club)
            hot_clubs: set[str] = set()
            wait_hot = None
            fast_due: list = []
            for it in items:
                if not (now < it["start"] <= horizon) or it["id"] in booked_ids:
                    continue
                if not (any(matches(it, w) for w in st["watches"]) or it["id"] in target_ids):
                    continue
                stt = state_of(it)
                if stt == "early":
                    seen_early.add(it["id"])
                    secs = (it["start"] - dt.timedelta(hours=OPEN_BEFORE_HOURS) - now).total_seconds()
                    if -HOT_TAIL <= secs <= HOT_LEAD:
                        hot_clubs.add(it["club"])
                        waf.prewarm("zdrofit.pl")  # свежий токен защиты заранее, чтобы не ждать браузер в момент открытия
                        if secs <= WARM_LEAD:
                            warm(it)
                        if -FAST_AFTER <= secs <= FAST_LEAD and not it["full"] and it["id"] not in booked_ids:
                            fast_due.append(it)  # быстрая фаза: не ждём, пока расписание «переключится»
                    elif secs > HOT_LEAD:
                        w = secs - HOT_LEAD
                        wait_hot = w if wait_hot is None else min(wait_hot, w)
                    continue
                if stt != "open" or it["full"]:
                    if it["id"] in target_ids:  # занятие из очереди: сообщаем, почему не записали (один раз на причину)
                        if stt == "open" and it["full"]:
                            reason = "Запись открылась, но мест уже нет (скорее всего, заняли в первые секунды). Продолжу следить: если кто-то отпишется, запишу."
                        elif stt == "closed":
                            reason = "Запись уже закрыта."
                        elif stt == "unknown":
                            reason = f"Сайт показывает статус «{it['status']}», бот не умеет с ним работать."
                        else:
                            reason = None
                        tg = next((t for t in st["targets"] if t["id"] == it["id"]), None)
                        if reason and tg is not None and tg.get("alerted") != reason and it["id"] not in alerts:
                            alerts[it["id"]] = reason
                            msgs.append(f"⚠️ Не записал\n{label(it)}\n{reason}")
                    continue
                if it["id"] in seen_early and observed is None:  # поймали момент открытия
                    observed = {"label": label(it), "hours_before": round((it["start"] - now).total_seconds() / 3600, 2),
                                "at": now.isoformat(timespec="seconds")}
                try:
                    attempt(it)
                except AuthError:
                    raise
                except Exception:  # noqa: BLE001 — сбой сети в критический момент: не обрываем, повторим в этом же прогоне
                    hot_clubs.add(it["club"])
            if fast_due:
                fast_phase(fast_due)  # если не сработает, ниже цикл продолжится по старой схеме (опрос расписания)
            if wait_hot is not None:
                next_hot = time.time() + wait_hot
            if hot_clubs and time.time() - started < BURST_SECONDS:
                clubs = sorted(hot_clubs)
                time.sleep(BURST_EVERY)
                continue
            break
    except AuthError:
        today = now_local().strftime("%Y-%m-%d")

        def mark(x):
            if x["auth_alert"] != today:
                x["auth_alert"] = today
                return True
            return False

        if store.update(mark):
            msgs.append("🔑 Сессия истекла. Войдите на zdrofit.pl в браузере и пришлите новый cookie командой /cookie")
        return msgs
    except Exception as e:  # сайт/сеть: не падаем; пишем только если сбой длится дольше DOWN_ALERT_SECONDS
        failed = True

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
        if mins is not None:
            msgs.append(f"⚠️ Сайт zdrofit.pl не отвечает уже {mins} мин. Последняя ошибка: {e!r}"[:400]
                        + "\nБот продолжает пробовать, очередь автозаписи сохранена.")

    if not failed:
        def mark_ok(x):
            was = bool(x.get("down_alerted"))
            x["err_since"] = 0
            x["down_alerted"] = False
            return was

        if store.update(mark_ok):
            msgs.append("✅ Сайт zdrofit.pl снова отвечает, проверка расписания идёт в обычном режиме.")

    now = now_local()

    def save_all(x):
        x["cookies"] = {**x["cookies"], **s.cookies.get_dict()}
        x["booked"] = [b for b in x["booked"] + new_booked if b["start"] > (now - dt.timedelta(days=30)).isoformat()]  # хвост 30 дней для календаря
        x["targets"] = [t for t in x["targets"] if t["id"] not in booked_ids and t["start"] > now.isoformat()]
        for t in x["targets"]:
            if t["id"] in alerts:
                t["alerted"] = alerts[t["id"]]
        x["next_hot"] = next_hot
        if observed:
            x["open_observed"] = observed
        if last_resp:
            x["last_response"] = last_resp
    store.update(save_all)
    return msgs

def book_now(club: str, item_id: str) -> str:
    """Кнопка «Записаться»: записывает сразу, либо ставит занятие в очередь автозаписи."""
    with LOCK:
        st = store.load()
        if not st["cookies"]:
            return "Нет сессии. Пришлите cookie командой /cookie (подсказка: /booking)."
        s = make_session(st["cookies"])
        try:
            items = fetch_club(s, club)
        except AuthError:
            return "🔑 Сессия истекла. Пришлите новый cookie командой /cookie"
        except Exception as e:
            return f"⚠️ Не удалось загрузить расписание: {e!r}"
        it = next((i for i in items if i["id"] == item_id), None)
        if not it:
            return "Это занятие уже не найдено в расписании."
        lab = label(it)
        stt = state_of(it)
        if stt == "booked" or item_id in {b["id"] for b in st["booked"]}:
            return f"Вы уже записаны\n{lab}"
        if stt == "closed" or it["start"] <= now_local():
            return f"⛔ Запись закрыта\n{lab}"
        if stt == "open" and not it["full"]:
            try:
                ok, msg, text = book(s, it)
            except AuthError:
                return "🔑 Сессия истекла. Пришлите новый cookie командой /cookie"
            except Exception as e:
                return f"⚠️ Ошибка записи: {e!r}"

            def save(x):
                x["cookies"] = {**x["cookies"], **s.cookies.get_dict()}
                if text:
                    x["last_response"] = text
                if ok:
                    x["booked"].append({"id": it["id"], "label": lab, "start": it["start"].isoformat(), "club": it["club"], "dur": it.get("dur")})
                    x["targets"] = [t for t in x["targets"] if t["id"] != it["id"]]
            store.update(save)
            return f"✅ Записан\n{lab}" if ok else f"⚠️ Не удалось записаться\n{lab}\n{msg}"
        if stt in ("open", "early"):
            def queue(x):
                if not any(t["id"] == it["id"] for t in x["targets"]):
                    x["targets"].append({"id": it["id"], "club": club, "label": lab, "start": it["start"].isoformat()})
            store.update(queue)
            why = "мест нет" if (stt == "open" and it["full"]) else "запись ещё не открыта (обычно открывается за ~48 ч до начала)"
            return f"⏳ {lab}\n{why}. Поставил на автозапись: запишу, как только станет возможно."
        return f"Статус занятия «{it['status']}», записаться пока нельзя\n{lab}"
