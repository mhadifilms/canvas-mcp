import httpx
import pytest

from canvas_mcp.client import CanvasClient, Credentials
from canvas_mcp.errors import AmbiguousCourseError, AuthError, NotFoundError, PermissionError_

from fake_canvas import HOST, FakeCanvas


def make_client(fake: FakeCanvas) -> CanvasClient:
    creds = Credentials(
        base_url=HOST,
        cookies={"canvas_session": "abc123", "_csrf_token": "tok%3D%3D"},
        source="test",
    )
    return CanvasClient(creds, transport=fake.transport())


async def test_paginate_follows_link_header():
    fake = FakeCanvas()
    async with make_client(fake) as client:
        courses = await client.courses()
    assert [c["course_code"] for c in courses] == ["BIO 101", "ENG 110", "BIO 101L"]
    assert any("page=2" in str(r.url) for r in fake.requests)


async def test_string_ids_header_is_sent():
    fake = FakeCanvas()
    async with make_client(fake) as client:
        await client.profile()
    assert "canvas-string-ids" in fake.requests[0].headers["Accept"]


async def test_session_cookie_is_replayed():
    fake = FakeCanvas()
    async with make_client(fake) as client:
        await client.profile()
    assert "canvas_session=abc123" in fake.requests[0].headers["Cookie"]


async def test_401_becomes_a_friendly_auth_error():
    fake = FakeCanvas(unauthenticated=True)
    async with make_client(fake) as client:
        with pytest.raises(AuthError) as excinfo:
            await client.profile()
    assert "expired" in str(excinfo.value)
    assert "canvas-mcp login" in excinfo.value.full_message()


async def test_html_login_page_is_treated_as_logged_out():
    """Some deployments bounce API calls to an HTML login page instead of 401ing."""
    fake = FakeCanvas(html_login=True)
    async with make_client(fake) as client:
        with pytest.raises(AuthError):
            await client.profile()


async def test_404_and_403_are_distinguished():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gone"):
            return httpx.Response(404, json={})
        return httpx.Response(403, json={"status": "unauthorized"})

    creds = Credentials(base_url=HOST, cookies={"canvas_session": "x"})
    async with CanvasClient(creds, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(NotFoundError):
            await client.get_json("/api/v1/gone")
        with pytest.raises(PermissionError_):
            await client.get_json("/api/v1/secret")


async def test_rate_limit_is_retried_then_surfaced():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(403, headers={"X-Rate-Limit-Remaining": "0"}, text="Rate Limit Exceeded")
        return httpx.Response(200, json={"id": "7"})

    creds = Credentials(base_url=HOST, cookies={"canvas_session": "x"})
    async with CanvasClient(creds, transport=httpx.MockTransport(handler)) as client:
        assert await client.get_json("/api/v1/users/self") == {"id": "7"}
    assert calls["n"] == 3


async def test_resolve_course_by_id_code_and_fuzzy_name():
    fake = FakeCanvas()
    async with make_client(fake) as client:
        assert (await client.resolve_course("101"))["course_code"] == "BIO 101"
        assert (await client.resolve_course("ENG 110"))["id"] == "202"
        assert (await client.resolve_course("eng110"))["id"] == "202"
        assert (await client.resolve_course("writing"))["id"] == "202"


async def test_resolve_course_reports_ambiguity_with_options():
    fake = FakeCanvas()
    async with make_client(fake) as client:
        with pytest.raises(AmbiguousCourseError) as excinfo:
            await client.resolve_course("biology")
    message = str(excinfo.value)
    assert "Introduction to Biology" in message and "Biology Lab" in message


async def test_resolve_course_unknown_lists_what_exists():
    fake = FakeCanvas()
    async with make_client(fake) as client:
        with pytest.raises(NotFoundError) as excinfo:
            await client.resolve_course("astrophysics")
    assert "Your courses" in str(excinfo.value)


async def test_write_requests_send_the_csrf_header():
    fake = FakeCanvas()
    async with make_client(fake) as client:
        created = await client.post("/api/v1/planner_notes", {"title": "read chapter 4"})
    assert created["id"] == "555"
    post = next(r for r in fake.requests if r.method == "POST")
    assert post.headers["X-CSRF-Token"] == "tok=="  # url-decoded from the cookie


async def test_cookies_are_scoped_to_the_canvas_host():
    """A redirect to a school SSO domain must not carry the Canvas session."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "school.instructure.com":
            return httpx.Response(302, headers={"Location": "https://sso.school.edu/login"})
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text="login")

    creds = Credentials(base_url=HOST, cookies={"canvas_session": "secret"})
    async with CanvasClient(creds, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AuthError):
            await client.get_json("/api/v1/users/self")

    off_host = [r for r in seen if r.url.host == "sso.school.edu"]
    assert off_host, "expected the redirect to be followed"
    assert "secret" not in off_host[0].headers.get("Cookie", "")


async def test_transport_error_is_explained_not_raised_raw():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    creds = Credentials(base_url=HOST, cookies={"canvas_session": "x"})
    async with CanvasClient(creds, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(Exception) as excinfo:
            await client.get_json("/api/v1/users/self")
    assert "Could not reach" in str(excinfo.value)


async def test_resolve_course_does_not_refetch_when_the_active_list_matches():
    fake = FakeCanvas()
    async with make_client(fake) as client:
        await client.resolve_course("ENG 110")
    course_calls = [r for r in fake.requests if r.url.path == "/api/v1/courses"]
    # Two pages of the active list, and no second pass for concluded courses.
    assert len(course_calls) == 2
    assert not any("state%5B%5D" in str(r.url) or "state[]" in str(r.url) for r in course_calls)
