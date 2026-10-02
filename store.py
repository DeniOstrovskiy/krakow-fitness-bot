"""Хранилище состояния: Upstash Redis (REST) если заданы переменные, иначе локальный data.json."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

import requests

KEY = "zdrofit:state"
FILE = Path(__file__).with_name("data.json")
DEFAULT = {
    "watches": [],      # [{club, activity, trainer, days, time_from, time_to}]
    "booked": [],       # [{id, label, start}]
    "targets": [],      # [{id, club, label, start}] занятия, ждущие открытия записи
    "paused": False,
    "cookies": {},
    "auth_alert": "",
    "last_tick": 0,
    "last_response": "",
    "next_hot": 0,        # когда ближайшее расчётное открытие записи (epoch)
    "open_observed": {},  # последнее наблюдённое открытие: за сколько часов до начала
    "err_alert": 0,
}
_lock = threading.RLock()


def _redis():
    url = os.getenv("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
    return (url, {"Authorization": f"Bearer {token}"}) if url and token else (None, None)


def load() -> dict:
    url, headers = _redis()
    raw = None
    if url:
        r = requests.post(url, json=["GET", KEY], headers=headers, timeout=10)
        r.raise_for_status()
        raw = r.json().get("result")
    elif FILE.exists():
        raw = FILE.read_text()
    state = copy.deepcopy(DEFAULT)
    if raw:
        state.update(json.loads(raw))
    return state


def save(state: dict) -> None:
    raw = json.dumps(state, ensure_ascii=False)
    url, headers = _redis()
    if url:
        requests.post(url, json=["SET", KEY, raw], headers=headers, timeout=10).raise_for_status()
    else:
        FILE.write_text(raw)


def update(fn):
    """load -> fn(state) -> save. Возвращает результат fn."""
    with _lock:
        state = load()
        result = fn(state)
        save(state)
        return result
