import time

import pytest

from canvas_mcp import server
from canvas_mcp.client import CanvasClient, Credentials

from fake_canvas import HOST, PROFILE, FakeCanvas


@pytest.fixture
def canvas(monkeypatch):
    """Wire the server's connection state to a fake Canvas."""
    fake = FakeCanvas()
    creds = Credentials(
        base_url=HOST,
        cookies={"canvas_session": "abc", "_csrf_token": "tok"},
        source="test",
    )
    client = CanvasClient(creds, transport=fake.transport())
    server._state.client = client
    server._state.profile = PROFILE
    server._state.source = "test"
    server._state.revalidate_after = time.monotonic() + 3600
    yield fake
    server._state.client = None
    server._state.revalidate_after = 0.0


async def test_status_reports_the_connection(canvas):
    out = await server.canvas_status()
    assert "Connected to https://school.instructure.com" in out
    assert "Sam Rivera" in out
    assert "no API key involved" in out
    assert "Active courses: 3" in out


async def test_list_courses_shows_ids_and_grades(canvas):
    out = await server.list_courses()
    assert "Introduction to Biology (BIO 101)" in out
    assert "id 101" in out
    assert "grade 88.4%" in out
    assert "Dr. Okafor" in out


async def test_upcoming_groups_by_day_and_hides_finished_work(canvas):
    out = await server.upcoming(days=7)
    assert "Cell Structure Lab Report" in out
    assert "Peer response thread" in out
    assert "Already handed in" not in out  # submitted items are filtered out
    assert "[assignment 1001]" in out  # ids for follow-up calls
    assert "BIO 101" in out


async def test_upcoming_can_include_finished_work(canvas):
    out = await server.upcoming(days=7, include_done=True)
    assert "Already handed in" in out


async def test_upcoming_can_be_scoped_to_one_course(canvas):
    out = await server.upcoming(days=7, course="writing")
    assert "Peer response thread" in out
    assert "Cell Structure Lab Report" not in out


async def test_missing_work_totals_the_damage(canvas):
    out = await server.missing_work()
    assert "1 missing assignment" in out
    assert "Essay 2 Draft" in out
    assert "100 points total" in out
    assert "CLOSES" in out  # still submittable, and says for how long
    assert "College Writing" in out


async def test_list_assignments_includes_status_and_score(canvas):
    out = await server.list_assignments("BIO 101")
    assert "Cell Structure Lab Report" in out
    assert "not submitted" in out
    assert "graded: 9" in out
    assert "9/10" in out


async def test_list_assignments_rejects_a_bad_bucket(canvas):
    out = await server.list_assignments("BIO 101", bucket="whenever")
    assert "bucket must be one of" in out


async def test_get_assignment_renders_instructions_and_rubric(canvas):
    out = await server.get_assignment("bio 101", "1001")
    assert "Cell Structure Lab Report" in out
    assert "Write up the cell structure lab." in out  # HTML flattened
    assert "- 3 pages" in out
    assert "File types: pdf, docx" in out
    assert "Data quality (20 pts)" in out


async def test_get_assignment_matches_on_name(canvas):
    out = await server.get_assignment("bio 101", "lab report")
    assert "Cell Structure Lab Report" in out


async def test_grades_summary_across_courses(canvas):
    out = await server.grades()
    assert "Introduction to Biology (BIO 101): 88.4%" in out
    assert "Biology Lab (BIO 101L): not posted yet" in out


async def test_grades_for_one_course_lists_feedback(canvas):
    out = await server.grades("BIO 101")
    assert "Reading Quiz 3: 9/10" in out
    assert "Dr. Okafor: Nice work." in out


async def test_course_modules_lists_items_with_handles(canvas):
    out = await server.course_modules("bio 101")
    assert "Week 1" in out
    assert "[page intro-notes] Intro notes" in out
    assert "[assignment 1001] Cell Structure Lab Report" in out


async def test_ambiguous_course_asks_instead_of_guessing(canvas):
    out = await server.list_assignments("biology")
    assert "matches several courses" in out
    assert "Biology Lab" in out


async def test_unknown_course_lists_the_real_ones(canvas):
    out = await server.list_assignments("chemistry")
    assert "No course matching" in out
    assert "Introduction to Biology" in out


async def test_add_todo_writes_to_the_planner(canvas):
    out = await server.add_todo("Start the lab report", "2026-03-12", course="BIO 101")
    assert "Added" in out and "note 555" in out
    path, body = canvas.posted[-1]
    assert path == "/api/v1/planner_notes"
    assert body["title"] == "Start the lab report"
    assert body["course_id"] == "101"
    assert body["todo_date"].startswith("2026-03-12T23:59")


async def test_add_todo_rejects_an_unparseable_date(canvas):
    out = await server.add_todo("something", "next tuesday-ish")
    assert "could not read" in out.lower()


async def test_writes_are_blocked_in_read_only_mode(canvas, monkeypatch):
    monkeypatch.setenv("CANVAS_MCP_READ_ONLY", "1")
    out = await server.add_todo("nope", "2026-03-12")
    assert "read-only" in out
    assert not canvas.posted


async def test_mark_done_validates_the_item_type(canvas):
    out = await server.mark_done("homework", "1001")
    assert "item_type must be one of" in out


async def test_auth_failure_is_explained_rather_than_raised(monkeypatch):
    from canvas_mcp.errors import AuthError

    async def broken():
        raise AuthError("session expired", hint="do the thing")

    monkeypatch.setattr(server, "_get_client", broken)
    out = await server.list_courses()
    assert "Not connected to Canvas." in out
    assert "do the thing" in out


async def test_tools_are_registered_with_the_mcp_server():
    tools = {tool.name for tool in await server.mcp.list_tools()}
    expected = {
        "canvas_status", "connect", "browser_login", "disconnect", "list_courses",
        "upcoming", "missing_work", "list_assignments", "get_assignment", "grades",
        "course_overview", "course_modules", "get_page", "list_files", "download_file",
        "announcements", "discussions", "list_quizzes", "todo_list", "add_todo",
        "delete_todo", "mark_done",
    }
    assert expected <= tools


async def test_no_tool_can_submit_coursework():
    """Deliberate boundary: this server helps plan work, it does not hand it in."""
    tools = {tool.name for tool in await server.mcp.list_tools()}
    forbidden = {"submit_assignment", "post_reply", "take_quiz", "answer_quiz", "upload_submission"}
    assert not (tools & forbidden)


async def test_announcements_batches_context_codes(monkeypatch):
    """Canvas rejects more than ten context_codes per request."""
    import httpx

    from canvas_mcp.client import CanvasClient, Credentials

    courses = [
        {"id": str(i), "name": f"Course {i}", "course_code": f"C{i}", "enrollments": []}
        for i in range(1, 13)
    ]
    seen_batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/courses":
            return httpx.Response(200, json=courses)
        if request.url.path == "/api/v1/announcements":
            codes = request.url.params.get_list("context_codes[]")
            seen_batches.append(codes)
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    creds = Credentials(base_url=HOST, cookies={"canvas_session": "x"})
    client = CanvasClient(creds, transport=httpx.MockTransport(handler))
    server._state.client = client
    server._state.profile = PROFILE
    server._state.revalidate_after = time.monotonic() + 3600
    try:
        await server.announcements()
    finally:
        await client.aclose()
        server._state.client = None
        server._state.revalidate_after = 0.0

    assert len(seen_batches) == 2
    assert all(len(batch) <= 10 for batch in seen_batches)
    assert sorted(code for batch in seen_batches for code in batch) == sorted(
        f"course_{i}" for i in range(1, 13)
    )


async def test_get_assignment_pulls_instructor_feedback_separately(canvas):
    """Comments live on the submission endpoint, not the assignment one."""
    import httpx

    from canvas_mcp.client import CanvasClient, Credentials

    assignment = {
        "id": "1001",
        "name": "Cell Structure Lab Report",
        "points_possible": 50,
        "due_at": "2099-01-01T00:00:00Z",
        "submission": {"workflow_state": "graded", "score": 44},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/courses":
            return httpx.Response(200, json=[{"id": "101", "name": "Bio", "course_code": "BIO 101"}])
        if path == "/api/v1/courses/101/assignments/1001/submissions/self":
            return httpx.Response(
                200,
                json={
                    "id": "9",
                    "workflow_state": "graded",
                    "score": 44,
                    "grade": "44",
                    "posted_at": "2026-01-01T00:00:00Z",
                    "submission_comments": [
                        {"author": {"display_name": "Dr. Okafor"}, "comment": "Tighten the analysis."}
                    ],
                },
            )
        if path == "/api/v1/courses/101/assignments/1001":
            return httpx.Response(200, json=assignment)
        return httpx.Response(404, json={})

    creds = Credentials(base_url=HOST, cookies={"canvas_session": "x"})
    client = CanvasClient(creds, transport=httpx.MockTransport(handler))
    server._state.client = client
    server._state.profile = PROFILE
    server._state.revalidate_after = time.monotonic() + 3600
    try:
        out = await server.get_assignment("BIO 101", "1001")
    finally:
        await client.aclose()
        server._state.client = None
        server._state.revalidate_after = 0.0

    assert "Dr. Okafor: Tighten the analysis." in out
    assert "Score:      44 / 50" in out


async def test_get_assignment_survives_a_missing_submission_record(canvas):
    """The fake has no /submissions/self route; the tool must not fall over."""
    out = await server.get_assignment("bio 101", "1001")
    assert "Cell Structure Lab Report" in out
    assert "Status:" in out
