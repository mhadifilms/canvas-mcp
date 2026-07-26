"""Canvas fetches and shaping shared by the MCP tools and the CLI digest.

Anything that both an interactive tool and a cron-driven brief need to know how to
ask for lives here, so the two never drift apart.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from .client import CanvasClient
from .errors import CanvasMCPError
from .formatting import parse_iso

# Planner item types, mapped to words a student would use.
TYPE_LABEL = {
    "assignment": "assignment",
    "quiz": "quiz",
    "discussion_topic": "discussion",
    "announcement": "announcement",
    "wiki_page": "page",
    "planner_note": "to-do",
    "calendar_event": "event",
    "assessment_request": "peer review",
    "sub_assignment": "assignment",
}


async def courses_by_id(client: CanvasClient, *, include_concluded: bool = False) -> dict[str, dict[str, Any]]:
    courses = await client.courses(include_concluded=include_concluded)
    return {str(c["id"]): c for c in courses}


async def planner_items(
    client: CanvasClient, *, days: int, lookback_hours: int = 12
) -> list[dict[str, Any]]:
    """The dashboard's list view: assignments, quizzes, events and planner notes."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=lookback_hours)
    end = now + timedelta(days=days)
    items = await client.paginate(
        "/api/v1/planner/items",
        {
            "start_date": start.isoformat().replace("+00:00", "Z"),
            "end_date": end.isoformat().replace("+00:00", "Z"),
        },
        limit=400,
    )
    return [i for i in items if isinstance(i, dict)]


async def missing_submissions(client: CanvasClient) -> list[dict[str, Any]]:
    items = await client.paginate(
        "/api/v1/users/self/missing_submissions",
        {"include[]": ["planner_overrides"], "filter[]": ["submittable"]},
        limit=200,
    )
    return [i for i in items if isinstance(i, dict)]


async def assignment_groups(client: CanvasClient, course_id: Any) -> list[dict[str, Any]]:
    """Assignment groups with their assignments and this student's submissions.

    This is the one call that makes real grade arithmetic possible: it carries the
    group weights, which is what turns "50 points" into "3% of your final grade".
    """
    groups = await client.paginate(
        f"/api/v1/courses/{course_id}/assignment_groups",
        {
            "include[]": ["assignments", "submission"],
            "override_assignment_dates": "false",
        },
        limit=60,
    )
    return [g for g in groups if isinstance(g, dict)]


async def grading_scheme(client: CanvasClient, course: dict[str, Any]) -> list[tuple[str, float]]:
    """The course's letter-grade cutoffs, or the conventional scale if it has none."""
    standard_id = course.get("grading_standard_id")
    if standard_id:
        try:
            data = await client.get_json(
                f"/api/v1/courses/{course['id']}/grading_standards/{standard_id}"
            )
        except CanvasMCPError:
            data = None
        entries = (data or {}).get("grading_scheme") if isinstance(data, dict) else None
        parsed = _parse_scheme(entries)
        if parsed:
            return parsed
    return DEFAULT_SCHEME


DEFAULT_SCHEME: list[tuple[str, float]] = [
    ("A", 0.94), ("A-", 0.90), ("B+", 0.87), ("B", 0.84), ("B-", 0.80),
    ("C+", 0.77), ("C", 0.74), ("C-", 0.70), ("D", 0.64), ("F", 0.0),
]


def _parse_scheme(entries: Any) -> list[tuple[str, float]]:
    if not isinstance(entries, list):
        return []
    scheme: list[tuple[str, float]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        value = entry.get("value")
        if name is None or value is None:
            continue
        try:
            scheme.append((str(name), float(value)))
        except (TypeError, ValueError):
            continue
    return sorted(scheme, key=lambda pair: pair[1], reverse=True)


def item_done(item: dict[str, Any]) -> bool:
    """Has the student dealt with this planner item, one way or another?"""
    override = item.get("planner_override") or {}
    if override.get("marked_complete") or override.get("dismissed"):
        return True
    submissions = item.get("submissions")
    if isinstance(submissions, dict):
        return bool(
            submissions.get("submitted") or submissions.get("excused") or submissions.get("graded")
        )
    return False


def item_title(item: dict[str, Any]) -> str:
    plannable = item.get("plannable") or {}
    return plannable.get("title") or plannable.get("name") or "(untitled)"


def item_points(item: dict[str, Any]) -> float:
    plannable = item.get("plannable") or {}
    try:
        return float(plannable.get("points_possible") or 0)
    except (TypeError, ValueError):
        return 0.0


def open_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Everything still outstanding, oldest deadline first."""
    pending = [
        i for i in items
        if not item_done(i) and str(i.get("plannable_type")) != "announcement"
    ]
    pending.sort(key=lambda i: parse_iso(i.get("plannable_date")) or datetime.max.replace(tzinfo=timezone.utc))
    return pending


def find_crunches(
    items: list[dict[str, Any]],
    *,
    window_hours: int = 36,
    min_items: int = 3,
    min_points: float = 100.0,
) -> list[dict[str, Any]]:
    """Spot pile-ups: several deadlines, or a lot of points, landing close together.

    Worth surfacing days ahead of time, while there is still room to start early.
    """
    dated = [
        (parse_iso(i.get("plannable_date")), i)
        for i in items
        if parse_iso(i.get("plannable_date")) is not None
    ]
    dated.sort(key=lambda pair: pair[0])

    window = timedelta(hours=window_hours)
    clusters: list[dict[str, Any]] = []
    for index, (start, _item) in enumerate(dated):
        group = [item for when, item in dated[index:] if when - start <= window]
        points = sum(item_points(i) for i in group)
        # One assignment is not a pile-up however big it is, so a cluster always
        # needs at least two things landing together.
        if len(group) < 2:
            continue
        if len(group) >= min_items or points >= min_points:
            clusters.append({"start": start, "items": group, "points": points})

    # Keep only maximal clusters - a run of overlapping windows describes one pile-up.
    maximal: list[dict[str, Any]] = []
    for cluster in clusters:
        ids = {id(i) for i in cluster["items"]}
        if any(ids <= {id(i) for i in kept["items"]} for kept in maximal):
            continue
        maximal = [k for k in maximal if not {id(i) for i in k["items"]} <= ids]
        maximal.append(cluster)
    return maximal


async def gather(*awaitables: Any) -> list[Any]:
    """asyncio.gather that yields None in place of a failure, so one dud call
    never takes a whole overview down with it."""
    results = await asyncio.gather(*awaitables, return_exceptions=True)
    return [None if isinstance(r, BaseException) else r for r in results]
