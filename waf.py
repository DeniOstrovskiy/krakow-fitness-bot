"""Обход JS-проверки AWS WAF (HTTP 202 + x-amzn-waf-action: challenge) через headless-Chromium.

Сайт отдаёт адресам дата-центров (Render и т. п.) не страницу, а JS-проверку. Настоящий браузер её проходит
и получает cookie aws-waf-token; дальше обычные запросы requests с этим cookie работают, пока токен жив.
Браузер запускается только когда сайт ответил проверкой (дома, где проверки нет, он не нужен).
Отключить: WAF_BROWSER=0.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from urllib.parse import urlparse

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
ENABLED = os.getenv("WAF_BROWSER", "1") != "0"
MIN_REFRESH_GAP = 20  # не запускать браузер чаще, чем раз в 20 с на хост
_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}   # host -> {"t": время, "cookies": {name: value}}
info = "браузер ещё не запускался"


def challenged(r: requests.Response) -> bool:
    return r.status_code == 202 and "x-amzn-waf-action" in {k.lower() for k in r.headers}


def _launch(host: str) -> dict:
    """Открывает https://host/ в Chromium, ждёт cookie aws-waf-token, возвращает cookie."""
    global info
    from playwright.sync_api import sync_playwright  # импорт лениво: локально Playwright не нужен

    t0 = time.time()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions",
            "--disable-background-networking", "--mute-audio"])
        try:
            ctx = browser.new_context(user_agent=UA, locale="pl-PL", viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            # картинки, шрифты и стили не нужны: экономим память и время
            page.route("**/*", lambda route: route.abort()
                       if route.request.resource_type in ("image", "media", "font", "stylesheet")
                       else route.continue_())
            page.goto(f"https://{host}/", wait_until="domcontentloaded", timeout=30000)
            got: dict[str, str] = {}
            for _ in range(40):  # до ~20 с
                got = {c["name"]: c["value"] for c in ctx.cookies() if c["name"].lower().startswith("aws-waf")}
                if got:
                    break
                page.wait_for_timeout(500)
            if got:
                page.wait_for_timeout(500)
                got = {c["name"]: c["value"] for c in ctx.cookies() if c["name"].lower().startswith("aws-waf")}
            info = (f"{host}: {'токен получен' if got else 'токен НЕ получен'} за {time.time() - t0:.1f} с; "
                    f"cookies: {','.join(sorted(got)) or '-'}; title: {(page.title() or '-')[:60]}")
            return got
        finally:
            browser.close()


def refresh(host: str, force: bool = False) -> dict:
    global info
    if not ENABLED:
        return {}
    with _LOCK:
        cur = _CACHE.get(host)
        if cur and not force and time.time() - cur["t"] < MIN_REFRESH_GAP:
            return cur["cookies"]
        if cur and force and time.time() - cur["t"] < MIN_REFRESH_GAP:
            return cur["cookies"]  # только что обновили (другим потоком), не гоняем браузер зря
        try:
            cookies = _launch(host)
        except Exception as exc:  # noqa: BLE001
            info = f"{host}: ошибка браузера: {type(exc).__name__}: {str(exc)[:200]}"
            logging.exception("WAF browser failed")
            cookies = {}
        _CACHE[host] = {"t": time.time(), "cookies": cookies}
        return cookies


class WafSession(requests.Session):
    """requests.Session, который сам проходит проверку WAF и повторяет запрос."""

    def __init__(self):
        super().__init__()
        self.headers.update({"User-Agent": UA, "Accept-Language": "pl,en;q=0.8"})

    def _apply(self, host: str) -> None:
        cur = _CACHE.get(host)
        if cur:
            for k, v in cur["cookies"].items():
                self.cookies.set(k, v, domain=host)

    def request(self, method, url, **kw):  # type: ignore[override]
        host = urlparse(url).hostname or ""
        self._apply(host)
        r = super().request(method, url, **kw)
        if challenged(r) and refresh(host, force=True):
            self._apply(host)
            r = super().request(method, url, **kw)
        return r


def new_session() -> WafSession:
    return WafSession()
