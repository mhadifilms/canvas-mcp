"""Interactive login: open a real browser, let the student sign in normally, keep the cookie.

This is the fallback that works everywhere, including schools that route Canvas
through Shibboleth/Okta/Duo and browsers whose cookie stores we cannot decrypt.
It needs Playwright, which is an optional extra.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from . import config
from .browser_cookies import SESSION_COOKIE_NAMES, USEFUL_COOKIE_NAMES, host_matches
from .config import StoredSession, normalize_base_url
from .errors import CanvasMCPError

PLAYWRIGHT_MISSING = (
    "Interactive login needs Playwright. Install it with:\n"
    "    pip install 'canvas-mcp[login]'\n"
    "    python -m playwright install chromium"
)


def interactive_login(
    base_url: str,
    *,
    timeout_seconds: int = 300,
    on_message=print,
) -> StoredSession:
    """Open a browser window at Canvas and wait until the session is real."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise CanvasMCPError(PLAYWRIGHT_MISSING) from exc

    base_url = normalize_base_url(base_url)
    if not base_url:
        raise CanvasMCPError("I need your Canvas address, e.g. https://yourschool.instructure.com")

    deadline = time.time() + timeout_seconds

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=False)
        except Exception as exc:  # pragma: no cover - depends on local install
            raise CanvasMCPError(
                f"Could not start a browser ({exc}).\nTry: python -m playwright install chromium"
            ) from exc

        context = browser.new_context(viewport={"width": 1180, "height": 860})
        page = context.new_page()
        on_message(f"Opening {base_url} - log in the way you normally would.")
        on_message("This window closes by itself once Canvas lets you in.")
        try:
            page.goto(base_url, wait_until="domcontentloaded")
        except Exception as exc:
            context.close()
            browser.close()
            raise CanvasMCPError(f"Could not load {base_url}: {exc}") from exc

        session: StoredSession | None = None
        while time.time() < deadline:
            if page.is_closed() and not context.pages:
                break
            host = _current_canvas_host(page, base_url)
            cookies = _canvas_cookies(context.cookies(), host)
            if any(name in cookies for name in SESSION_COOKIE_NAMES):
                profile = _verify(context, f"https://{host}")
                if profile:
                    session = StoredSession(
                        base_url=f"https://{host}",
                        cookies=cookies,
                        source="browser login",
                        saved_at=time.time(),
                        user={
                            k: profile[k]
                            for k in ("id", "name", "short_name", "login_id", "time_zone")
                            if k in profile
                        },
                    )
                    break
            page.wait_for_timeout(1500)

        context.close()
        browser.close()

    if session is None:
        raise CanvasMCPError(
            "Login timed out before Canvas handed over a session. "
            "Run `canvas-mcp login` again and finish signing in (including any 2-factor prompt)."
        )

    config.save_session(session)
    on_message(f"Signed in as {session.user.get('name', 'you')} on {session.base_url}.")
    return session


def _current_canvas_host(page: Any, base_url: str) -> str:
    """Follow the student if the school bounced them to a different Canvas host."""
    original = urlparse(base_url).hostname or ""
    try:
        current = urlparse(page.url).hostname or ""
    except Exception:
        return original
    if current and ("instructure.com" in current or "canvas" in current):
        return current
    return original


def _canvas_cookies(all_cookies: list[dict[str, Any]], host: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for cookie in all_cookies:
        domain = str(cookie.get("domain", ""))
        if not host_matches(domain, host):
            continue
        name = str(cookie.get("name", ""))
        if name in USEFUL_COOKIE_NAMES or name.startswith("_"):
            cookies[name] = str(cookie.get("value", ""))
    return cookies


def _verify(context: Any, base_url: str) -> dict[str, Any] | None:
    """Use the browser's own request context so we test the exact cookie jar."""
    try:
        response = context.request.get(
            f"{base_url}/api/v1/users/self",
            headers={"Accept": "application/json+canvas-string-ids, application/json"},
        )
        if not response.ok:
            return None
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) and data.get("id") else None
