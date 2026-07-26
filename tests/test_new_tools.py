"""Grade forecasting, triage, materials, calendar export and the digest."""

import time

import pytest

from canvas_mcp import digest, server
from canvas_mcp.client import CanvasClient, Credentials

from fake_canvas import HOST, PROFILE, FakeCanvas


@pytest.fixture
def canvas():
    fake = FakeCanvas()
    creds = Credentials(
        base_url=HOST, cookies={"canvas_session": "abc", "_csrf_token": "tok"}, source="test"
    )
    client = CanvasClient(creds, transport=fake.transport())
    server._state.client = client
    server._state.profile = PROFILE
    server._state.source = "test"
    server._state.revalidate_after = time.monotonic() + 3600
    yield fake
    server._state.client = None
    server._state.revalidate_after = 0.0


# --------------------------------------------------------------------------- #
# Grade forecasting
# --------------------------------------------------------------------------- #

async def test_grade_forecast_uses_group_weights(canvas):
    out = await server.grade_forecast("BIO 101")
    # Quizzes 40% (9/10 graded), lab reports 60% (nothing graded yet).
    assert "Current grade:    90.0%" in out
    assert "weighted by group" in out
    assert "Quizzes: 40% of the grade" in out
    assert "Lab reports: 60% of the grade" in out


async def test_grade_forecast_shows_both_ends_of_the_range(canvas):
    out = await server.grade_forecast("BIO 101")
    assert "If you do nothing else:  36.0%" in out
    assert "If you ace everything:   96.0%" in out


async def test_grade_forecast_answers_a_specific_letter_target(canvas):
    out = await server.grade_forecast("BIO 101", target="B")
    # A B needs 84%: (0.84 - 0.36) / 0.60 = 80% of the remaining work.
    assert "B (84%)" in out
    assert "80.0%" in out


async def test_grade_forecast_accepts_a_percentage_target(canvas):
    out = await server.grade_forecast("BIO 101", target="85%")
    assert "85%" in out


async def test_grade_forecast_flags_an_impossible_target(canvas):
    out = await server.grade_forecast("BIO 101", target="99")
    assert "out of reach" in out


async def test_grade_forecast_rejects_nonsense_targets(canvas):
    out = await server.grade_forecast("BIO 101", target="pretty good")
    assert "could not read" in out


async def test_grade_forecast_without_a_target_lists_reachable_grades(canvas):
    out = await server.grade_forecast("BIO 101")
    assert "What you'd need on everything remaining" in out
    assert "average" in out


async def test_what_if_moves_the_grade_and_reports_the_stake(canvas):
    out = await server.what_if("BIO 101", "1001", 25)
    assert "grade now:   90.0%" in out
    # Lab report at 25/50 -> 50% of a 60% group, plus 40% * 90%.
    assert "grade after: 66.0%" in out
    assert "-24.00 points of final grade" in out
    assert "worth 60.0% of your final grade" in out


async def test_what_if_with_a_perfect_score(canvas):
    out = await server.what_if("BIO 101", "Cell Structure", 50)
    assert "grade after: 96.0%" in out
    assert "+6.00" in out


async def test_triage_ranks_missing_work_by_grade_cost(canvas):
    out = await server.triage()
    assert "Essay 2 Draft" in out
    assert "worth 100.0% of the course grade" in out
    assert "still open" in out
    assert "do nothing more" in out


async def test_crunch_check_reports_quiet_weeks_honestly(canvas):
    out = await server.crunch_check(days=7)
    # The fake has two open items spread across days - not a pile-up.
    assert "No pile-ups" in out or "Busy stretches" in out


# --------------------------------------------------------------------------- #
# Course materials
# --------------------------------------------------------------------------- #

async def test_read_file_returns_the_text(canvas, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_materials_cache", lambda: tmp_path)
    out = await server.read_file("9001")
    assert "week6-slides.txt" in out
    assert "Slide 2: Staining protocol" in out


async def test_download_file_points_at_read_file_when_readable(canvas, tmp_path):
    out = await server.download_file("9001", dest_dir=str(tmp_path))
    assert "Saved" in out
    assert "read_file" in out
    assert (tmp_path / "week6-slides.txt").exists()


async def test_read_file_on_a_missing_file_explains(canvas, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_materials_cache", lambda: tmp_path)
    out = await server.read_file("404404")
    assert "404" in out or "nothing at" in out.lower()


# --------------------------------------------------------------------------- #
# Calendar export
# --------------------------------------------------------------------------- #

async def test_export_calendar_writes_an_importable_file(canvas, tmp_path):
    target = tmp_path / "deadlines.ics"
    out = await server.export_calendar(days=14, path=str(target))
    assert "Wrote 2 deadlines" in out
    assert "Google Calendar" in out

    text = target.read_text(encoding="utf-8")
    assert text.startswith("BEGIN:VCALENDAR")
    assert "BIO 101: Cell Structure Lab Report" in text
    assert "Already handed in" not in text  # submitted work is not a deadline
    assert text.count("BEGIN:VEVENT") == 2


async def test_export_calendar_defaults_to_a_sensible_filename(canvas, tmp_path):
    out = await server.export_calendar(days=14, path=str(tmp_path))
    assert "canvas-deadlines.ics" in out
    assert (tmp_path / "canvas-deadlines.ics").exists()


async def test_export_is_stable_across_runs(canvas, tmp_path):
    first = tmp_path / "a.ics"
    second = tmp_path / "b.ics"
    await server.export_calendar(days=14, path=str(first))
    await server.export_calendar(days=14, path=str(second))
    uids = lambda p: sorted(  # noqa: E731
        line for line in p.read_text().split("\r\n") if line.startswith("UID:")
    )
    assert uids(first) == uids(second)


# --------------------------------------------------------------------------- #
# The digest
# --------------------------------------------------------------------------- #

async def test_digest_leads_with_what_is_imminent(canvas):
    out = await server.daily_digest(days=7)
    assert "Canvas brief" in out
    assert "MISSING (1, 100 points)" in out
    assert "Essay 2 Draft" in out
    assert "can still be turned in" in out


async def test_digest_stays_quiet_about_grades_that_are_merely_mediocre(canvas):
    """A 74% is a C, not an emergency; the brief shouldn't cry wolf."""
    out = await server.daily_digest(days=7)
    assert "GRADES WORTH A LOOK" not in out


async def test_digest_flags_a_course_that_is_actually_failing():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/users/self":
            return httpx.Response(200, json=PROFILE)
        if path == "/api/v1/courses":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "1", "name": "Organic Chemistry", "course_code": "CHM 220",
                        "enrollments": [{"enrollment_state": "active", "computed_current_score": 55.0}],
                    },
                    {
                        "id": "2", "name": "Studio Art", "course_code": "ART 101",
                        "enrollments": [{"enrollment_state": "active", "computed_current_score": 93.0}],
                    },
                ],
            )
        return httpx.Response(200, json=[])

    creds = Credentials(base_url=HOST, cookies={"canvas_session": "x"})
    async with CanvasClient(creds, transport=httpx.MockTransport(handler)) as client:
        out = await digest.build_digest(client, days=7)

    section = out.split("GRADES WORTH A LOOK")[1]
    assert "Organic Chemistry: 55%" in section
    assert "Studio Art" not in section


async def test_digest_is_callable_without_the_mcp_layer(canvas):
    """The cron path uses the same code as the tool."""
    text = await digest.build_digest(server._state.client, days=7)
    assert "Canvas brief" in text


async def test_new_tools_are_registered():
    tools = {tool.name for tool in await server.mcp.list_tools()}
    assert {
        "grade_forecast", "what_if", "triage", "crunch_check",
        "read_file", "read_assignment_attachments", "export_calendar", "daily_digest",
    } <= tools


async def test_what_if_resolves_from_the_groups_without_a_second_fetch(canvas):
    """The assignment is already in the groups payload; don't re-fetch it."""
    await server.what_if("BIO 101", "Cell Structure Lab Report", 40)
    detail_calls = [
        r for r in canvas.requests
        if r.url.path == "/api/v1/courses/101/assignments/1001"
    ]
    assert detail_calls == []


async def test_what_if_reports_ambiguous_assignment_names(canvas):
    out = await server.what_if("BIO 101", "e", 10)
    assert "matches several assignments" in out
