"""Клиент PerfectGym ClientPortal2 (API портала zdrofit.perfectgym.pl).

Расписание и детали занятий отдаются без входа и без защиты сайта. Для записи нужен вход
по e-mail и паролю (PG_LOGIN / PG_PASSWORD): сервер выдаёт JWT в заголовке ответа `jwt-token`.
Cookie не используются: каждый запрос идёт отдельным вызовом requests без cookie-jar.
Пароль и токен нигде не логируются и не сохраняются в базе.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import threading
import time
import unicodedata
from zoneinfo import ZoneInfo

import requests

BASE = os.getenv("PG_BASE", "https://zdrofit.perfectgym.pl/ClientPortal2").strip().rstrip("/")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Warsaw"))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
RELOGIN_BEFORE = 600       # обновляем токен, если до его истечения меньше N секунд
DEFAULT_TTL = 900          # если в JWT нет exp
CLUBS_TTL = 6 * 3600

_lock = threading.RLock()
_tok = {"token": None, "exp": 0.0, "uid": None, "src": ""}
_clubs_cache = {"t": 0.0, "data": []}


class PGError(Exception):
    pass


class PGAuthError(PGError):
    """Вход не удался (неверные данные, нужна двухфакторка и т.п.)."""


def login_name() -> str:
    return os.getenv("PG_LOGIN", "").strip()


def enabled() -> bool:
    return bool(login_name() and os.getenv("PG_PASSWORD", ""))


def mask_login() -> str:
    s = login_name()
    if "@" in s:
        a, b = s.split("@", 1)
        return (a[:2] + "***@" + b) if a else s
    return s[:2] + "***"


def _headers(token: str | None = None) -> dict:
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
         "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest",
         "Origin": BASE.split("/ClientPortal2")[0], "Referer": BASE + "/"}
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def _call(method: str, path: str, payload=None, token: str | None = None, timeout: float = 15):
    """Один HTTP-вызов без cookie. Возвращает (status, json|None, headers, text)."""
    r = requests.request(method, f"{BASE}/{path}", headers=_headers(token), timeout=timeout,
                         data=None if payload is None else json.dumps(payload))
    try:
        data = r.json()
    except ValueError:
        data = None
    return r.status_code, data, r.headers, r.text


# ---------------------------------------------------------------- вход

def _jwt_claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:  # noqa: BLE001
        return {}


def _find_uid(obj, depth: int = 0):
    """Ищет идентификатор пользователя в ответе входа (формат точно неизвестен, ищем по типичным ключам)."""
    if depth > 3 or obj is None:
        return None
    if isinstance(obj, dict):
        low = {str(k).lower(): v for k, v in obj.items()}
        for k in ("userid", "memberid", "id"):
            v = low.get(k)
            if isinstance(v, (int, str)) and str(v).isdigit():
                return int(v)
        for k in ("user", "member", "identity", "data", "result"):
            if k in low:
                r = _find_uid(low[k], depth + 1)
                if r is not None:
                    return r
    return None


def _claims_uid(claims: dict):
    for k, v in claims.items():
        if str(k).lower().rsplit("/", 1)[-1] in ("userid", "memberid", "id", "nameidentifier", "sub") and str(v).isdigit():
            return int(v)
    return None


def login(force: bool = False) -> None:
    """Входит (или обновляет токен). Бросает PGAuthError при отказе."""
    with _lock:
        if not force and _tok["token"] and _tok["exp"] - time.time() > RELOGIN_BEFORE:
            return
        user, pwd = login_name(), os.getenv("PG_PASSWORD", "")
        if not (user and pwd):
            raise PGAuthError("не заданы PG_LOGIN / PG_PASSWORD")
        st, data, hdr, text = _call("POST", "Auth/Login", {"RememberMe": False, "Login": user, "Password": pwd})
        token = hdr.get("jwt-token")
        if not token:
            msg = ""
            if isinstance(data, dict):
                errs = data.get("Errors") or []
                msg = "; ".join(str(e.get("Message", e)) if isinstance(e, dict) else str(e) for e in errs)
                if data.get("TwoFactorRequired") or data.get("RequiresTwoFactor"):
                    msg = "включена двухфакторная проверка, бот её не поддерживает"
                msg = msg or str(data.get("Message") or data.get("message") or "")
            raise PGAuthError(f"HTTP {st}: {msg or 'вход отклонён'}"[:200])
        claims = _jwt_claims(token)
        uid = _find_uid(data) or _claims_uid(claims)
        src = "ответ входа" if _find_uid(data) else ("JWT" if uid else "")
        if uid is None:  # последняя попытка: «кто я»
            for meth in ("GET", "POST"):
                try:
                    st2, d2, _, _ = _call(meth, "Auth/Login/Identity", None if meth == "GET" else {}, token=token)
                    uid = _find_uid(d2)
                    if uid:
                        src = "Identity"
                        break
                except Exception:  # noqa: BLE001
                    pass
        if uid is None:
            raise PGAuthError("вход выполнен, но не удалось определить номер пользователя")
        _tok.update(token=token, uid=uid, src=src,
                    exp=float(claims.get("exp") or (time.time() + DEFAULT_TTL)))


def token_info() -> dict:
    return {"has_token": bool(_tok["token"]), "expires_in": int(_tok["exp"] - time.time()) if _tok["token"] else None,
            "uid_src": _tok["src"]}


def _soft_token():
    """Токен, если он есть и ещё действует (для чтения расписания вход не обязателен)."""
    return _tok["token"] if _tok["token"] and _tok["exp"] - time.time() > 30 else None


def _authed(method: str, path: str, payload=None, timeout: float = 15):
    login()
    st, data, hdr, text = _call(method, path, payload, token=_tok["token"], timeout=timeout)
    if st == 401:  # токен отозван: входим заново и повторяем один раз
        login(force=True)
        st, data, hdr, text = _call(method, path, payload, token=_tok["token"], timeout=timeout)
    return st, data, hdr, text


def _soft(method: str, path: str, payload=None, timeout: float = 15):
    tok = _soft_token()
    st, data, hdr, text = _call(method, path, payload, token=tok, timeout=timeout)
    if st == 401 and tok:  # просроченный токен мешает чтению — читаем анонимно
        st, data, hdr, text = _call(method, path, payload, timeout=timeout)
    return st, data, hdr, text


# ---------------------------------------------------------------- клубы

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower().replace("ł", "l"))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", s)


def clubs() -> list[dict]:
    with _lock:
        if _clubs_cache["data"] and time.time() - _clubs_cache["t"] < CLUBS_TTL:
            return _clubs_cache["data"]
    st, data, _, _ = _call("POST", "Clubs/GetClubs", {})
    if st != 200 or not isinstance(data, list):
        raise PGError(f"список клубов: HTTP {st}")
    with _lock:
        _clubs_cache.update(t=time.time(), data=data)
    return data


def _overrides() -> dict:
    out = {}
    for part in os.getenv("PG_CLUB_IDS", "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if v.strip().isdigit():
                out[k.strip()] = int(v.strip())
    return out


def club_candidates(slug: str) -> list[dict]:
    key = norm(slug.removeprefix("krakow-"))
    if not key:
        return []
    res = []
    for c in clubs():
        full = norm(c.get("FullName") or c.get("Name") or "")
        if "krakow" in full and key in full:
            res.append(c)
    return res


def club_id(slug: str):
    """Номер клуба в PerfectGym по slug клуба на сайте; None, если не найден или неоднозначно."""
    ov = _overrides()
    if slug in ov:
        return ov[slug]
    found = club_candidates(slug)
    return found[0]["Id"] if len(found) == 1 else None


# ---------------------------------------------------------------- расписание

def _flatten(data: dict, club_id_: int) -> list[dict]:
    out = []
    for zone in (data or {}).get("CalendarData") or []:
        for hour in zone.get("ClassesPerHour") or []:
            for day in hour.get("ClassesPerDay") or []:
                for c in day or []:
                    c = dict(c)
                    c["ClubId"] = club_id_
                    c["Zone"] = zone.get("ZoneName")
                    out.append(c)
    return out


def weekly(club_id_: int, date: dt.date):
    """Неделя расписания, начиная с date. Возвращает (занятия, дата следующей недели, можно ли вперёд)."""
    st, data, _, text = _soft("POST", "Classes/ClassCalendar/WeeklyClasses",
                              {"clubId": club_id_, "date": date.isoformat(), "daysInWeek": 7})
    if st != 200 or not isinstance(data, dict) or "CalendarData" not in data:
        raise PGError(f"расписание клуба {club_id_}: HTTP {st} {text[:100]}")
    pg = data.get("PagerData") or {}
    return _flatten(data, club_id_), pg.get("NextDate"), bool(pg.get("CanGoForward"))


def horizon_classes(club_id_: int, days: int, today: dt.date | None = None) -> list[dict]:
    """Все занятия клуба на ближайшие days дней (несколько недельных запросов)."""
    day = today or dt.datetime.now(TZ).date()
    end = day + dt.timedelta(days=days)
    out, seen = [], set()
    for _ in range(6):
        items, nxt, fwd = weekly(club_id_, day)
        for c in items:
            if c["Id"] not in seen:
                seen.add(c["Id"])
                out.append(c)
        if not fwd or not nxt:
            break
        day = dt.date.fromisoformat(nxt[:10])
        if day > end:
            break
    return out


def details(class_id: int) -> dict:
    st, data, _, text = _soft("POST", "Classes/ClassCalendar/Details", {"classId": class_id})
    if st != 200 or not isinstance(data, dict):
        raise PGError(f"детали занятия {class_id}: HTTP {st} {text[:100]}")
    return data


def state_of(status: str, reason) -> str:
    s, r = (status or ""), (reason or "").lower()
    if s == "Bookable":
        return "open"
    if s == "FullBooked":
        return "full"
    if s == "Awaitable":
        return "waitlist"
    if s == "Unavailable":
        if "soon" in r:
            return "early"
        if "late" in r or "ended" in r:
            return "closed"
        return "unknown"
    if s.lower().startswith(("book", "regist", "attend", "reserv")):
        return "booked"
    return "unknown"


def parse_dur(s) -> int | None:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", s or "")
    if not m or not (m.group(1) or m.group(2)):
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def to_item(club_slug: str, c: dict) -> dict:
    """Приводит занятие PerfectGym к виду, близкому к элементу расписания сайта."""
    return {
        "id": f"pg{c['Id']}", "pg_id": c["Id"], "club": club_slug, "pg_club": c.get("ClubId"),
        "name": c.get("Name") or "", "activity": norm(c.get("Name") or ""),
        "trainer": c.get("Trainer") or "",
        "start": dt.datetime.fromisoformat(c["StartTime"]),  # локальное время без пояса, как на сайте
        "dur": parse_dur(c.get("Duration")),
        "status": c.get("Status") or "", "reason": c.get("StatusReason"),
        "state": state_of(c.get("Status"), c.get("StatusReason")),
        "full": c.get("Status") in ("FullBooked", "Awaitable"),
        "available": (c.get("BookingIndicator") or {}).get("Available"),
    }


def find_class(club_slug: str, start: dt.datetime, name: str):
    """Находит занятие PerfectGym по клубу, времени начала и названию (id на сайте и в API разные)."""
    cid = club_id(club_slug)
    if not cid:
        return None
    day = start.astimezone(TZ).date() if start.tzinfo else start.date()
    items, _, _ = weekly(cid, day)
    want = start.astimezone(TZ).replace(tzinfo=None) if start.tzinfo else start
    nm = norm(name)
    cand = []
    for c in items:
        if dt.datetime.fromisoformat(c["StartTime"]) != want.replace(second=0, microsecond=0):
            continue
        cn = norm(c.get("Name") or "")
        if cn == nm:
            return to_item(club_slug, c)
        if nm and (nm in cn or cn in nm):
            cand.append(c)
    return to_item(club_slug, cand[0]) if len(cand) == 1 else None


def same_class(t: dict, it: dict) -> bool:
    """Подходит ли занятие из очереди (клуб, время, название из подписи) под занятие PerfectGym."""
    if t.get("pg_id"):
        return t["pg_id"] == it["pg_id"]
    if t["club"] != it["club"]:
        return False
    try:
        tstart = dt.datetime.fromisoformat(t["start"])
    except Exception:  # noqa: BLE001
        return False
    if abs((tstart - it["start"]).total_seconds()) > 60:
        return False
    lines = (t.get("label") or "").split("\n")
    third = lines[2] if len(lines) >= 3 else (lines[-1] if lines else "")
    nm = norm(third.split(" - ")[0])
    return bool(nm) and (nm == it["activity"] or nm in it["activity"] or it["activity"] in nm)


# ---------------------------------------------------------------- запись

def _err_text(st: int, data, text: str) -> str:
    if isinstance(data, dict):
        errs = data.get("Errors") or []
        msgs = [str(e.get("Message", e)) if isinstance(e, dict) else str(e) for e in errs]
        if msgs:
            return "; ".join(msgs)
        for k in ("Message", "message", "ErrorMessage"):
            if data.get(k):
                return str(data[k])
    return f"HTTP {st}: {(text or '')[:150]}"


def book(class_id: int, club_id_: int):
    """BookClass. Возвращает (ok, сообщение, сырой ответ)."""
    st, data, _, text = _authed("POST", "Classes/ClassCalendar/BookClass",
                                {"classId": class_id, "userId": _tok["uid"], "seatNumber": None, "clubId": club_id_})
    raw = f"PG BookClass {class_id}: HTTP {st} {(text or '')[:300]}"
    if st == 200 and isinstance(data, dict):
        if data.get("Errors"):
            return False, _err_text(st, data, text), raw
        if data.get("NeedProduct"):
            return False, "нужен абонемент или продукт (проверьте членство)", raw
        return True, "", raw
    if st == 200 and data is None and not (text or "").strip():
        return True, "", raw
    return False, _err_text(st, data, text), raw


def cancel(class_id: int):
    st, data, _, text = _authed("POST", "Classes/ClassCalendar/CancelBooking",
                                {"classId": class_id, "userId": _tok["uid"]})
    raw = f"PG CancelBooking {class_id}: HTTP {st} {(text or '')[:300]}"
    if st == 200 and not (isinstance(data, dict) and data.get("Errors")):
        return True, "", raw
    return False, _err_text(st, data, text), raw
