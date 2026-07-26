"""Working out which Canvas credential to use, in order of least hassle.

The chain is:

1. Environment variables, for people who do have a token or already know their cookie.
2. The session saved by a previous ``connect`` / ``canvas-mcp login``.
3. A scan of local browser profiles for a Canvas session cookie.

Every candidate is proven against ``/api/v1/users/self`` before it is trusted, so a
stale cookie fails loudly here rather than halfway through answering a question.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from . import browser_cookies, config
from .client import CanvasClient, Credentials
from .errors import CONNECT_HINT, AuthError, CanvasMCPError


@dataclass
class Connection:
    """A credential that has been checked and the profile it belongs to."""

    credentials: Credentials
    profile: dict[str, Any]

    @property
    def display_name(self) -> str:
        return self.profile.get("short_name") or self.profile.get("name") or "unknown user"


async def validate(creds: Credentials, *, timeout: float = 20.0) -> dict[str, Any]:
    """Prove a credential works. Returns the Canvas profile or raises AuthError."""
    client = CanvasClient(creds, timeout=timeout)
    try:
        profile = await client.get_json("/api/v1/users/self")
    finally:
        await client.aclose()
    if not isinstance(profile, dict) or not profile.get("id"):
        raise AuthError(
            f"{creds.base_url} answered, but not with a Canvas profile. "
            "Double-check the URL is your school's Canvas host.",
            hint=CONNECT_HINT,
        )
    return profile


def credentials_from_env() -> Credentials | None:
    base_url = config.env_base_url()
    token = os.environ.get("CANVAS_API_TOKEN", "").strip()
    cookie = os.environ.get("CANVAS_SESSION_COOKIE", "").strip()

    if token:
        if not base_url:
            raise AuthError(
                "CANVAS_API_TOKEN is set but CANVAS_BASE_URL is not. "
                "Set CANVAS_BASE_URL=https://yourschool.instructure.com"
            )
        return Credentials(base_url=base_url, token=token, source="env:CANVAS_API_TOKEN")

    if cookie:
        if not base_url:
            raise AuthError(
                "CANVAS_SESSION_COOKIE is set but CANVAS_BASE_URL is not. "
                "Set CANVAS_BASE_URL=https://yourschool.instructure.com"
            )
        return Credentials(
            base_url=base_url,
            cookies=parse_cookie_input(cookie),
            source="env:CANVAS_SESSION_COOKIE",
        )
    return None


def credentials_from_store() -> Credentials | None:
    stored = config.load_session()
    if stored is None:
        return None
    return Credentials(
        base_url=stored.base_url,
        cookies=dict(stored.cookies),
        token=stored.token,
        source=stored.source or "saved session",
    )


def parse_cookie_input(raw: str) -> dict[str, str]:
    """Accept a bare cookie value, ``name=value``, or a whole ``Cookie:`` header."""
    raw = raw.strip()
    if not raw:
        return {}
    if "=" not in raw:
        return {"canvas_session": raw}
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()

    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies[name.strip()] = value.strip()
    return cookies


async def try_browser_import(base_url_hint: str = "") -> tuple[Connection | None, list[str]]:
    """Scan local browsers and return the first Canvas session that actually works."""
    host_filter = urlparse(base_url_hint).hostname if base_url_hint else None
    sessions, notes = browser_cookies.discover_sessions(host_filter)

    if not sessions:
        return None, notes

    for session in sessions:
        if not session.has_session():
            notes.append(f"{session.describe()}: no Canvas session cookie in that profile")
            continue
        creds = Credentials(
            base_url=session.base_url,
            cookies=session.cookies,
            source=f"browser:{session.browser}",
        )
        try:
            profile = await validate(creds)
        except AuthError:
            notes.append(f"{session.describe()}: cookie found but Canvas says it is expired")
            continue
        except CanvasMCPError as exc:
            notes.append(f"{session.describe()}: {exc}")
            continue
        return Connection(credentials=creds, profile=profile), notes

    return None, notes


async def connect(
    *,
    base_url: str = "",
    session_cookie: str = "",
    allow_browser_scan: bool = True,
    persist: bool = True,
) -> tuple[Connection, list[str]]:
    """Establish a working connection, trying every avenue. Raises AuthError if none work."""
    notes: list[str] = []
    base_url = config.normalize_base_url(base_url) if base_url else ""

    if session_cookie:
        target = base_url or config.env_base_url() or _stored_base_url()
        if not target:
            raise AuthError(
                "I need to know your Canvas address to use that cookie, "
                "e.g. https://yourschool.instructure.com"
            )
        creds = Credentials(
            base_url=target, cookies=parse_cookie_input(session_cookie), source="manual cookie"
        )
        profile = await validate(creds)
        connection = Connection(creds, profile)
        if persist:
            _persist(connection)
        return connection, notes

    env_creds = credentials_from_env()
    if env_creds and (not base_url or env_creds.base_url == base_url):
        profile = await validate(env_creds)
        return Connection(env_creds, profile), notes

    stored_creds = credentials_from_store()
    if stored_creds and (not base_url or stored_creds.base_url == base_url):
        try:
            profile = await validate(stored_creds)
            return Connection(stored_creds, profile), notes
        except AuthError:
            notes.append("The previously saved session has expired; looking for a fresh one.")

    if allow_browser_scan:
        connection, scan_notes = await try_browser_import(base_url or _stored_base_url())
        notes.extend(scan_notes)
        if connection:
            if persist:
                _persist(connection)
            return connection, notes

    raise AuthError(_failure_message(notes), hint=CONNECT_HINT)


def refresh_stored_cookies(base_url: str, cookies: dict[str, str]) -> bool:
    """Write a rotated cookie jar back to the saved session.

    Canvas hands out a new session cookie as you use it. Persisting the current jar
    means the saved session ages from the last request rather than the first, which
    is most of what stops a student having to reconnect every morning.
    """
    stored = config.load_session()
    if stored is None or stored.base_url != base_url or not cookies:
        return False
    if stored.cookies == cookies:
        return False
    stored.cookies = dict(cookies)
    stored.saved_at = time.time()
    config.save_session(stored)
    return True


def _stored_base_url() -> str:
    stored = config.load_session()
    return stored.base_url if stored else ""


def _persist(connection: Connection) -> None:
    stored = connection.credentials.to_stored(user=_slim_profile(connection.profile))
    stored.saved_at = time.time()
    config.save_session(stored)


def _slim_profile(profile: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "name", "short_name", "sortable_name", "primary_email", "login_id", "time_zone")
    return {k: profile[k] for k in keep if k in profile}


def _failure_message(notes: list[str]) -> str:
    lines = ["I couldn't find a working Canvas session on this computer."]
    if notes:
        lines.append("")
        lines.append("What I tried:")
        lines.extend(f"  - {note}" for note in notes)
    return "\n".join(lines)
