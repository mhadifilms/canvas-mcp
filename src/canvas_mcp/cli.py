"""Command line entry point: run the server, or get a session onto this machine."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from typing import Any

from . import auth, browser_cookies, config
from .errors import CONNECT_HINT, AuthError, CanvasMCPError

COOKIE_INSTRUCTIONS = """\
Where to find the cookie:
  1. Open Canvas in your browser and make sure you're logged in.
  2. Open DevTools (F12, or Cmd-Option-I on a Mac).
  3. Go to Application (Chrome/Edge) or Storage (Firefox) -> Cookies -> your Canvas URL.
  4. Copy the *value* of the cookie named canvas_session (some schools call it
     _normandy_session). It's long. It will not show up in the Console tab -
     it's HttpOnly, which is why you need the Application/Storage tab.

Treat that value like your password: anyone holding it is logged in as you."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="canvas-mcp",
        description="Canvas LMS over MCP, using the browser session you already have.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Run the MCP server on stdio (default).")

    login = sub.add_parser("login", help="Open a browser window and log in through your school.")
    login.add_argument("--base-url", default="", help="https://yourschool.instructure.com")
    login.add_argument("--timeout", type=int, default=300, help="Seconds to wait for login.")

    imp = sub.add_parser("import-cookies", help="Find a Canvas session in a local browser and save it.")
    imp.add_argument("--base-url", default="", help="Limit the search to one Canvas host.")

    setc = sub.add_parser("set-cookie", help="Paste a Canvas session cookie by hand.")
    setc.add_argument("--base-url", default="", help="https://yourschool.instructure.com")
    setc.add_argument("--cookie", default="", help="Cookie value (omit to be prompted, which hides it).")

    brief = sub.add_parser("digest", help="Print a short brief of what's due and what's missing.")
    brief.add_argument("--days", type=int, default=7, help="How far ahead to look.")

    cal = sub.add_parser("ics", help="Export upcoming deadlines as a calendar file.")
    cal.add_argument("--days", type=int, default=120, help="How far ahead to export.")
    cal.add_argument("--out", default="", help="Where to write the .ics (default ~/Downloads).")

    sub.add_parser("status", help="Show the saved session and check that it still works.")
    sub.add_parser("logout", help="Delete the saved session.")
    sub.add_parser("doctor", help="Diagnose why connecting isn't working.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"

    try:
        if command == "serve":
            from .server import main as serve_main

            serve_main()
            return 0
        if command == "login":
            return cmd_login(args)
        if command == "import-cookies":
            return asyncio.run(cmd_import(args))
        if command == "set-cookie":
            return asyncio.run(cmd_set_cookie(args))
        if command == "digest":
            return asyncio.run(cmd_digest(args))
        if command == "ics":
            return asyncio.run(cmd_ics(args))
        if command == "status":
            return asyncio.run(cmd_status())
        if command == "logout":
            return cmd_logout()
        if command == "doctor":
            return asyncio.run(cmd_doctor())
    except KeyboardInterrupt:
        return 130
    except CanvasMCPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 1


def _ask_base_url(given: str) -> str:
    base_url = config.normalize_base_url(given or config.env_base_url())
    if base_url:
        return base_url
    stored = config.load_session()
    if stored:
        return stored.base_url
    entered = input("Canvas address (e.g. https://yourschool.instructure.com): ").strip()
    base_url = config.normalize_base_url(entered)
    if not base_url:
        raise CanvasMCPError("A Canvas address is required.")
    return base_url


def cmd_login(args: Any) -> int:
    from .login import interactive_login

    base_url = _ask_base_url(args.base_url)
    session = interactive_login(base_url, timeout_seconds=args.timeout)
    print(f"Saved to {config.session_path()}")
    print(f"You are set. Point your MCP client at `canvas-mcp` and ask about {session.base_url}.")
    return 0


async def cmd_import(args: Any) -> int:
    base_url = config.normalize_base_url(args.base_url) if args.base_url else ""
    print("Looking through local browsers for a Canvas login...")
    connection, notes = await auth.try_browser_import(base_url)
    for note in notes:
        print(f"  - {note}")
    if connection is None:
        print("\nNo usable Canvas session found.")
        print(CONNECT_HINT)
        return 1
    print(
        f"\nConnected to {connection.credentials.base_url} as {connection.display_name}"
        f" (from {connection.credentials.source})."
    )
    print(f"Saved to {config.session_path()}")
    return 0


async def cmd_set_cookie(args: Any) -> int:
    base_url = _ask_base_url(args.base_url)
    cookie = args.cookie
    if not cookie:
        print(COOKIE_INSTRUCTIONS)
        print()
        cookie = getpass.getpass("Paste the cookie value (input hidden): ").strip()
    if not cookie:
        raise CanvasMCPError("No cookie provided.")

    connection, _ = await auth.connect(base_url=base_url, session_cookie=cookie)
    print(f"Connected to {base_url} as {connection.display_name}.")
    print(f"Saved to {config.session_path()}")
    return 0


async def _connected_client():
    """A validated client for the one-shot commands, or a clear failure."""
    from .client import CanvasClient

    connection, _notes = await auth.connect(allow_browser_scan=config.auto_import_enabled())
    return CanvasClient(connection.credentials), connection


async def cmd_digest(args: Any) -> int:
    from .digest import build_digest
    from .formatting import resolve_timezone

    client, connection = await _connected_client()
    try:
        tz = resolve_timezone(connection.profile.get("time_zone"))
        print(await build_digest(client, days=max(1, args.days), tz=tz))
    finally:
        await client.aclose()
    return 0


async def cmd_ics(args: Any) -> int:
    from pathlib import Path

    from . import ics, queries
    from .formatting import parse_iso

    client, _connection = await _connected_client()
    try:
        courses = await queries.courses_by_id(client)
        items = queries.open_items(
            await queries.planner_items(client, days=max(1, args.days), lookback_hours=0)
        )
        entries = []
        for item in items:
            when = parse_iso(item.get("plannable_date"))
            if when is None:
                continue
            course = courses.get(str(item.get("course_id") or ""))
            code = (course or {}).get("course_code") or (course or {}).get("name") or "Canvas"
            entries.append(
                ics.CalendarItem(
                    summary=f"{code}: {queries.item_title(item)}",
                    start=when,
                    uid_seed=f"{item.get('plannable_type')}-{item.get('plannable_id')}",
                    description="Due in Canvas",
                    categories=[str(code)],
                )
            )
    finally:
        await client.aclose()

    target = Path(args.out).expanduser() if args.out else Path.home() / "Downloads" / "canvas-deadlines.ics"
    if target.is_dir():
        target = target / "canvas-deadlines.ics"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ics.build_calendar(entries), encoding="utf-8")
    print(f"Wrote {len(entries)} deadlines to {target}")
    return 0


async def cmd_status() -> int:
    stored = config.load_session()
    if stored is None:
        print("No saved Canvas session.")
        print(CONNECT_HINT)
        return 1
    print(f"Saved session : {stored.base_url}")
    print(f"Source        : {stored.source}")
    print(f"User          : {stored.user.get('name', 'unknown')}")
    print(f"Age           : {stored.age_seconds() / 3600:.1f} hours")
    try:
        connection, _ = await auth.connect(base_url=stored.base_url, allow_browser_scan=False, persist=False)
    except AuthError as exc:
        print("\nThe saved session no longer works.")
        print(exc.full_message())
        return 1
    print(f"Check         : OK, Canvas still recognises {connection.display_name}")
    return 0


def cmd_logout() -> int:
    print("Deleted saved session." if config.clear_session() else "Nothing to delete.")
    return 0


async def cmd_doctor() -> int:
    print("canvas-mcp doctor")
    print("-----------------")
    print(f"Python           : {sys.version.split()[0]}")
    print(f"Config directory : {config.config_dir()}")
    print(f"Session file     : {config.session_path()} "
          f"({'present' if config.session_path().exists() else 'absent'})")
    print(f"CANVAS_BASE_URL  : {config.env_base_url() or '(unset)'}")
    print(f"Read-only mode   : {config.read_only()}")
    print(f"Auto browser scan: {config.auto_import_enabled()}")

    print("\nOptional dependencies")
    print(f"  browser_cookie3 : {'installed' if browser_cookies.browser_cookie3_available() else 'missing (Chrome/Edge/Brave/Safari cannot be read)'}")
    try:
        import playwright  # noqa: F401

        print("  playwright      : installed")
    except ImportError:
        print("  playwright      : missing (`canvas-mcp login` unavailable)")

    from . import documents

    readable = ", ".join(name for name, ok in documents.available_readers().items() if ok) or "none"
    unreadable = [name for name, ok in documents.available_readers().items() if not ok]
    print(f"  file reading    : {readable}")
    if unreadable:
        print(f"                    missing {', '.join(unreadable)} "
              "(pip install 'canvas-mcp[documents]')")

    print("\nBrowsers on this machine")
    sessions, notes = browser_cookies.discover_sessions()
    if sessions:
        for session in sessions:
            mark = "session cookie found" if session.has_session() else "no session cookie"
            print(f"  - {session.describe()}: {mark}, {len(session.cookies)} cookies")
    else:
        print("  - no Canvas cookies found in any readable browser profile")
    for note in notes:
        print(f"  ! {note}")

    print("\nConnection")
    try:
        connection, _ = await auth.connect(persist=False)
        print(f"  OK - {connection.credentials.base_url} as {connection.display_name}")
        return 0
    except AuthError as exc:
        print(f"  FAILED\n{exc.full_message()}")
        return 1
