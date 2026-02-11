from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from config import load_config
from schedule import _click_first, _maybe_accept_cookies


API_HINT_RE = re.compile(
    r"(api|graphql|schedule|timetable|grafik|lesson|class|event|calendar|plan)",
    re.I,
)

_BODY_PREVIEW_LIMIT = 1200
_PAGE_LOAD_WAIT_MS = 14_000


@dataclass
class LoggedResponse:
    url: str
    status: int
    content_type: str
    body_preview: str
    resource_type: str
    method: str
    request_post: str | None


def _should_log(url: str, content_type: str, resource_type: str) -> bool:
    if resource_type in {"xhr", "fetch"}:
        return True
    if "application/json" in content_type:
        return True
    if "text/json" in content_type:
        return True
    if API_HINT_RE.search(url):
        return True
    return False


def _sanitize(text: str, limit: int = _BODY_PREVIEW_LIMIT) -> str:
    text = text.strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


async def main() -> None:
    cfg = load_config()
    out_dir = Path("api_discovery")
    out_dir.mkdir(exist_ok=True)
    log_path = Path("api_discovery.log")

    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            "Playwright не установлен. Запусти: python3 -m pip install playwright && "
            "python3 -m playwright install"
        ) from exc

    responses: list[LoggedResponse] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=cfg.user_agent, locale="pl-PL")

        for club in cfg.clubs:
            page = await context.new_page()

            slug = club.url.rstrip("/").split("/")[-2]

            async def handle_response(resp):
                try:
                    url = resp.url
                    ct = resp.headers.get("content-type", "")
                    resource_type = resp.request.resource_type
                    if not _should_log(url, ct, resource_type):
                        return
                    body = await resp.text()
                    preview = _sanitize(body)
                    method = resp.request.method
                    post_data = None
                    try:
                        if method.upper() in {"POST", "PUT", "PATCH"}:
                            post_data = resp.request.post_data or None
                    except Exception:
                        post_data = None
                    responses.append(
                        LoggedResponse(
                            url=url,
                            status=resp.status,
                            content_type=ct,
                            body_preview=preview,
                            resource_type=resource_type,
                            method=method,
                            request_post=post_data,
                        )
                    )
                except Exception:
                    return

            page.on("response", lambda resp: asyncio.create_task(handle_response(resp)))

            await page.goto(club.url, wait_until="domcontentloaded", timeout=60_000)
            await _maybe_accept_cookies(page)
            await page.wait_for_timeout(_PAGE_LOAD_WAIT_MS)

            html = await page.content()
            (out_dir / f"{slug}.html").write_text(html, encoding="utf-8")
            try:
                await page.screenshot(path=str(out_dir / f"{slug}.png"), full_page=True)
            except Exception:
                pass

            await page.close()

        await browser.close()

    unique = {}
    for resp in responses:
        unique[resp.url] = resp

    lines = []
    for url, resp in unique.items():
        lines.append(f"URL: {resp.url}")
        lines.append(f"Status: {resp.status}")
        lines.append(f"Type: {resp.resource_type} | Method: {resp.method}")
        lines.append(f"Content-Type: {resp.content_type}")
        if resp.request_post:
            lines.append(f"Request-Body: {resp.request_post}")
        lines.append(f"Body: {resp.body_preview}")
        lines.append("")

    log_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved {len(unique)} responses to {log_path}")


if __name__ == "__main__":
    asyncio.run(main())
