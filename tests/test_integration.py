"""End-to-end over a real socket, against a server that behaves like Canvas does.

MockTransport cannot prove that httpx actually puts the session cookie on the wire,
or that a Canvas-style redirect-to-login is recognised. This can.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from canvas_mcp import auth, server
from canvas_mcp.client import CanvasClient, Credentials
from canvas_mcp.errors import AuthError

GOOD_COOKIE = "good-session-value"

PROFILE = {"id": "7", "name": "Sam Rivera", "login_id": "srivera", "time_zone": "UTC"}
COURSES = [
    {
        "id": "101",
        "name": "Introduction to Biology",
        "course_code": "BIO 101",
        "enrollments": [{"enrollment_state": "active", "computed_current_score": 91.0}],
    }
]


class Handler(BaseHTTPRequestHandler):
    received: list[tuple[str, str, dict]] = []

    def log_message(self, *args):  # keep pytest output clean
        pass

    def _authenticated(self) -> bool:
        return f"canvas_session={GOOD_COOKIE}" in (self.headers.get("Cookie") or "")

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib naming
        path = urlparse(self.path).path
        Handler.received.append(("GET", path, dict(self.headers)))

        if path == "/login/canvas":
            body = b"<html><body>Please log in</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not self._authenticated():
            # This is what a real Canvas does to a session-authenticated request
            # once the session dies: it redirects to the login page.
            self.send_response(302)
            self.send_header("Location", "/login/canvas")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/":
            self.send_response(200)
            self.send_header("Set-Cookie", "_csrf_token=abc%3D%3D; path=/")
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/v1/users/self":
            return self._json(200, PROFILE)
        if path == "/api/v1/courses":
            return self._json(200, COURSES)
        return self._json(404, {"errors": [{"message": "not found"}]})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode()
        Handler.received.append(("POST", path, dict(self.headers)))

        if not self._authenticated():
            return self._json(401, {"status": "unauthenticated"})
        if not self.headers.get("X-CSRF-Token"):
            return self._json(422, {"errors": [{"message": "Invalid Authenticity Token"}]})
        fields = {k: v[0] for k, v in parse_qs(raw).items()}
        return self._json(200, {"id": "555", **fields})


@pytest.fixture(scope="module")
def canvas_server():
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(autouse=True)
def clear_log():
    Handler.received.clear()


async def test_session_cookie_authenticates_a_real_request(canvas_server):
    creds = Credentials(base_url=canvas_server, cookies={"canvas_session": GOOD_COOKIE})
    async with CanvasClient(creds) as client:
        profile = await client.profile()
        courses = await client.courses()
    assert profile["name"] == "Sam Rivera"
    assert courses[0]["course_code"] == "BIO 101"
    assert any(f"canvas_session={GOOD_COOKIE}" in h.get("Cookie", "") for _, _, h in Handler.received)


async def test_dead_session_redirected_to_login_is_reported_as_auth_error(canvas_server):
    creds = Credentials(base_url=canvas_server, cookies={"canvas_session": "expired"})
    async with CanvasClient(creds) as client:
        with pytest.raises(AuthError) as excinfo:
            await client.profile()
    assert "expired" in str(excinfo.value)


async def test_csrf_token_is_fetched_from_the_site_then_echoed(canvas_server):
    """No _csrf_token to start with: the client must go get one before posting."""
    creds = Credentials(base_url=canvas_server, cookies={"canvas_session": GOOD_COOKIE})
    async with CanvasClient(creds) as client:
        created = await client.post("/api/v1/planner_notes", {"title": "read chapter 4"})
    assert created["title"] == "read chapter 4"
    post_headers = next(h for method, _, h in Handler.received if method == "POST")
    assert post_headers["X-CSRF-Token"] == "abc=="


async def test_env_credentials_drive_the_whole_stack(canvas_server, monkeypatch):
    monkeypatch.setenv("CANVAS_BASE_URL", canvas_server)
    monkeypatch.setenv("CANVAS_SESSION_COOKIE", f"canvas_session={GOOD_COOKIE}")

    connection, _notes = await auth.connect()
    assert connection.display_name == "Sam Rivera"

    server._state.client = CanvasClient(connection.credentials)
    server._state.profile = connection.profile
    server._state.source = connection.credentials.source
    server._state.revalidate_after = time.monotonic() + 60
    try:
        out = await server.list_courses()
    finally:
        await server._state.client.aclose()
        server._state.client = None
        server._state.revalidate_after = 0.0

    assert "Introduction to Biology (BIO 101)" in out
    assert "grade 91%" in out
