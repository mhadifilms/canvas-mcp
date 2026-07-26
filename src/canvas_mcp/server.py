"""The MCP server: every tool an assistant needs to help a student run their semester.

Design notes:

* Tools return readable text, not raw JSON. The model reads this, and so does the
  student when their client shows tool output.
* Reads are unrestricted. Writes are limited to the student's own planner (to-do
  notes and "mark as done"). Nothing here submits coursework, posts to a
  discussion, or takes a quiz - keeping deadlines straight is the job.
* Anything that can fail with "you aren't logged in" says how to fix it.
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin

from mcp.server.fastmcp import FastMCP

from . import auth, config, digest, documents, gradecalc, ics, queries
from .client import CanvasClient, chunked
from .errors import AuthError, CanvasMCPError
from .formatting import (
    format_day,
    format_due,
    html_to_text,
    humanize_delta,
    parse_iso,
    points_label,
    resolve_timezone,
    submission_status,
    truncate,
)

INSTRUCTIONS = """\
Canvas LMS access for a student, using the login session already present on this
computer - no API key required.

Start with `canvas_status`. If it reports "not connected", call `connect`.

For "what do I need to do?" questions use `upcoming` and `missing_work` first;
they cover every course in one call. Reach for the per-course tools
(`list_assignments`, `get_assignment`, `course_modules`, `grades`) once you know
which class matters.

For anything about grades, prefer `grade_forecast`, `what_if` and `triage` over
doing the arithmetic yourself - they use the course's real assignment group
weights, which is the part that is easy to get wrong. `triage` is the right tool
when a student has fallen behind and needs to know what to do first.

`read_file` extracts the text of slides and readings, so course material can be
discussed rather than just listed. `export_calendar` puts deadlines on the
student's phone calendar.

Course arguments are forgiving: an id, a course code, or part of the name all work.

This server reads coursework and manages the student's own planner. It cannot
submit assignments, post replies, or take quizzes, and should not be asked to
produce work the student is supposed to write themselves.
"""

mcp = FastMCP("canvas", instructions=INSTRUCTIONS)


# --------------------------------------------------------------------------- #
# Connection state
# --------------------------------------------------------------------------- #

class _State:
    client: CanvasClient | None = None
    profile: dict[str, Any] = {}
    source: str = ""
    revalidate_after: float = 0.0
    keepalive: "asyncio.Task[None] | None" = None


_state = _State()
_lock = asyncio.Lock()
REVALIDATE_SECONDS = 900


def _keepalive_interval() -> int:
    """Seconds between keepalive pings; 0 disables it."""
    try:
        return max(0, int(os.environ.get("CANVAS_MCP_KEEPALIVE_SECONDS", "600")))
    except ValueError:
        return 600


async def _keepalive_tick() -> None:
    """One keepalive beat: prove the session is alive, then save the rotated jar."""
    client = _state.client
    if client is None:
        return
    try:
        await client.get_json("/api/v1/users/self")
    except AuthError:
        # Don't reconnect from a background task - just make the next tool call
        # revalidate, which reconnects with the student's attention on it.
        _state.revalidate_after = 0.0
        return
    except CanvasMCPError:
        return  # transient; try again next tick
    try:
        auth.refresh_stored_cookies(client.base_url, client.current_cookies())
    except OSError:
        pass


async def _keepalive_loop(interval: int) -> None:
    """Keep the borrowed session from idling out.

    A Rails session expires on inactivity, so a student who asks about Canvas once a
    day would find it dead every time. Touching a cheap endpoint on a timer keeps it
    alive for as long as this server runs, and saving the rotated cookies means the
    freshness survives a restart too.
    """
    while True:
        await asyncio.sleep(interval)
        await _keepalive_tick()


def _ensure_keepalive() -> None:
    if _state.keepalive is not None and not _state.keepalive.done():
        return
    interval = _keepalive_interval()
    if interval <= 0:
        return
    try:
        _state.keepalive = asyncio.create_task(_keepalive_loop(interval))
    except RuntimeError:  # pragma: no cover - no running loop (unit tests)
        _state.keepalive = None


async def _get_client() -> CanvasClient:
    """Return a live client, connecting (and re-connecting) as needed."""
    async with _lock:
        if _state.client is not None and time.monotonic() < _state.revalidate_after:
            return _state.client

        if _state.client is not None:
            # Cheap liveness check; a cookie can die mid-session.
            try:
                await _state.client.get_json("/api/v1/users/self")
                _state.revalidate_after = time.monotonic() + REVALIDATE_SECONDS
                return _state.client
            except AuthError:
                await _state.client.aclose()
                _state.client = None

        connection, _notes = await auth.connect(
            allow_browser_scan=config.auto_import_enabled()
        )
        _state.client = CanvasClient(connection.credentials)
        _state.profile = connection.profile
        _state.source = connection.credentials.source
        _state.revalidate_after = time.monotonic() + REVALIDATE_SECONDS
        _ensure_keepalive()
        return _state.client


async def _reset_client() -> None:
    async with _lock:
        if _state.client is not None:
            await _state.client.aclose()
        _state.client = None
        _state.profile = {}
        _state.source = ""
        _state.revalidate_after = 0.0


def _tz():
    return resolve_timezone(_state.profile.get("time_zone"))


def handle_errors(
    fn: Callable[..., Awaitable[str]] | None = None, *, reconnect: bool = True
) -> Any:
    """Turn expected failures into helpful prose instead of protocol errors.

    On an expired session this quietly re-establishes the connection and runs the
    tool again, so a cookie dying mid-conversation costs the student nothing. Set
    ``reconnect=False`` on the tools that manage the connection themselves.
    """

    def decorate(inner: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        @functools.wraps(inner)
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                return await inner(*args, **kwargs)
            except AuthError as exc:
                if not reconnect:
                    return f"Not connected to Canvas.\n\n{exc.full_message()}"
            except CanvasMCPError as exc:
                return str(exc)
            except Exception as exc:  # pragma: no cover - last line of defence
                return f"Something went wrong talking to Canvas: {exc.__class__.__name__}: {exc}"

            # Second attempt, on a freshly established session.
            try:
                await _reset_client()
                await _get_client()
                return await inner(*args, **kwargs)
            except AuthError as exc:
                return f"Not connected to Canvas.\n\n{exc.full_message()}"
            except CanvasMCPError as exc:
                return str(exc)
            except Exception as exc:  # pragma: no cover
                return f"Something went wrong talking to Canvas: {exc.__class__.__name__}: {exc}"

        return wrapper

    return decorate(fn) if fn is not None else decorate


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #

def _course_label(course: dict[str, Any]) -> str:
    code = (course.get("course_code") or "").strip()
    name = (course.get("name") or "").strip()
    if code and code.lower() not in name.lower():
        return f"{name} ({code})"
    return name or code or f"course {course.get('id')}"


def _percent(value: Any) -> str:
    """Canvas sends 91.0 for a clean 91; don't make students read the trailing zero."""
    try:
        return f"{float(value):g}%"
    except (TypeError, ValueError):
        return str(value)


def _short_label(course: dict[str, Any] | None) -> str:
    if not course:
        return "Personal"
    return (course.get("course_code") or course.get("name") or "").strip() or f"course {course.get('id')}"


def _abs_url(client: CanvasClient, url: Any) -> str:
    if not url or not isinstance(url, str):
        return ""
    return url if url.startswith("http") else urljoin(client.base_url + "/", url.lstrip("/"))


async def _resolve_assignment(client: CanvasClient, course_id: Any, reference: str) -> dict[str, Any]:
    """Find an assignment by id or name, and always return the detailed record.

    The index endpoint omits things like score statistics, so once we know the id we
    fetch it properly - a student who typed a name gets the same answer as one who
    had the id to hand.
    """
    ref = str(reference).strip()
    assignment_id = ref if ref.isdigit() else _pick_assignment_id(
        await client.paginate(
            f"/api/v1/courses/{course_id}/assignments",
            {"include[]": ["submission"], "order_by": "due_at"},
            limit=300,
        ),
        ref,
    )

    data = await client.get_json(
        f"/api/v1/courses/{course_id}/assignments/{assignment_id}",
        {"include[]": ["submission", "score_statistics"]},
    )
    if not isinstance(data, dict) or not data.get("id"):
        raise CanvasMCPError(f'Could not load assignment "{reference}".')
    return data


def _pick_assignment_id(assignments: list[dict[str, Any]], reference: str) -> str:
    needle = reference.lower()
    exact = [a for a in assignments if str(a.get("name", "")).lower() == needle]
    if exact:
        return str(exact[0]["id"])
    partial = [a for a in assignments if needle in str(a.get("name", "")).lower()]
    if len(partial) == 1:
        return str(partial[0]["id"])
    if len(partial) > 1:
        options = "\n".join(f"  - {a.get('name')} (id {a.get('id')})" for a in partial[:12])
        raise CanvasMCPError(f'"{reference}" matches several assignments:\n{options}')
    raise CanvasMCPError(f'No assignment matching "{reference}" in that course.')


def _parse_when(value: str) -> datetime:
    """Accept 'today', 'tomorrow', '2026-08-01', or '2026-08-01 17:00'."""
    tz = _tz()
    text = (value or "").strip().lower()
    today = datetime.now(tz).replace(hour=23, minute=59, second=0, microsecond=0)
    if text in ("", "today"):
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)

    normalized = value.strip().replace("/", "-")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(normalized[: len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d":
            parsed = parsed.replace(hour=23, minute=59)
        return parsed.replace(tzinfo=tz)
    raise CanvasMCPError(
        f'I could not read "{value}" as a date. Use YYYY-MM-DD, "YYYY-MM-DD HH:MM", today, or tomorrow.'
    )


def _require_writes() -> None:
    if config.read_only():
        raise CanvasMCPError("This server is running read-only (CANVAS_MCP_READ_ONLY is set).")


# --------------------------------------------------------------------------- #
# Connection tools
# --------------------------------------------------------------------------- #

@mcp.tool()
@handle_errors(reconnect=False)
async def canvas_status() -> str:
    """Check whether Canvas is connected, and as whom. Start here."""
    stored = config.load_session()
    lines: list[str] = []

    try:
        client = await _get_client()
    except AuthError as exc:
        lines.append("Not connected to Canvas.")
        if stored:
            lines.append(f"Last saved session: {stored.base_url} (source: {stored.source})")
        lines.append("")
        lines.append(exc.full_message())
        return "\n".join(lines)

    profile = _state.profile or await client.profile()
    courses = await client.courses()
    lines.append(f"Connected to {client.base_url}")
    lines.append(f"Signed in as {profile.get('name', 'unknown')} ({profile.get('login_id', 'no login id')})")
    lines.append(f"Credential: {_state.source or 'saved session'} - no API key involved")
    lines.append(f"Active courses: {len(courses)}")
    if config.read_only():
        lines.append("Mode: read-only (planner writes disabled)")
    return "\n".join(lines)


@mcp.tool()
@handle_errors(reconnect=False)
async def connect(base_url: str = "", session_cookie: str = "") -> str:
    """Connect to Canvas without an API key.

    With no arguments, looks through the browsers on this computer for a Canvas
    login and reuses it. Pass base_url (e.g. https://yourschool.instructure.com)
    to target a specific school, or session_cookie to supply the cookie by hand.
    """
    await _reset_client()
    connection, notes = await auth.connect(base_url=base_url, session_cookie=session_cookie)

    _state.client = CanvasClient(connection.credentials)
    _state.profile = connection.profile
    _state.source = connection.credentials.source
    _state.revalidate_after = time.monotonic() + REVALIDATE_SECONDS

    courses = await _state.client.courses()
    lines = [
        f"Connected to {connection.credentials.base_url} as {connection.display_name}.",
        f"Found {len(courses)} active course{'s' if len(courses) != 1 else ''}.",
        f"Session saved to {config.session_path()} (readable only by you).",
    ]
    if notes:
        lines.append("")
        lines.append("Notes from the search:")
        lines.extend(f"  - {note}" for note in notes)
    return "\n".join(lines)


@mcp.tool()
@handle_errors(reconnect=False)
async def browser_login(base_url: str) -> str:
    """Open a browser window so the student can log in through their school's normal
    sign-in page (SSO, 2-factor, whatever). Use this when `connect` cannot find a
    session. Returns immediately; call `canvas_status` once the login finishes.
    """
    target = config.normalize_base_url(base_url)
    if not target:
        return "Tell me your Canvas address first, e.g. https://yourschool.instructure.com"

    try:
        subprocess.Popen(  # noqa: S603 - launching our own CLI
            [sys.executable, "-m", "canvas_mcp", "login", "--base-url", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return f"Could not start the login window: {exc}\n\nRun this yourself:\n  canvas-mcp login --base-url {target}"

    await _reset_client()
    return (
        f"A browser window is opening at {target}.\n"
        "Log in the way you normally do, including any 2-factor prompt. The window closes\n"
        "by itself once Canvas lets you in - then ask me to check `canvas_status`.\n\n"
        "(If no window appeared, Playwright probably isn't installed: "
        "pip install 'canvas-mcp[login]' && python -m playwright install chromium)"
    )


@mcp.tool()
@handle_errors(reconnect=False)
async def disconnect() -> str:
    """Forget the saved Canvas session on this computer."""
    await _reset_client()
    removed = config.clear_session()
    return "Saved Canvas session deleted." if removed else "There was no saved session to delete."


# --------------------------------------------------------------------------- #
# The everyday tools
# --------------------------------------------------------------------------- #

@mcp.tool()
@handle_errors
async def list_courses(include_concluded: bool = False) -> str:
    """List the student's courses with ids, instructors, and current grade."""
    client = await _get_client()
    courses = await client.courses(include_concluded=include_concluded)
    if not courses:
        return "No courses found. If the term just started, they may not be published yet."

    lines = [f"{len(courses)} course{'s' if len(courses) != 1 else ''}:", ""]
    for course in courses:
        enrollment = (course.get("enrollments") or [{}])[0]
        score = enrollment.get("computed_current_score")
        grade = enrollment.get("computed_current_grade")
        bits = [f"id {course.get('id')}"]
        term = (course.get("term") or {}).get("name")
        if term:
            bits.append(term)
        teachers = ", ".join(t.get("display_name", "") for t in (course.get("teachers") or [])[:2])
        if teachers:
            bits.append(teachers)
        if score is not None:
            bits.append(f"grade {_percent(score)}" + (f" ({grade})" if grade else ""))
        if enrollment.get("enrollment_state") == "completed":
            bits.append("concluded")
        lines.append(f"- {_course_label(course)} - {' | '.join(bits)}")
    return "\n".join(lines)


def _planner_line(item: dict[str, Any], courses: dict[str, dict[str, Any]], tz) -> str:
    plannable = item.get("plannable") or {}
    title = plannable.get("title") or plannable.get("name") or "(untitled)"
    kind = queries.TYPE_LABEL.get(str(item.get("plannable_type")), str(item.get("plannable_type") or "item"))
    course = courses.get(str(item.get("course_id") or ""))
    when = parse_iso(item.get("plannable_date"))
    clock = when.astimezone(tz).strftime("%-I:%M %p" if os.name != "nt" else "%I:%M %p") if when else "--"

    bits = [f"{clock}  [{_short_label(course)}]  {title}", f"({kind}"]
    points = points_label(plannable.get("points_possible"))
    if points:
        bits[-1] += f", {points}"
    bits[-1] += ")"

    submissions = item.get("submissions")
    override = item.get("planner_override") or {}
    if override.get("marked_complete"):
        bits.append("- marked done")
    elif isinstance(submissions, dict):
        if submissions.get("graded"):
            bits.append("- graded")
        elif submissions.get("submitted"):
            bits.append("- submitted")
        elif submissions.get("missing"):
            bits.append("- MISSING")

    ident = item.get("plannable_id")
    if ident:
        bits.append(f"[{item.get('plannable_type')} {ident}]")
    return "  " + " ".join(bits)


@mcp.tool()
@handle_errors
async def upcoming(days: int = 14, include_done: bool = False, course: str = "") -> str:
    """Everything due in the next N days across all courses, grouped by day.

    This is the tool for "what do I have coming up?". Items already submitted or
    marked done are hidden unless include_done is true.
    """
    days = max(1, min(int(days), 90))
    client = await _get_client()
    tz = _tz()
    now = datetime.now(timezone.utc)
    courses = await queries.courses_by_id(client)

    course_filter: str | None = None
    if course:
        resolved = await client.resolve_course(course)
        course_filter = str(resolved.get("id"))

    try:
        items = await queries.planner_items(client, days=days)
    except CanvasMCPError:
        items = []

    if items:
        rows = queries.open_items(items) if not include_done else [
            i for i in items if str(i.get("plannable_type")) != "announcement"
        ]
        if course_filter:
            rows = [i for i in rows if str(i.get("course_id") or "") == course_filter]
        rows.sort(key=lambda i: parse_iso(i.get("plannable_date")) or now)

        if not rows:
            return f"Nothing due in the next {days} days" + (
                " for that course." if course_filter else " - you're clear."
            )

        out: list[str] = [f"Due in the next {days} days ({len(rows)} item{'s' if len(rows) != 1 else ''}):"]
        current_day = None
        for item in rows:
            day = format_day(item.get("plannable_date"), tz)
            if day != current_day:
                out.append("")
                out.append(day)
                current_day = day
            out.append(_planner_line(item, courses, tz))
        return "\n".join(out)

    # Planner unavailable on some deployments - assemble the same view by hand.
    return await _upcoming_fallback(client, courses, days, course_filter, include_done)


async def _upcoming_fallback(
    client: CanvasClient,
    courses: dict[str, dict[str, Any]],
    days: int,
    course_filter: str | None,
    include_done: bool,
) -> str:
    tz = _tz()
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    targets = [c for cid, c in courses.items() if not course_filter or cid == course_filter]

    async def fetch(course: dict[str, Any]) -> list[tuple[datetime, str]]:
        assignments = await client.paginate(
            f"/api/v1/courses/{course['id']}/assignments",
            {"bucket": "upcoming", "include[]": ["submission"], "order_by": "due_at"},
            limit=100,
        )
        rows: list[tuple[datetime, str]] = []
        for a in assignments:
            due = parse_iso(a.get("due_at"))
            if not due or due > horizon:
                continue
            status = submission_status(a.get("submission"), due=a.get("due_at"))
            if not include_done and status.startswith(("submitted", "graded", "excused")):
                continue
            label = (
                f"  {due.astimezone(tz).strftime('%-I:%M %p' if os.name != 'nt' else '%I:%M %p')}"
                f"  [{_short_label(course)}]  {a.get('name')} ({points_label(a.get('points_possible')) or 'assignment'})"
                f" - {status} [assignment {a.get('id')}]"
            )
            rows.append((due, label))
        return rows

    gathered = await asyncio.gather(*(fetch(c) for c in targets), return_exceptions=True)
    rows: list[tuple[datetime, str]] = []
    for result in gathered:
        if isinstance(result, list):
            rows.extend(result)
    if not rows:
        return f"Nothing due in the next {days} days."

    rows.sort(key=lambda r: r[0])
    out = [f"Due in the next {days} days ({len(rows)} items):"]
    current_day = None
    for due, label in rows:
        day = format_day(due.isoformat(), tz)
        if day != current_day:
            out.append("")
            out.append(day)
            current_day = day
        out.append(label)
    return "\n".join(out)


@mcp.tool()
@handle_errors
async def missing_work() -> str:
    """Assignments that are past due and still not submitted, across every course.

    The honest answer to "how far behind am I?".
    """
    client = await _get_client()
    courses = await queries.courses_by_id(client)
    missing = await client.paginate(
        "/api/v1/users/self/missing_submissions",
        {"include[]": ["planner_overrides"], "filter[]": ["submittable"]},
        limit=200,
    )
    missing = [m for m in missing if isinstance(m, dict)]
    if not missing:
        return "Nothing is showing as missing. Everything past due has been submitted or excused."

    missing.sort(key=lambda a: parse_iso(a.get("due_at")) or datetime.max.replace(tzinfo=timezone.utc))
    by_course: dict[str, list[dict[str, Any]]] = {}
    for item in missing:
        by_course.setdefault(str(item.get("course_id") or ""), []).append(item)

    total_points = sum(float(m.get("points_possible") or 0) for m in missing)
    out = [
        f"{len(missing)} missing assignment{'s' if len(missing) != 1 else ''}"
        + (f" worth {total_points:g} points total:" if total_points else ":"),
    ]
    for course_id, items in by_course.items():
        course = courses.get(course_id)
        out.append("")
        out.append(f"{_course_label(course) if course else f'course {course_id}'}")
        for item in items:
            due = parse_iso(item.get("due_at"))
            late = f"due {humanize_delta(due)}" if due else "no due date"
            still_open = ""
            lock_at = parse_iso(item.get("lock_at"))
            if lock_at:
                still_open = (
                    " - CLOSES " + humanize_delta(lock_at)
                    if lock_at > datetime.now(timezone.utc)
                    else " - submissions closed"
                )
            out.append(
                f"  - {item.get('name')} ({points_label(item.get('points_possible')) or 'ungraded'})"
                f" - {late}{still_open} [assignment {item.get('id')}]"
            )
    out.append("")
    out.append("Use `get_assignment` for what any of these actually require.")
    return "\n".join(out)


@mcp.tool()
@handle_errors
async def list_assignments(course: str, bucket: str = "", limit: int = 60) -> str:
    """All assignments in one course with due dates and submission status.

    bucket narrows the list: upcoming, past, overdue, undated, unsubmitted, ungraded.
    """
    client = await _get_client()
    resolved = await client.resolve_course(course)
    params: dict[str, Any] = {"include[]": ["submission"], "order_by": "due_at"}
    valid = {"upcoming", "past", "overdue", "undated", "unsubmitted", "ungraded", "future"}
    if bucket:
        if bucket.lower() not in valid:
            return f"bucket must be one of: {', '.join(sorted(valid))}"
        params["bucket"] = bucket.lower()

    assignments = await client.paginate(
        f"/api/v1/courses/{resolved['id']}/assignments", params, limit=max(1, min(limit, 200))
    )
    if not assignments:
        return f"No assignments{f' in bucket {bucket}' if bucket else ''} for {_course_label(resolved)}."

    tz = _tz()
    out = [f"{_course_label(resolved)} - {len(assignments)} assignment{'s' if len(assignments) != 1 else ''}"]
    if bucket:
        out[0] += f" ({bucket})"
    out.append("")
    for a in assignments:
        status = submission_status(a.get("submission"), due=a.get("due_at"))
        score = ""
        submission = a.get("submission") or {}
        if submission.get("score") is not None and a.get("points_possible"):
            score = f" - {submission['score']:g}/{float(a['points_possible']):g}"
        out.append(
            f"- {a.get('name')} [id {a.get('id')}]\n"
            f"    due {format_due(a.get('due_at'), tz)} | {points_label(a.get('points_possible')) or 'no points'}"
            f" | {status}{score}"
        )
    return "\n".join(out)


@mcp.tool()
@handle_errors
async def get_assignment(course: str, assignment: str) -> str:
    """Full detail for one assignment: instructions, rubric, dates, and where the
    student stands on it. Accepts an assignment id or part of its name.
    """
    client = await _get_client()
    resolved = await client.resolve_course(course)
    data = await _resolve_assignment(client, resolved["id"], assignment)
    tz = _tz()

    # Instructor comments are the most useful part of a graded assignment and the
    # assignment endpoint does not carry them.
    submission = data.get("submission") or {}
    try:
        detailed = await client.get_json(
            f"/api/v1/courses/{resolved['id']}/assignments/{data['id']}/submissions/self",
            {"include[]": ["submission_comments", "rubric_assessment"]},
        )
        if isinstance(detailed, dict) and detailed.get("id"):
            submission = detailed
    except CanvasMCPError:
        pass  # Not every assignment exposes a submission record; the summary still works.

    out = [f"{data.get('name')} - {_course_label(resolved)}", ""]
    out.append(f"Due:        {format_due(data.get('due_at'), tz)}")
    if data.get("unlock_at"):
        out.append(f"Opens:      {format_due(data.get('unlock_at'), tz)}")
    if data.get("lock_at"):
        out.append(f"Closes:     {format_due(data.get('lock_at'), tz)}")
    out.append(f"Worth:      {points_label(data.get('points_possible')) or 'not graded'}")
    types = ", ".join(data.get("submission_types") or []) or "unspecified"
    out.append(f"Submit as:  {types}")
    if data.get("allowed_extensions"):
        out.append(f"File types: {', '.join(data['allowed_extensions'])}")
    if data.get("allowed_attempts") and data["allowed_attempts"] != -1:
        out.append(f"Attempts:   {data['allowed_attempts']}")

    out.append(f"Status:     {submission_status(submission, due=data.get('due_at'))}")
    if submission.get("submitted_at"):
        out.append(f"Submitted:  {format_due(submission['submitted_at'], tz)}")
    if submission.get("score") is not None:
        out.append(f"Score:      {submission['score']} / {data.get('points_possible')}")

    stats = data.get("score_statistics") or {}
    if stats.get("mean") is not None:
        out.append(f"Class mean: {stats['mean']} (min {stats.get('min')}, max {stats.get('max')})")

    url = _abs_url(client, data.get("html_url"))
    if url:
        out.append(f"Link:       {url}")

    description = html_to_text(data.get("description"), limit=6000)
    if description:
        out.extend(["", "Instructions", "------------", description])

    rubric = data.get("rubric") or []
    if rubric:
        out.extend(["", "Rubric", "------"])
        for row in rubric:
            out.append(f"- {row.get('description')} ({points_label(row.get('points'))})")
            long_desc = html_to_text(row.get("long_description"), limit=400)
            if long_desc:
                out.append(f"    {long_desc}")

    comments = submission.get("submission_comments") or []
    if comments:
        out.extend(["", "Instructor comments", "-------------------"])
        for comment in comments[-5:]:
            author = (comment.get("author") or {}).get("display_name", "someone")
            out.append(f"- {author}: {truncate(comment.get('comment', ''), 400)}")

    return "\n".join(out)


@mcp.tool()
@handle_errors
async def grades(course: str = "") -> str:
    """Current grade in every course, or a full graded-work breakdown for one course."""
    client = await _get_client()
    tz = _tz()

    if not course:
        courses = await client.courses()
        if not courses:
            return "No active courses to grade."
        out = ["Current grades:", ""]
        for c in courses:
            enrollment = (c.get("enrollments") or [{}])[0]
            score = enrollment.get("computed_current_score")
            grade = enrollment.get("computed_current_grade")
            if score is None:
                out.append(f"- {_course_label(c)}: not posted yet")
            else:
                out.append(f"- {_course_label(c)}: {_percent(score)}" + (f" ({grade})" if grade else ""))
        out.append("")
        out.append("Ask for one course by name to see the assignment-by-assignment breakdown.")
        return "\n".join(out)

    resolved = await client.resolve_course(course)
    submissions = await client.paginate(
        f"/api/v1/courses/{resolved['id']}/students/submissions",
        {
            "student_ids[]": ["self"],
            "include[]": ["assignment", "submission_comments"],
            "order": "graded_at",
        },
        limit=200,
    )
    graded = [s for s in submissions if isinstance(s, dict) and s.get("score") is not None]
    enrollment = (resolved.get("enrollments") or [{}])[0]

    out = [f"{_course_label(resolved)}"]
    score = enrollment.get("computed_current_score")
    if score is not None:
        grade_letter = enrollment.get("computed_current_grade")
        out.append(f"Current grade: {_percent(score)}" + (f" ({grade_letter})" if grade_letter else ""))
    out.append("")

    if not graded:
        return "\n".join(out + ["Nothing graded yet."])

    earned = sum(float(s.get("score") or 0) for s in graded)
    possible = sum(float((s.get("assignment") or {}).get("points_possible") or 0) for s in graded)
    if possible:
        out.append(f"Graded work so far: {earned:g}/{possible:g} ({earned / possible * 100:.1f}%)")
        out.append("")

    graded.sort(key=lambda s: parse_iso(s.get("graded_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for s in graded:
        assignment = s.get("assignment") or {}
        possible_pts = assignment.get("points_possible")
        line = f"- {assignment.get('name')}: {s.get('score'):g}"
        if possible_pts:
            line += f"/{float(possible_pts):g}"
        if s.get("late"):
            line += " (late)"
        if s.get("graded_at"):
            line += f" - graded {format_day(s['graded_at'], tz)}"
        out.append(line)
        for comment in (s.get("submission_comments") or [])[-2:]:
            author = (comment.get("author") or {}).get("display_name", "instructor")
            out.append(f"    {author}: {truncate(comment.get('comment', ''), 240)}")

    ungraded = [
        s for s in submissions
        if isinstance(s, dict) and s.get("score") is None and s.get("submitted_at")
    ]
    if ungraded:
        out.append("")
        out.append(f"Submitted, awaiting a grade ({len(ungraded)}):")
        for s in ungraded[:15]:
            out.append(f"- {(s.get('assignment') or {}).get('name')}")
    return "\n".join(out)


@mcp.tool()
@handle_errors
async def course_overview(course: str) -> str:
    """Syllabus, instructor contact, module outline, and what's due soon - for one course."""
    client = await _get_client()
    resolved = await client.resolve_course(course)
    course_id = resolved["id"]
    tz = _tz()

    detail, modules, assignments = await asyncio.gather(
        client.get_json(
            f"/api/v1/courses/{course_id}",
            {"include[]": ["syllabus_body", "teachers", "term", "course_progress"]},
        ),
        client.paginate(f"/api/v1/courses/{course_id}/modules", limit=60),
        client.paginate(
            f"/api/v1/courses/{course_id}/assignments",
            {"bucket": "upcoming", "include[]": ["submission"], "order_by": "due_at"},
            limit=10,
        ),
        return_exceptions=True,
    )
    detail = detail if isinstance(detail, dict) else resolved
    modules = modules if isinstance(modules, list) else []
    assignments = assignments if isinstance(assignments, list) else []

    out = [_course_label(detail), ""]
    term = (detail.get("term") or {}).get("name")
    if term:
        out.append(f"Term: {term}")
    teachers = detail.get("teachers") or []
    if teachers:
        out.append("Instructors: " + ", ".join(t.get("display_name", "") for t in teachers))
    out.append(f"Course id: {course_id}")

    if assignments:
        out.extend(["", "Coming up", "---------"])
        for a in assignments:
            out.append(
                f"- {a.get('name')} - due {format_due(a.get('due_at'), tz)}"
                f" - {submission_status(a.get('submission'), due=a.get('due_at'))}"
            )

    if modules:
        out.extend(["", f"Modules ({len(modules)})", "-------"])
        for module in modules[:25]:
            state = module.get("state")
            marker = {"completed": "done", "started": "in progress", "locked": "locked"}.get(state, "")
            out.append(f"- {module.get('name')} [id {module.get('id')}]" + (f" - {marker}" if marker else ""))
        out.append("Use `course_modules` for the items inside a module.")

    syllabus = html_to_text(detail.get("syllabus_body"), limit=5000)
    if syllabus:
        out.extend(["", "Syllabus", "--------", syllabus])

    return "\n".join(out)


@mcp.tool()
@handle_errors
async def course_modules(course: str, module: str = "") -> str:
    """Module structure for a course - readings, pages, and assignments in teaching order.

    Pass module (id or part of its name) to see the items inside just that one.
    """
    client = await _get_client()
    resolved = await client.resolve_course(course)
    modules = await client.paginate(
        f"/api/v1/courses/{resolved['id']}/modules", {"include[]": ["items"]}, limit=80
    )
    if not modules:
        return f"{_course_label(resolved)} has no modules (the instructor may organise things differently)."

    if module:
        needle = module.strip().lower()
        modules = [
            m for m in modules
            if str(m.get("id")) == needle or needle in str(m.get("name", "")).lower()
        ] or modules

    out = [f"{_course_label(resolved)} - modules", ""]
    for m in modules:
        state = m.get("state")
        header = f"{m.get('name')} [id {m.get('id')}]"
        if state:
            header += f" - {state}"
        if m.get("unlock_at"):
            header += f" - unlocks {format_due(m.get('unlock_at'), _tz())}"
        out.append(header)
        for item in m.get("items") or []:
            kind = str(item.get("type", "")).lower()
            title = item.get("title")
            marker = "  - "
            if kind == "assignment":
                marker += f"[assignment {item.get('content_id')}] "
            elif kind == "page":
                marker += f"[page {item.get('page_url')}] "
            elif kind == "quiz":
                marker += f"[quiz {item.get('content_id')}] "
            elif kind == "file":
                marker += f"[file {item.get('content_id')}] "
            done = (item.get("completion_requirement") or {}).get("completed")
            suffix = " (done)" if done else ""
            out.append(f"{marker}{title}{suffix}")
        out.append("")
    return "\n".join(out).rstrip()


@mcp.tool()
@handle_errors
async def get_page(course: str, page: str) -> str:
    """Read a Canvas page (lecture notes, instructions, course info) as plain text."""
    client = await _get_client()
    resolved = await client.resolve_course(course)
    slug = page.strip()
    try:
        data = await client.get_json(f"/api/v1/courses/{resolved['id']}/pages/{slug}")
    except CanvasMCPError:
        pages = await client.paginate(
            f"/api/v1/courses/{resolved['id']}/pages", {"search_term": slug}, limit=25
        )
        if not pages:
            return f'No page matching "{page}" in {_course_label(resolved)}.'
        if len(pages) > 1:
            listing = "\n".join(f"  - {p.get('title')} [{p.get('url')}]" for p in pages[:15])
            return f'"{page}" matches several pages:\n{listing}'
        data = await client.get_json(f"/api/v1/courses/{resolved['id']}/pages/{pages[0]['url']}")

    if not isinstance(data, dict):
        return f'Could not read "{page}".'
    body = html_to_text(data.get("body"), limit=12000)
    header = f"{data.get('title')} - {_course_label(resolved)}"
    updated = data.get("updated_at")
    if updated:
        header += f"\nLast updated {format_day(updated, _tz())}"
    return f"{header}\n\n{body or '(this page is empty)'}"


@mcp.tool()
@handle_errors
async def list_files(course: str, search: str = "", limit: int = 40) -> str:
    """List files posted in a course (slides, readings, templates). Optional search term."""
    client = await _get_client()
    resolved = await client.resolve_course(course)
    params: dict[str, Any] = {"sort": "updated_at", "order": "desc"}
    if search:
        params["search_term"] = search
    try:
        files = await client.paginate(
            f"/api/v1/courses/{resolved['id']}/files", params, limit=max(1, min(limit, 100))
        )
    except CanvasMCPError as exc:
        return f"Could not list files for {_course_label(resolved)}: {exc}"
    if not files:
        qualifier = f" matching '{search}'" if search else ""
        return f"No files{qualifier} in {_course_label(resolved)}."

    out = [f"{_course_label(resolved)} - {len(files)} file{'s' if len(files) != 1 else ''}", ""]
    for f in files:
        size = f.get("size") or 0
        size_label = f"{size / 1_048_576:.1f} MB" if size > 1_048_576 else f"{max(size, 0) // 1024} KB"
        out.append(
            f"- {f.get('display_name')} [file {f.get('id')}] - {size_label}"
            f" - updated {format_day(f.get('updated_at'), _tz())}"
        )
    out.append("")
    out.append("Use `download_file` with the file id to save one locally.")
    return "\n".join(out)


@mcp.tool()
@handle_errors
async def download_file(file_id: str, dest_dir: str = "") -> str:
    """Download a course file to this computer so it can be read or worked with."""
    client = await _get_client()
    target_dir = Path(dest_dir).expanduser() if dest_dir else Path.home() / "Downloads" / "canvas"
    target, _info = await _fetch_file(client, file_id, target_dir)
    size_kb = target.stat().st_size / 1024
    suffix = target.suffix.lower()
    hint = ""
    if suffix in documents.supported_suffixes():
        hint = "\nUse `read_file` on the same id to read it as text."
    return f"Saved {target} ({size_kb:.0f} KB).{hint}"


@mcp.tool()
@handle_errors
async def announcements(course: str = "", days: int = 21) -> str:
    """Recent announcements from instructors - deadline changes usually land here first."""
    client = await _get_client()
    tz = _tz()
    days = max(1, min(int(days), 180))

    if course:
        resolved = await client.resolve_course(course)
        context_codes = [f"course_{resolved['id']}"]
        courses = {str(resolved["id"]): resolved}
    else:
        courses = await queries.courses_by_id(client)
        context_codes = [f"course_{cid}" for cid in courses]
    if not context_codes:
        return "No courses to read announcements from."

    start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    end = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Canvas rejects more than ten context codes per call, and plenty of students
    # are enrolled in more than ten things.
    batches = await asyncio.gather(
        *(
            client.paginate(
                "/api/v1/announcements",
                {
                    "context_codes[]": list(batch),
                    "start_date": start,
                    "end_date": end,
                    "active_only": "true",
                },
                limit=60,
            )
            for batch in chunked(context_codes, 10)
        ),
        return_exceptions=True,
    )
    posts = [p for batch in batches if isinstance(batch, list) for p in batch if isinstance(p, dict)]
    if not posts:
        return f"No announcements in the last {days} days."

    posts.sort(key=lambda p: parse_iso(p.get("posted_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    out = [f"{len(posts)} announcement{'s' if len(posts) != 1 else ''} in the last {days} days:", ""]
    for post in posts:
        context = str(post.get("context_code", "")).replace("course_", "")
        label = _short_label(courses.get(context))
        out.append(f"[{label}] {post.get('title')} - {format_day(post.get('posted_at'), tz)}")
        body = html_to_text(post.get("message"), limit=900)
        if body:
            out.append(f"  {body.replace(chr(10), chr(10) + '  ')}")
        out.append("")
    return "\n".join(out).rstrip()


@mcp.tool()
@handle_errors
async def discussions(course: str, topic: str = "", limit: int = 25) -> str:
    """List discussion topics in a course, or read one topic with its replies."""
    client = await _get_client()
    resolved = await client.resolve_course(course)
    tz = _tz()

    if not topic:
        topics = await client.paginate(
            f"/api/v1/courses/{resolved['id']}/discussion_topics",
            {"order_by": "recent_activity"},
            limit=max(1, min(limit, 60)),
        )
        if not topics:
            return f"No discussions in {_course_label(resolved)}."
        out = [f"{_course_label(resolved)} - discussions", ""]
        for t in topics:
            unread = t.get("unread_count") or 0
            due = (t.get("assignment") or {}).get("due_at") or t.get("todo_date")
            line = f"- {t.get('title')} [topic {t.get('id')}] - {t.get('discussion_subentry_count', 0)} replies"
            if unread:
                line += f", {unread} unread"
            if due:
                line += f" - due {format_due(due, tz)}"
            out.append(line)
        return "\n".join(out)

    topic_id = topic.strip()
    if not topic_id.isdigit():
        topics = await client.paginate(
            f"/api/v1/courses/{resolved['id']}/discussion_topics", {"search_term": topic_id}, limit=20
        )
        if not topics:
            return f'No discussion matching "{topic}".'
        if len(topics) > 1:
            listing = "\n".join(f"  - {t.get('title')} [topic {t.get('id')}]" for t in topics[:12])
            return f'"{topic}" matches several discussions:\n{listing}'
        topic_id = str(topics[0]["id"])

    detail = await client.get_json(f"/api/v1/courses/{resolved['id']}/discussion_topics/{topic_id}")
    if not isinstance(detail, dict):
        return f"Could not read discussion {topic_id}."
    out = [f"{detail.get('title')} - {_course_label(resolved)}", ""]
    if detail.get("posted_at"):
        out.append(f"Posted {format_day(detail['posted_at'], tz)}")
    assignment = detail.get("assignment") or {}
    if assignment.get("due_at"):
        out.append(f"Due {format_due(assignment['due_at'], tz)} ({points_label(assignment.get('points_possible'))})")
    if detail.get("require_initial_post"):
        out.append("You must post before you can see classmates' replies.")
    out.append("")
    out.append(html_to_text(detail.get("message"), limit=5000) or "(no prompt text)")

    try:
        view = await client.get_json(f"/api/v1/courses/{resolved['id']}/discussion_topics/{topic_id}/view")
    except CanvasMCPError:
        view = None
    entries = (view or {}).get("view") or []
    if entries:
        participants = {str(p.get("id")): p.get("display_name", "someone") for p in (view or {}).get("participants", [])}
        out.extend(["", f"Replies ({len(entries)})", "-------"])
        for entry in entries[:20]:
            who = participants.get(str(entry.get("user_id")), "someone")
            when = format_day(entry.get("created_at"), tz)
            out.append(f"- {who} ({when}): {truncate(html_to_text(entry.get('message')), 500)}")
    return "\n".join(out)


@mcp.tool()
@handle_errors
async def list_quizzes(course: str) -> str:
    """Quizzes and exams in a course: when they open and close, time limits, attempts."""
    client = await _get_client()
    resolved = await client.resolve_course(course)
    tz = _tz()
    try:
        quizzes = await client.paginate(f"/api/v1/courses/{resolved['id']}/quizzes", limit=100)
    except CanvasMCPError as exc:
        return f"Could not list quizzes for {_course_label(resolved)}: {exc}"
    if not quizzes:
        return f"No quizzes in {_course_label(resolved)}."

    out = [f"{_course_label(resolved)} - {len(quizzes)} quiz{'zes' if len(quizzes) != 1 else ''}", ""]
    for q in quizzes:
        bits = [f"due {format_due(q.get('due_at'), tz)}"]
        if q.get("time_limit"):
            bits.append(f"{q['time_limit']} min limit")
        if q.get("allowed_attempts") and q["allowed_attempts"] != 1:
            attempts = "unlimited" if q["allowed_attempts"] == -1 else q["allowed_attempts"]
            bits.append(f"{attempts} attempts")
        if q.get("points_possible"):
            bits.append(points_label(q["points_possible"]))
        if q.get("unlock_at"):
            bits.append(f"opens {format_due(q['unlock_at'], tz)}")
        if q.get("lock_at"):
            bits.append(f"closes {format_due(q['lock_at'], tz)}")
        out.append(f"- {q.get('title')} [quiz {q.get('id')}]\n    {' | '.join(bits)}")
    return "\n".join(out)


@mcp.tool()
@handle_errors
async def todo_list() -> str:
    """Canvas's own to-do list plus any personal notes the student has added to the planner."""
    client = await _get_client()
    tz = _tz()
    courses = await queries.courses_by_id(client)

    todos, notes = await asyncio.gather(
        client.paginate("/api/v1/users/self/todo", limit=60),
        client.paginate(
            "/api/v1/planner_notes",
            {
                "start_date": (datetime.now(timezone.utc) - timedelta(days=7)).isoformat().replace("+00:00", "Z"),
            },
            limit=60,
        ),
        return_exceptions=True,
    )
    todos = [t for t in todos if isinstance(t, dict)] if isinstance(todos, list) else []
    notes = [n for n in notes if isinstance(n, dict)] if isinstance(notes, list) else []

    out: list[str] = []
    if todos:
        out.append(f"Canvas to-do ({len(todos)}):")
        for todo in todos:
            assignment = todo.get("assignment") or {}
            course = courses.get(str(todo.get("course_id") or assignment.get("course_id") or ""))
            out.append(
                f"- [{_short_label(course)}] {assignment.get('name') or todo.get('type')}"
                f" - due {format_due(assignment.get('due_at'), tz)}"
                + (f" [assignment {assignment.get('id')}]" if assignment.get("id") else "")
            )
    else:
        out.append("Canvas's to-do list is empty.")

    if notes:
        out.append("")
        out.append(f"Your own planner notes ({len(notes)}):")
        for note in notes:
            course = courses.get(str(note.get("course_id") or ""))
            out.append(
                f"- [{_short_label(course)}] {note.get('title')}"
                f" - {format_due(note.get('todo_date'), tz)} [note {note.get('id')}]"
            )
            if note.get("details"):
                out.append(f"    {truncate(note['details'], 200)}")
    return "\n".join(out)


@mcp.tool()
@handle_errors
async def add_todo(title: str, when: str, course: str = "", details: str = "") -> str:
    """Add a personal to-do to the Canvas planner (e.g. "start the lab report").

    when accepts YYYY-MM-DD, "YYYY-MM-DD HH:MM", today, or tomorrow. Pass course to
    file the note under a class. These notes appear in the student's real Canvas
    planner, so they show up on their phone too.
    """
    _require_writes()
    client = await _get_client()
    target = _parse_when(when)

    payload: dict[str, Any] = {
        "title": title.strip(),
        "todo_date": target.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if details:
        payload["details"] = details
    label = ""
    if course:
        resolved = await client.resolve_course(course)
        payload["course_id"] = resolved["id"]
        label = f" under {_short_label(resolved)}"

    created = await client.post("/api/v1/planner_notes", payload)
    note_id = (created or {}).get("id")
    return (
        f'Added "{title}"{label} to your Canvas planner for {format_due(payload["todo_date"], _tz())}'
        f"{f' [note {note_id}]' if note_id else ''}."
    )


@mcp.tool()
@handle_errors
async def delete_todo(note_id: str) -> str:
    """Delete a personal planner note by its id (from `todo_list`)."""
    _require_writes()
    client = await _get_client()
    await client.delete(f"/api/v1/planner_notes/{note_id}")
    return f"Deleted planner note {note_id}."


@mcp.tool()
@handle_errors
async def mark_done(item_type: str, item_id: str, done: bool = True) -> str:
    """Tick an item off in the Canvas planner without submitting anything.

    item_type is one of: assignment, quiz, discussion_topic, wiki_page, planner_note,
    calendar_event. The ids come from `upcoming` (shown in square brackets).
    """
    _require_writes()
    client = await _get_client()
    kind = item_type.strip().lower()
    allowed = {
        "assignment", "quiz", "discussion_topic", "wiki_page",
        "planner_note", "calendar_event", "assessment_request",
    }
    if kind not in allowed:
        return f"item_type must be one of: {', '.join(sorted(allowed))}"

    overrides = await client.paginate("/api/v1/planner/overrides", limit=200)
    existing = next(
        (
            o for o in overrides
            if isinstance(o, dict)
            and str(o.get("plannable_id")) == str(item_id)
            and str(o.get("plannable_type")) == kind
        ),
        None,
    )
    if existing:
        await client.put(f"/api/v1/planner/overrides/{existing['id']}", {"marked_complete": str(done).lower()})
    else:
        await client.post(
            "/api/v1/planner/overrides",
            {"plannable_type": kind, "plannable_id": str(item_id), "marked_complete": str(done).lower()},
        )
    return f"{kind.replace('_', ' ')} {item_id} marked {'done' if done else 'not done'} in your planner."


# --------------------------------------------------------------------------- #
# Grade arithmetic
# --------------------------------------------------------------------------- #

async def _standing_for(client: CanvasClient, course: dict[str, Any]):
    """Fetch a course's assignment groups and reduce them to a grade standing."""
    course_id = course["id"]
    detail, groups = await queries.gather(
        client.get_json(
            f"/api/v1/courses/{course_id}",
            {"include[]": ["total_scores", "term"]},
        ),
        queries.assignment_groups(client, course_id),
    )
    detail = detail if isinstance(detail, dict) else course
    if not groups:
        raise CanvasMCPError(
            f"Canvas would not show me the assignment groups for {_course_label(course)}, "
            "so I cannot do the grade maths for it."
        )
    weighted = bool(detail.get("apply_assignment_group_weights"))
    standing = gradecalc.summarize(groups, weighted=weighted)
    scheme = await queries.grading_scheme(client, detail)
    return standing, scheme, detail, groups


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


@mcp.tool()
@handle_errors
async def grade_forecast(course: str, target: str = "") -> str:
    """What this course's grade really is, and what you need on what's left.

    Answers "can I still get a B?" properly - using the assignment group weights, so
    a 10-point quiz in a 5%-weighted group is treated as the rounding error it is.
    target accepts a letter ("B") or a percentage ("85"); omit it to see what each
    grade above your current one would take.
    """
    client = await _get_client()
    resolved = await client.resolve_course(course)
    standing, scheme, detail, _groups = await _standing_for(client, resolved)

    current = standing.current
    out = [f"{_course_label(detail)} - grade forecast", ""]
    out.append(f"Current grade:    {_pct(current)} ({gradecalc.letter_for(current, scheme)})")
    out.append(
        f"Graded so far:    {standing.earned:g} / {standing.graded_possible:g} points"
        f" across {sum(g.graded_count for g in standing.groups)} assignments"
    )
    out.append(
        f"Still to come:    {standing.remaining:g} points"
        f" across {sum(g.remaining_count for g in standing.groups)} assignments"
    )
    out.append(f"Grading:          {'weighted by group' if standing.weighted else 'straight points'}")

    if standing.remaining > 0:
        floor = standing.projected(0.0)
        ceiling = standing.projected(1.0)
        out.append("")
        out.append(f"If you do nothing else:  {_pct(floor)} ({gradecalc.letter_for(floor, scheme)})")
        out.append(f"If you ace everything:   {_pct(ceiling)} ({gradecalc.letter_for(ceiling, scheme)})")

    if standing.weighted:
        out.extend(["", "Where the weight sits", "---------------------"])
        for group in sorted(standing.groups, key=lambda g: -g.weight):
            if group.total_possible <= 0 and group.weight <= 0:
                continue
            piece = f"- {group.name}: {group.weight:g}% of the grade"
            if group.percentage is not None:
                piece += f" - you're at {_pct(group.percentage)}"
            else:
                piece += " - nothing graded yet"
            if group.remaining > 0:
                piece += f", {group.remaining:g} points left"
            out.append(piece)

    out.extend(["", "What you'd need on everything remaining", "---------------------------------------"])
    if standing.remaining <= 0:
        out.append("Nothing left to submit - this grade is final.")
        return "\n".join(out)

    targets = _forecast_targets(target, current, scheme)
    if not targets:
        out.append(f'I could not read "{target}" as a letter grade or a percentage.')
        return "\n".join(out)

    for label, cutoff in targets:
        needed = standing.needed_for(cutoff)
        if needed is None:
            out.append(f"- {label} ({cutoff * 100:g}%): can't be computed")
        elif needed <= 0:
            out.append(f"- {label} ({cutoff * 100:g}%): already locked in, even at zero from here")
        elif needed > 1.0:
            short = (cutoff - (standing.projected(1.0) or 0)) * 100
            out.append(
                f"- {label} ({cutoff * 100:g}%): out of reach - {short:.1f} points of grade short"
                " even with perfect scores"
            )
        else:
            out.append(f"- {label} ({cutoff * 100:g}%): average {_pct(needed)} on the remaining work")

    out.append("")
    out.append("That assumes every remaining assignment is graded and counts. Use `what_if` to test a single score.")
    return "\n".join(out)


def _forecast_targets(
    target: str, current: float | None, scheme: list[tuple[str, float]]
) -> list[tuple[str, float]]:
    """Either the grade the student asked about, or every grade above where they are."""
    text = (target or "").strip()
    if text:
        for name, cutoff in scheme:
            if name.lower() == text.lower():
                return [(name, cutoff)]
        cleaned = text.rstrip("%")
        try:
            value = float(cleaned)
        except ValueError:
            return []
        fraction = value / 100 if value > 1 else value
        return [(f"{fraction * 100:g}%", fraction)]

    reachable = [
        (name, cutoff) for name, cutoff in scheme
        if cutoff > 0 and (current is None or cutoff > current)
    ]
    reachable.sort(key=lambda pair: pair[1])
    return reachable[:4] or [(scheme[0][0], scheme[0][1])]


@mcp.tool()
@handle_errors
async def what_if(course: str, assignment: str, score: float) -> str:
    """Try a hypothetical score on one assignment and see the grade move.

    Useful before deciding how much to sweat something: "if I get 30/50 on this,
    where do I land?"
    """
    client = await _get_client()
    resolved = await client.resolve_course(course)
    standing, scheme, detail, groups = await _standing_for(client, resolved)

    # The groups payload already carries every assignment, so look there first:
    # it saves a round trip and still works when the detail endpoint is restricted.
    group_id, target = _find_in_groups(groups, assignment)
    if target is None:
        target = await _resolve_assignment(client, resolved["id"], assignment)
        group_id = str(target.get("assignment_group_id") or "") or next(
            (str(g.get("id")) for g in groups if _group_holds(g, target)), ""
        )
    points = float(target.get("points_possible") or 0)
    if not group_id or not any(g.id == group_id for g in standing.groups):
        return f"I couldn't work out which assignment group \"{target.get('name')}\" belongs to."

    before = standing.current
    after_standing = gradecalc.with_hypothetical(standing, group_id, points, float(score))
    after = after_standing.current
    impact = standing.impact(group_id, points)

    out = [f"{target.get('name')} - {_course_label(detail)}", ""]
    out.append(f"Scoring {score:g} out of {points:g}:")
    out.append(f"  grade now:   {_pct(before)} ({gradecalc.letter_for(before, scheme)})")
    out.append(f"  grade after: {_pct(after)} ({gradecalc.letter_for(after, scheme)})")
    if before is not None and after is not None:
        delta = (after - before) * 100
        out.append(f"  change:      {delta:+.2f} points of final grade")
    if impact is not None:
        out.append("")
        out.append(f"This assignment is worth {impact * 100:.1f}% of your final grade in this course.")
    return "\n".join(out)


def _find_in_groups(
    groups: list[dict[str, Any]], reference: str
) -> tuple[str, dict[str, Any] | None]:
    """Locate an assignment inside the assignment_groups payload by id or name."""
    ref = str(reference).strip().lower()
    pairs = [
        (str(group.get("id")), a)
        for group in groups
        for a in group.get("assignments") or []
        if isinstance(a, dict)
    ]
    for group_id, a in pairs:
        if str(a.get("id")).lower() == ref or str(a.get("name", "")).lower() == ref:
            return group_id, a

    partial = [(gid, a) for gid, a in pairs if ref and ref in str(a.get("name", "")).lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        options = "\n".join(f"  - {a.get('name')} (id {a.get('id')})" for _gid, a in partial[:12])
        raise CanvasMCPError(f'"{reference}" matches several assignments:\n{options}')
    return "", None


def _group_holds(group: dict[str, Any], assignment: dict[str, Any]) -> bool:
    return any(
        str(a.get("id")) == str(assignment.get("id"))
        for a in group.get("assignments") or []
        if isinstance(a, dict)
    )


@mcp.tool()
@handle_errors
async def triage() -> str:
    """Rank every piece of missing work by how much it actually costs your grade.

    Points alone are misleading - a 100-point assignment in a lightly weighted group
    can matter less than a 20-point one that isn't. This sorts by real grade impact
    and says which ones Canvas will still accept.
    """
    client = await _get_client()
    now = datetime.now(timezone.utc)
    courses, missing = await queries.gather(
        queries.courses_by_id(client), queries.missing_submissions(client)
    )
    courses = courses or {}
    missing = missing or []
    if not missing:
        return "Nothing is missing. Everything past due has been submitted or excused."

    course_ids = {str(m.get("course_id")) for m in missing if m.get("course_id")}
    standings: dict[str, Any] = {}
    for course_id in course_ids:
        course = courses.get(course_id)
        if not course:
            continue
        try:
            standings[course_id] = await _standing_for(client, course)
        except CanvasMCPError:
            continue

    rows: list[tuple[float, str]] = []
    for item in missing:
        course_id = str(item.get("course_id") or "")
        course = courses.get(course_id)
        points = float(item.get("points_possible") or 0)
        lock_at = parse_iso(item.get("lock_at"))
        open_still = lock_at is None or lock_at > now

        impact = None
        entry = standings.get(course_id)
        if entry:
            standing, _scheme, _detail, groups = entry
            group_id = next(
                (str(g.get("id")) for g in groups if _group_holds(g, item)), ""
            )
            if group_id:
                impact = standing.impact(group_id, points)

        cost = (impact or 0) * 100
        label = (
            f"- [{_short_label(course)}] {item.get('name')} - {points_label(points) or 'ungraded'}"
        )
        if impact is not None:
            label += f" - worth {cost:.1f}% of the course grade"
        label += (
            f" - {'still open' + (f', closes {humanize_delta(lock_at)}' if lock_at else '') if open_still else 'CLOSED'}"
            f" [assignment {item.get('id')}]"
        )
        # Work that can still be handed in outranks work that cannot, whatever it's worth.
        rows.append(((1000 if open_still else 0) + cost, label))

    rows.sort(key=lambda row: -row[0])
    out = [f"{len(missing)} missing assignments, ranked by what they cost you:", ""]
    out.extend(label for _score, label in rows)

    out.append("")
    out.append("Where each course lands if none of this gets done:")
    for entry in standings.values():
        standing, scheme, detail, _groups = entry
        floor = standing.projected(0.0)
        ceiling = standing.projected(1.0)
        out.append(
            f"- {_course_label(detail)}: now {_pct(standing.current)}"
            f" | do nothing more {_pct(floor)} ({gradecalc.letter_for(floor, scheme)})"
            f" | finish everything {_pct(ceiling)} ({gradecalc.letter_for(ceiling, scheme)})"
        )
    return "\n".join(out)


@mcp.tool()
@handle_errors
async def crunch_check(days: int = 21) -> str:
    """Find the weeks where deadlines pile up, while there's still time to start early."""
    days = max(1, min(int(days), 120))
    client = await _get_client()
    tz = _tz()
    courses, items = await queries.gather(
        queries.courses_by_id(client), queries.planner_items(client, days=days)
    )
    pending = queries.open_items(items or [])
    clusters = queries.find_crunches(pending)
    if not clusters:
        return f"No pile-ups in the next {days} days - the workload is fairly even."

    out = [f"Busy stretches in the next {days} days:", ""]
    for cluster in clusters:
        when = format_day(cluster["start"].isoformat(), tz)
        out.append(f"{when} onwards - {len(cluster['items'])} items, {cluster['points']:g} points")
        for item in cluster["items"]:
            course = (courses or {}).get(str(item.get("course_id") or ""))
            out.append(
                f"  - [{_short_label(course)}] {queries.item_title(item)}"
                f" - {format_due(item.get('plannable_date'), tz)}"
            )
        out.append("")
    out.append("Starting the biggest of these a few days early is usually the whole difference.")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Course materials
# --------------------------------------------------------------------------- #

async def _fetch_file(client: CanvasClient, file_id: str, dest_dir: Path) -> tuple[Path, dict[str, Any]]:
    info = await client.get_json(f"/api/v1/files/{file_id}")
    if not isinstance(info, dict) or not info.get("url"):
        raise CanvasMCPError(f"File {file_id} has no downloadable URL (it may be locked).")

    size = int(info.get("size") or 0)
    if size > 100 * 1_048_576:
        raise CanvasMCPError(
            f"{info.get('display_name')} is {size / 1_048_576:.0f} MB - too large to pull down automatically."
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\s.\-()]+", "_", str(info.get("display_name") or f"canvas-{file_id}")).strip()
    target = dest_dir / (safe_name or f"canvas-{file_id}")
    response = await client.request("GET", info["url"])
    target.write_bytes(response.content)
    return target, info


def _materials_cache() -> Path:
    return config.config_dir() / "files"


@mcp.tool()
@handle_errors
async def read_file(file_id: str, max_chars: int = 20000) -> str:
    """Read a course file as text - lecture slides, a reading, a handout.

    Downloads it and extracts the text, so the content can actually be discussed
    rather than just located. Handles PDF, Word, PowerPoint, HTML, CSV and plain text.
    """
    client = await _get_client()
    path, info = await _fetch_file(client, file_id, _materials_cache())
    text = documents.extract_text(path, limit=max(500, min(int(max_chars), 200_000)))
    header = f"{info.get('display_name')} ({path.suffix.lstrip('.') or 'file'}, saved to {path})"
    return f"{header}\n{'-' * len(header)}\n\n{text}"


@mcp.tool()
@handle_errors
async def read_assignment_attachments(course: str, assignment: str, max_chars: int = 15000) -> str:
    """Read the files attached to an assignment - the brief, the template, the dataset."""
    client = await _get_client()
    resolved = await client.resolve_course(course)
    data = await _resolve_assignment(client, resolved["id"], assignment)

    urls = re.findall(r"/files/(\d+)", str(data.get("description") or ""))
    unique = list(dict.fromkeys(urls))
    if not unique:
        return (
            f"No files are attached to \"{data.get('name')}\". "
            "The instructions themselves are in `get_assignment`."
        )

    budget = max(500, min(int(max_chars), 100_000))
    chunks: list[str] = []
    for file_id in unique[:5]:
        try:
            path, info = await _fetch_file(client, file_id, _materials_cache())
            text = documents.extract_text(path, limit=budget // min(len(unique), 5))
            chunks.append(f"### {info.get('display_name')}\n\n{text}")
        except CanvasMCPError as exc:
            chunks.append(f"### file {file_id}\n\n(could not read: {exc})")
    return f"Attachments on {data.get('name')}\n\n" + "\n\n".join(chunks)


# --------------------------------------------------------------------------- #
# Getting deadlines out of Canvas and in front of the student
# --------------------------------------------------------------------------- #

@mcp.tool()
@handle_errors
async def export_calendar(days: int = 120, path: str = "") -> str:
    """Write every upcoming deadline to a .ics file for Google or Apple Calendar.

    This is how deadlines reach a student who never opens Canvas: import once and
    they show up on the phone, with reminders a day and two hours before.
    """
    days = max(1, min(int(days), 365))
    client = await _get_client()
    courses, items = await queries.gather(
        queries.courses_by_id(client), queries.planner_items(client, days=days, lookback_hours=0)
    )
    pending = queries.open_items(items or [])
    if not pending:
        return f"Nothing due in the next {days} days, so there is nothing to export."

    entries = []
    for item in pending:
        when = parse_iso(item.get("plannable_date"))
        if when is None:
            continue
        course = (courses or {}).get(str(item.get("course_id") or ""))
        kind = queries.TYPE_LABEL.get(str(item.get("plannable_type")), "item")
        points = points_label((item.get("plannable") or {}).get("points_possible"))
        entries.append(
            ics.CalendarItem(
                summary=f"{_short_label(course)}: {queries.item_title(item)}",
                start=when,
                uid_seed=f"{item.get('plannable_type')}-{item.get('plannable_id')}",
                description=f"{kind}{f' worth {points}' if points else ''} - due in Canvas",
                url=_abs_url(client, item.get("html_url")),
                categories=[_short_label(course)],
            )
        )

    target = Path(path).expanduser() if path else Path.home() / "Downloads" / "canvas-deadlines.ics"
    if target.is_dir():
        target = target / "canvas-deadlines.ics"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ics.build_calendar(entries), encoding="utf-8")

    return (
        f"Wrote {len(entries)} deadlines to {target}.\n\n"
        "To use it:\n"
        "  Google Calendar - Settings > Import & export > Import, pick the file\n"
        "  Apple Calendar  - File > Import, pick the file\n"
        "  Phone           - email the file to yourself and open the attachment\n\n"
        "Each event carries reminders a day and two hours before. Re-run this to refresh it; "
        "events keep stable ids, so re-importing updates rather than duplicates."
    )


@mcp.tool()
@handle_errors
async def daily_digest(days: int = 7) -> str:
    """A short brief: what's imminent, what's missing, where the pile-ups are.

    The same text `canvas-mcp digest` prints, which can be run from cron.
    """
    client = await _get_client()
    return await digest.build_digest(client, days=max(1, min(int(days), 60)), tz=_tz())


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

@mcp.prompt()
def weekly_plan(days: int = 7) -> str:
    """Build a realistic study plan for the next stretch of days."""
    return (
        f"Look at my Canvas: call `upcoming` for {days} days and `missing_work`. Then:\n"
        "1. Tell me what is genuinely urgent versus what merely looks urgent.\n"
        "2. Flag anything already overdue that can still be turned in, and whether it has closed.\n"
        "3. Lay out a day-by-day plan for the next week with rough time estimates, front-loading\n"
        "   the heavy items and leaving the day before a deadline for review, not first drafts.\n"
        "4. For anything I clearly cannot finish in time, say so plainly and suggest what to ask\n"
        "   the instructor for.\n"
        "Do not write any of the coursework for me - help me schedule it."
    )


@mcp.prompt()
def catch_up() -> str:
    """Triage after falling behind."""
    return (
        "I have fallen behind. Call `missing_work`, then `grades` for the overall picture.\n"
        "Work out which missing assignments still accept submissions and which are worth the most\n"
        "points, and rank them by what actually moves my grade. Show me the maths (points at stake\n"
        "per class), tell me what is unrecoverable, and draft a short, plain email I could send to\n"
        "each instructor whose work I have missed - stating my situation honestly, not making\n"
        "excuses for me."
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
