"""Read the Canvas session cookie out of a browser the student is already logged into.

This is the whole trick behind "no API key needed". Canvas's own web UI talks to
``/api/v1/...`` using nothing but the session cookie, so if we borrow that cookie
from the local browser profile we get exactly the access the student already has -
no developer key, no institution-blocked token page.

Two readers live here:

* Firefox and its forks, done with plain sqlite3 from the standard library, so the
  common case needs no third-party dependency at all.
* Everything Chromium-based (Chrome, Edge, Brave, Vivaldi, Opera, Arc) plus Safari,
  delegated to ``browser_cookie3`` when it is installed, because those stores are
  encrypted with an OS keychain and re-implementing that here would be silly.

Only cookies belonging to a Canvas host are ever returned or written to disk.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

# Rails session cookies Canvas is known to set. Presence of any of these is our
# signal that a host is a Canvas instance rather than some other site.
SESSION_COOKIE_NAMES = (
    "canvas_session",
    "_normandy_session",
    "_legacy_normandy_session",
)
# "Stay signed in" cookies. These outlive the session by weeks, and Canvas will
# mint a fresh session from one - which is the difference between reconnecting
# once a month and reconnecting every morning.
REMEMBER_COOKIE_NAMES = (
    "pseudonym_credentials",
    "canvas_pseudonym",
)
# Cookies worth replaying alongside the session (CSRF token, sticky routing, etc.).
USEFUL_COOKIE_NAMES = SESSION_COOKIE_NAMES + REMEMBER_COOKIE_NAMES + (
    "_csrf_token",
    "log_session_id",
    "canvas_theme_preference",
)


@dataclass
class BrowserSession:
    """Candidate Canvas login found in a local browser profile."""

    browser: str
    host: str
    cookies: dict[str, str] = field(default_factory=dict)
    profile: str = ""

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"

    def has_session(self) -> bool:
        return any(name in self.cookies for name in SESSION_COOKIE_NAMES)

    def has_remember_me(self) -> bool:
        """A "stay signed in" cookie means this login survives session expiry."""
        return any(is_remember_me(name) for name in self.cookies)

    def describe(self) -> str:
        where = f"{self.browser}" + (f" ({self.profile})" if self.profile else "")
        return f"{self.host} via {where}"


def is_remember_me(name: str) -> bool:
    """Canvas suffixes the remember-me cookie per account on some deployments."""
    return name in REMEMBER_COOKIE_NAMES or name.startswith(
        ("pseudonym_credentials", "canvas_pseudonym")
    )


def looks_like_canvas(host: str, cookie_names: set[str]) -> bool:
    host = host.lstrip(".").lower()
    if any(name in cookie_names for name in SESSION_COOKIE_NAMES):
        return True
    return "instructure.com" in host or host.startswith("canvas.") or ".canvas." in host


def host_matches(cookie_domain: str, host: str) -> bool:
    """Cookie-domain matching: ``.instructure.com`` covers ``school.instructure.com``."""
    cookie_domain = cookie_domain.lower().lstrip(".")
    host = host.lower().lstrip(".")
    return host == cookie_domain or host.endswith("." + cookie_domain)


# --------------------------------------------------------------------------- #
# Firefox family (no third-party dependency)
# --------------------------------------------------------------------------- #

def _firefox_profile_roots() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        bases = [
            home / "Library/Application Support/Firefox/Profiles",
            home / "Library/Application Support/LibreWolf/Profiles",
            home / "Library/Application Support/Zen/Profiles",
        ]
    elif os.name == "nt":
        appdata = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        bases = [
            appdata / "Mozilla/Firefox/Profiles",
            appdata / "librewolf/Profiles",
            appdata / "zen/Profiles",
        ]
    else:
        bases = [
            home / ".mozilla/firefox",
            home / ".librewolf",
            home / "snap/firefox/common/.mozilla/firefox",
            home / ".var/app/org.mozilla.firefox/.mozilla/firefox",
            home / ".zen",
        ]
    return [b for b in bases if b.is_dir()]


def _firefox_cookie_dbs() -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for root in _firefox_profile_roots():
        label = "librewolf" if "librewolf" in root.as_posix().lower() else (
            "zen" if "zen" in root.as_posix().lower() else "firefox"
        )
        for db in sorted(root.glob("*/cookies.sqlite")):
            found.append((f"{label}:{db.parent.name}", db))
    return found


@contextmanager
def _opened_copy(db: Path):
    """Copy the DB aside first - a running browser holds a lock on the original."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / db.name
        shutil.copy2(db, target)
        for suffix in ("-wal", "-shm"):
            side = db.with_name(db.name + suffix)
            if side.exists():
                shutil.copy2(side, target.with_name(target.name + suffix))
        conn = sqlite3.connect(f"file:{target}?immutable=0", uri=True)
        try:
            yield conn
        finally:
            conn.close()


# Hosts are identified first, then read in full. Finding the host by cookie name
# alone would drop everything else that host set - including the remember-me
# cookie, which is the one that keeps a login alive for weeks.
_CANDIDATE_HOSTS_SQL = (
    "SELECT DISTINCT host FROM moz_cookies "
    "WHERE host LIKE '%instructure.com' OR host LIKE '%canvas%' OR name IN (?, ?, ?)"
)


def firefox_sessions(host_filter: str | None = None) -> list[BrowserSession]:
    sessions: list[BrowserSession] = []
    for label, db in _firefox_cookie_dbs():
        try:
            with _opened_copy(db) as conn:
                hosts = [
                    str(row[0]) for row in conn.execute(_CANDIDATE_HOSTS_SQL, SESSION_COOKIE_NAMES)
                ]
                if host_filter:
                    hosts = [h for h in hosts if host_matches(h, host_filter)]
                if not hosts:
                    continue
                placeholders = ",".join("?" * len(hosts))
                rows = list(
                    conn.execute(
                        f"SELECT host, name, value FROM moz_cookies WHERE host IN ({placeholders})",
                        hosts,
                    )
                )
        except (sqlite3.Error, OSError):
            continue

        by_host: dict[str, dict[str, str]] = {}
        for host, name, value in rows:
            by_host.setdefault(str(host).lstrip("."), {})[str(name)] = str(value)
        browser, _, profile = label.partition(":")
        for host, cookies in by_host.items():
            if host_filter and not host_matches(host, host_filter):
                continue
            if not looks_like_canvas(host, set(cookies)):
                continue
            sessions.append(BrowserSession(browser=browser, host=host, cookies=cookies, profile=profile))
    return sessions


# --------------------------------------------------------------------------- #
# Chromium family + Safari (via browser_cookie3)
# --------------------------------------------------------------------------- #

_BC3_LOADERS = (
    "chrome", "chromium", "brave", "edge", "vivaldi", "opera", "opera_gx", "arc", "safari",
)


def browser_cookie3_available() -> bool:
    try:
        import browser_cookie3  # noqa: F401
    except Exception:
        return False
    return True


def bc3_sessions(host_filter: str | None = None) -> tuple[list[BrowserSession], list[str]]:
    """Returns (sessions, notes). Notes explain browsers we could not read."""
    notes: list[str] = []
    try:
        import browser_cookie3  # type: ignore
    except Exception:
        return [], [
            "browser_cookie3 is not installed, so Chrome/Edge/Brave/Safari were skipped "
            "(pip install 'canvas-mcp[browsers]')."
        ]

    sessions: list[BrowserSession] = []
    for name in _BC3_LOADERS:
        loader = getattr(browser_cookie3, name, None)
        if loader is None:
            continue
        try:
            # Passing domain_name keeps us from decrypting the student's whole cookie jar
            # when we already know which host we want.
            jar = loader(domain_name=host_filter) if host_filter else loader()
        except Exception as exc:  # locked profile, no keychain access, browser absent
            message = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
            if "could not find" not in message.lower() and "no such file" not in message.lower():
                notes.append(f"{name}: {message}")
            continue

        # Same two-pass shape as the Firefox reader: work out which hosts are
        # Canvas, then keep everything those hosts set and nothing from anywhere else.
        by_host: dict[str, dict[str, str]] = {}
        for cookie in jar:
            host = (cookie.domain or "").lstrip(".")
            if not host:
                continue
            if host_filter and not host_matches(host, host_filter):
                continue
            by_host.setdefault(host, {})[cookie.name] = cookie.value or ""

        for host, cookies in by_host.items():
            if not looks_like_canvas(host, set(cookies)):
                continue
            sessions.append(BrowserSession(browser=name, host=host, cookies=cookies))
    return sessions, notes


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def discover_sessions(host_filter: str | None = None) -> tuple[list[BrowserSession], list[str]]:
    """Scan every browser we know how to read.

    ``host_filter`` is a bare hostname; when given, only that host's cookies are
    touched. Sessions that actually carry a Canvas session cookie sort first.
    """
    notes: list[str] = []
    sessions: list[BrowserSession] = []

    try:
        sessions.extend(firefox_sessions(host_filter))
    except Exception as exc:  # pragma: no cover - defensive
        notes.append(f"firefox: {exc}")

    bc3, bc3_notes = bc3_sessions(host_filter)
    sessions.extend(bc3)
    notes.extend(bc3_notes)

    # Collapse duplicates (same host seen in two browsers) keeping the richer jar.
    best: dict[tuple[str, str], BrowserSession] = {}
    for session in sessions:
        key = (session.host, session.browser)
        current = best.get(key)
        if current is None or len(session.cookies) > len(current.cookies):
            best[key] = session

    # A login that carries a remember-me cookie is worth more than one that doesn't:
    # it survives session expiry, so it is what we want saved.
    ordered = sorted(
        best.values(),
        key=lambda s: (not s.has_session(), not s.has_remember_me(), s.host, s.browser),
    )
    return ordered, notes
