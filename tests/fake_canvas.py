"""A small stand-in for a Canvas instance, wired up as an httpx MockTransport."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

HOST = "https://school.instructure.com"
# Anchored to the real clock so "due in two days" stays true whenever the suite runs.
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


PROFILE = {
    "id": "7",
    "name": "Sam Rivera",
    "short_name": "Sam",
    "login_id": "srivera",
    "time_zone": "America/New_York",
}

COURSES = [
    {
        "id": "101",
        "name": "Introduction to Biology",
        "course_code": "BIO 101",
        "term": {"name": "Spring 2026"},
        "teachers": [{"display_name": "Dr. Okafor"}],
        "enrollments": [
            {
                "enrollment_state": "active",
                "computed_current_score": 88.4,
                "computed_current_grade": "B+",
            }
        ],
    },
    {
        "id": "202",
        "name": "College Writing",
        "course_code": "ENG 110",
        "term": {"name": "Spring 2026"},
        "teachers": [{"display_name": "Prof. Lindqvist"}],
        "enrollments": [{"enrollment_state": "active", "computed_current_score": 74.0}],
    },
    {
        "id": "303",
        "name": "Biology Lab",
        "course_code": "BIO 101L",
        "term": {"name": "Spring 2026"},
        "enrollments": [{"enrollment_state": "active"}],
    },
]

ASSIGNMENTS = {
    "101": [
        {
            "id": "1001",
            "name": "Cell Structure Lab Report",
            "due_at": iso(NOW + timedelta(days=2)),
            "points_possible": 50,
            "html_url": f"{HOST}/courses/101/assignments/1001",
            "submission_types": ["online_upload"],
            "allowed_extensions": ["pdf", "docx"],
            "description": "<p>Write up the <strong>cell structure</strong> lab.</p><ul><li>3 pages</li></ul>",
            "rubric": [{"description": "Data quality", "points": 20}],
            "submission": {"workflow_state": "unsubmitted"},
        },
        {
            "id": "1002",
            "name": "Reading Quiz 3",
            "due_at": iso(NOW - timedelta(days=4)),
            "points_possible": 10,
            "submission": {"workflow_state": "graded", "score": 9, "grade": "9", "posted_at": iso(NOW)},
        },
    ],
    "202": [
        {
            "id": "2001",
            "name": "Essay 2 Draft",
            "due_at": iso(NOW - timedelta(days=1)),
            "points_possible": 100,
            "submission": {"workflow_state": "unsubmitted", "missing": True},
        }
    ],
    "303": [],
}

PLANNER_ITEMS = [
    {
        "course_id": "101",
        "plannable_id": "1001",
        "plannable_type": "assignment",
        "plannable_date": iso(NOW + timedelta(days=2)),
        "plannable": {"id": "1001", "title": "Cell Structure Lab Report", "points_possible": 50},
        "submissions": {"submitted": False, "missing": False, "graded": False},
        "planner_override": None,
        "html_url": "/courses/101/assignments/1001",
    },
    {
        "course_id": "202",
        "plannable_id": "2002",
        "plannable_type": "discussion_topic",
        "plannable_date": iso(NOW + timedelta(days=3)),
        "plannable": {"id": "2002", "title": "Peer response thread", "points_possible": 15},
        "submissions": {"submitted": False},
        "planner_override": None,
    },
    {
        "course_id": "101",
        "plannable_id": "1003",
        "plannable_type": "assignment",
        "plannable_date": iso(NOW + timedelta(days=1)),
        "plannable": {"id": "1003", "title": "Already handed in", "points_possible": 5},
        "submissions": {"submitted": True},
        "planner_override": None,
    },
]

MISSING = [
    {
        "id": "2001",
        "course_id": "202",
        "name": "Essay 2 Draft",
        "due_at": iso(NOW - timedelta(days=1)),
        "lock_at": iso(NOW + timedelta(days=5)),
        "points_possible": 100,
    }
]


class FakeCanvas:
    """Records what was asked for, so tests can assert on the calls too."""

    def __init__(self, *, unauthenticated: bool = False, html_login: bool = False) -> None:
        self.unauthenticated = unauthenticated
        self.html_login = html_login
        self.requests: list[httpx.Request] = []
        self.posted: list[tuple[str, dict[str, Any]]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if self.html_login:
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text="<html>Log in</html>")
        if self.unauthenticated:
            return httpx.Response(401, json={"status": "unauthenticated"})

        handler: Callable[[httpx.Request], httpx.Response] | None = None
        for prefix, fn in self._routes().items():
            if path == prefix or (prefix.endswith("*") and path.startswith(prefix[:-1])):
                handler = fn
                break
        if handler is None:
            return httpx.Response(404, json={"errors": [{"message": "not found"}]})
        return handler(request)

    def _routes(self) -> dict[str, Callable[[httpx.Request], httpx.Response]]:
        return {
            "/api/v1/users/self": lambda r: httpx.Response(200, json=PROFILE),
            "/api/v1/users/self/missing_submissions": lambda r: httpx.Response(200, json=MISSING),
            "/api/v1/users/self/todo": lambda r: httpx.Response(200, json=[]),
            "/api/v1/courses": self._courses,
            "/api/v1/planner/items": lambda r: httpx.Response(200, json=PLANNER_ITEMS),
            "/api/v1/planner_notes": self._planner_notes,
            "/api/v1/courses/*": self._course_scoped,
        }

    def _courses(self, request: httpx.Request) -> httpx.Response:
        # Page the course list to exercise Link-header following.
        page = request.url.params.get("page", "1")
        if page == "1":
            return httpx.Response(
                200,
                json=COURSES[:2],
                headers={"Link": f'<{HOST}/api/v1/courses?page=2&per_page=100>; rel="next"'},
            )
        return httpx.Response(200, json=COURSES[2:])

    def _course_scoped(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")  # api v1 courses <id> ...
        course_id = parts[3]
        rest = parts[4:]

        if not rest:
            course = next((c for c in COURSES if c["id"] == course_id), None)
            return httpx.Response(200, json=course) if course else httpx.Response(404, json={})

        if rest[0] == "assignments":
            items = ASSIGNMENTS.get(course_id, [])
            if len(rest) == 1:
                bucket = request.url.params.get("bucket")
                if bucket == "upcoming":
                    items = [a for a in items if a.get("due_at", "") > iso(NOW)]
                return httpx.Response(200, json=items)
            match = next((a for a in items if a["id"] == rest[1]), None)
            return httpx.Response(200, json=match) if match else httpx.Response(404, json={})

        if rest[0] == "students" and rest[1:2] == ["submissions"]:
            payload = [
                {
                    "score": 9,
                    "graded_at": iso(NOW),
                    "assignment": {"name": "Reading Quiz 3", "points_possible": 10},
                    "submission_comments": [
                        {"author": {"display_name": "Dr. Okafor"}, "comment": "Nice work."}
                    ],
                }
            ]
            return httpx.Response(200, json=payload)

        if rest[0] == "modules":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "9",
                        "name": "Week 1",
                        "state": "started",
                        "items": [
                            {"type": "Page", "title": "Intro notes", "page_url": "intro-notes"},
                            {"type": "Assignment", "title": "Cell Structure Lab Report", "content_id": "1001"},
                        ],
                    }
                ],
            )

        return httpx.Response(404, json={"errors": [{"message": "not found"}]})

    def _planner_notes(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = dict(httpx.QueryParams(request.content.decode()))
            self.posted.append((str(request.url.path), body))
            if not request.headers.get("X-CSRF-Token"):
                return httpx.Response(422, json={"errors": [{"message": "missing csrf"}]})
            return httpx.Response(200, json={"id": "555", **body})
        return httpx.Response(200, json=[])


def dump(obj: Any) -> str:
    return json.dumps(obj, indent=2)
