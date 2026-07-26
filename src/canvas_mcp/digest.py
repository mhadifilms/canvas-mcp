"""The morning brief.

Same content whether it arrives through the MCP tool or a cron job at 7am, because
the student who most needs it is the one who won't think to ask.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import queries
from .client import CanvasClient
from .formatting import (
    format_day,
    humanize_delta,
    parse_iso,
    points_label,
    resolve_timezone,
)

LOW_GRADE_THRESHOLD = 0.70


async def build_digest(client: CanvasClient, *, days: int = 7, tz=None) -> str:
    tz = tz or resolve_timezone((await client.profile()).get("time_zone"))
    now = datetime.now(timezone.utc)

    courses, items, missing = await queries.gather(
        queries.courses_by_id(client),
        queries.planner_items(client, days=days),
        queries.missing_submissions(client),
    )
    courses = courses or {}
    pending = queries.open_items(items or [])
    missing = missing or []

    lines = [f"Canvas brief - {datetime.now(tz).strftime('%A %d %B')}", "=" * 46]

    urgent = [i for i in pending if _within(i, now, hours=48)]
    later = [i for i in pending if i not in urgent]

    if urgent:
        lines.append("")
        lines.append("NEXT 48 HOURS")
        for item in urgent:
            lines.append("  " + _line(item, courses, tz))
    else:
        lines.append("")
        lines.append("Nothing due in the next 48 hours.")

    if later:
        lines.append("")
        lines.append(f"REST OF THE NEXT {days} DAYS")
        current_day = None
        for item in later:
            day = format_day(item.get("plannable_date"), tz)
            if day != current_day:
                lines.append(f"  {day}")
                current_day = day
            lines.append("    " + _line(item, courses, tz, with_day=False))

    if missing:
        still_open = [m for m in missing if _still_open(m, now)]
        total = sum(float(m.get("points_possible") or 0) for m in missing)
        lines.append("")
        lines.append(f"MISSING ({len(missing)}, {total:g} points)")
        for item in sorted(
            missing, key=lambda m: -float(m.get("points_possible") or 0)
        )[:6]:
            course = courses.get(str(item.get("course_id") or ""))
            label = _short(course)
            due = parse_iso(item.get("due_at"))
            when = humanize_delta(due) if due else "no due date"
            closes = "" if _still_open(item, now) else "  [closed]"
            lines.append(
                f"  [{label}] {item.get('name')} - {points_label(item.get('points_possible'))}"
                f" - due {when}{closes}"
            )
        if still_open:
            lines.append(f"  {len(still_open)} of these can still be turned in.")

    crunches = queries.find_crunches(pending)
    if crunches:
        lines.append("")
        lines.append("HEADS UP")
        for cluster in crunches[:3]:
            when = format_day(cluster["start"].isoformat(), tz)
            count = len(cluster["items"])
            lines.append(
                f"  {count} thing{'s' if count != 1 else ''} due around {when}"
                f" ({cluster['points']:g} points) - start early."
            )

    struggling = [
        c for c in courses.values()
        if _score(c) is not None and _score(c) < LOW_GRADE_THRESHOLD * 100
    ]
    if struggling:
        lines.append("")
        lines.append("GRADES WORTH A LOOK")
        for course in struggling:
            lines.append(f"  {course.get('name')}: {_score(course):g}%")

    return "\n".join(lines)


def _within(item: dict[str, Any], now: datetime, *, hours: int) -> bool:
    when = parse_iso(item.get("plannable_date"))
    return when is not None and when <= now + timedelta(hours=hours)


def _still_open(assignment: dict[str, Any], now: datetime) -> bool:
    lock_at = parse_iso(assignment.get("lock_at"))
    return lock_at is None or lock_at > now


def _short(course: dict[str, Any] | None) -> str:
    if not course:
        return "Personal"
    return (course.get("course_code") or course.get("name") or "").strip() or "course"


def _score(course: dict[str, Any]) -> float | None:
    enrollment = (course.get("enrollments") or [{}])[0]
    value = enrollment.get("computed_current_score")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _line(item: dict[str, Any], courses: dict[str, Any], tz, *, with_day: bool = True) -> str:
    when = parse_iso(item.get("plannable_date"))
    course = courses.get(str(item.get("course_id") or ""))
    kind = queries.TYPE_LABEL.get(str(item.get("plannable_type")), "item")
    points = points_label((item.get("plannable") or {}).get("points_possible"))

    stamp = ""
    if when:
        stamp = format_day(item.get("plannable_date"), tz) + ", " if with_day else ""
        stamp += when.astimezone(tz).strftime("%H:%M")
    parts = [f"[{_short(course)}]", queries.item_title(item), f"({kind}{', ' + points if points else ''})"]
    if stamp:
        parts.append(f"- {stamp}")
    if when:
        parts.append(f"({humanize_delta(when)})")
    return " ".join(parts)
