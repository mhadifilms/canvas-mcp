"""Turning Canvas's JSON and HTML into something readable in a chat window."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any, Iterable

try:  # Python 3.9+ stdlib, but the tz database can be missing on some Windows installs.
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

# Canvas hands back Rails time zone labels rather than IANA names. These are the
# ones US students actually hit; anything else falls through to the local clock.
_RAILS_TZ = {
    "Eastern Time (US & Canada)": "America/New_York",
    "Central Time (US & Canada)": "America/Chicago",
    "Mountain Time (US & Canada)": "America/Denver",
    "Arizona": "America/Phoenix",
    "Pacific Time (US & Canada)": "America/Los_Angeles",
    "Alaska": "America/Anchorage",
    "Hawaii": "Pacific/Honolulu",
    "Atlantic Time (Canada)": "America/Halifax",
    "London": "Europe/London",
    "Dublin": "Europe/Dublin",
    "Paris": "Europe/Paris",
    "Berlin": "Europe/Berlin",
    "Madrid": "Europe/Madrid",
    "Amsterdam": "Europe/Amsterdam",
    "UTC": "UTC",
}


def resolve_timezone(canvas_time_zone: str | None = None):
    """Prefer an explicit override, then the Canvas profile, then the local clock."""
    override = os.environ.get("CANVAS_MCP_TZ", "").strip()
    for candidate in (override, _RAILS_TZ.get(canvas_time_zone or "", canvas_time_zone or "")):
        if candidate and ZoneInfo is not None:
            try:
                return ZoneInfo(candidate)
            except Exception:
                continue
    return datetime.now().astimezone().tzinfo or timezone.utc


def parse_iso(value: Any) -> datetime | None:
    """Parse Canvas's ISO-8601 timestamps (always UTC, usually ``...Z``)."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def humanize_delta(target: datetime, *, reference: datetime | None = None) -> str:
    """"in 3 days" / "in 4 hours" / "2 days late" - the part students actually read."""
    reference = reference or now_utc()
    delta = target - reference
    seconds = delta.total_seconds()
    overdue = seconds < 0
    seconds = abs(seconds)

    if seconds < 90:
        phrase = "less than a minute"
    elif seconds < 3600:
        minutes = round(seconds / 60)
        phrase = f"{minutes} minute{'s' if minutes != 1 else ''}"
    elif seconds < 86400:
        # Round down throughout: telling someone a deadline is further away than it
        # is would be the one rounding error that actually costs them marks.
        hours = int(seconds // 3600)
        phrase = f"{hours} hour{'s' if hours != 1 else ''}"
    elif seconds < 86400 * 14:
        days = int(seconds // 86400)
        phrase = f"{days} day{'s' if days != 1 else ''}"
    else:
        weeks = int(seconds // (86400 * 7))
        phrase = f"{weeks} week{'s' if weeks != 1 else ''}"

    return f"{phrase} ago" if overdue else f"in {phrase}"


def format_due(value: Any, tz=None, *, reference: datetime | None = None) -> str:
    """"Fri Aug 1, 11:59 PM (in 3 days)" or "no due date"."""
    dt = parse_iso(value)
    if dt is None:
        return "no due date"
    tz = tz or resolve_timezone()
    local = dt.astimezone(tz)
    stamp = local.strftime("%a %b %-d, %-I:%M %p") if os.name != "nt" else local.strftime("%a %b %d, %I:%M %p")
    return f"{stamp} ({humanize_delta(dt, reference=reference)})"


def format_day(value: Any, tz=None) -> str:
    dt = parse_iso(value)
    if dt is None:
        return "Undated"
    tz = tz or resolve_timezone()
    local = dt.astimezone(tz)
    today = datetime.now(tz).date()
    if local.date() == today:
        return f"Today - {local.strftime('%A, %B %d')}"
    if local.date() == today + timedelta(days=1):
        return f"Tomorrow - {local.strftime('%A, %B %d')}"
    return local.strftime("%A, %B %d")


class _TextExtractor(HTMLParser):
    """Minimal HTML-to-text pass. Good enough for assignment descriptions."""

    _BLOCK = {
        "p", "div", "br", "tr", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "blockquote", "pre",
    }
    _SKIP = {"script", "style", "head", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0
        self._link: str | None = None
        self._list_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrd = {k: (v or "") for k, v in attrs}
        if tag in self._SKIP:
            self._skipping += 1
            return
        if self._skipping:
            return
        if tag in ("ul", "ol"):
            self._list_depth += 1
        if tag in self._BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("\n" + "  " * max(0, self._list_depth - 1) + "- ")
        elif tag == "a":
            self._link = attrd.get("href") or None
        elif tag == "img":
            alt = attrd.get("alt") or "image"
            self.parts.append(f"[{alt}]")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skipping = max(0, self._skipping - 1)
            return
        if self._skipping:
            return
        if tag in ("ul", "ol"):
            self._list_depth = max(0, self._list_depth - 1)
        if tag == "a" and self._link:
            # Keep the URL - students often need the actual link out of a prompt.
            if not self._link.startswith("#"):
                self.parts.append(f" ({self._link})")
            self._link = None
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skipping:
            return
        self.parts.append(data)


def html_to_text(html: Any, *, limit: int | None = None) -> str:
    if not html or not isinstance(html, str):
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Malformed markup: fall back to a blunt tag strip rather than failing a tool call.
        return unescape(re.sub(r"<[^>]+>", " ", html)).strip()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + f"\n... [truncated, {len(text) - limit} more characters]"
    return text


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def bullet_list(lines: Iterable[str]) -> str:
    items = [line for line in lines if line]
    return "\n".join(f"- {line}" for line in items)


def points_label(points: Any) -> str:
    if points is None:
        return ""
    try:
        value = float(points)
    except (TypeError, ValueError):
        return ""
    return f"{value:g} pt{'s' if value != 1 else ''}"


def submission_status(submission: dict[str, Any] | None, *, due: Any = None) -> str:
    """One short phrase covering the states a student cares about."""
    if not submission or not isinstance(submission, dict):
        due_dt = parse_iso(due)
        if due_dt and due_dt < now_utc():
            return "NOT SUBMITTED (past due)"
        return "not submitted"

    if submission.get("excused"):
        return "excused"
    if submission.get("missing"):
        return "MISSING"

    state = submission.get("workflow_state")
    submitted_at = submission.get("submitted_at")
    score = submission.get("score")
    grade = submission.get("grade")

    if state == "graded" or (score is not None and submission.get("posted_at")):
        detail = f"graded: {grade if grade is not None else score}"
        if submission.get("late"):
            detail += " (late)"
        return detail
    if submitted_at:
        return "submitted (awaiting grade)" + (" - late" if submission.get("late") else "")
    if state == "pending_review":
        return "submitted (pending review)"

    due_dt = parse_iso(due)
    if due_dt and due_dt < now_utc():
        return "NOT SUBMITTED (past due)"
    return "not submitted"
