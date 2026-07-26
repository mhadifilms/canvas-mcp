"""Everything that keeps a borrowed session working past the first day."""

import asyncio
import sqlite3
import time

import httpx

from canvas_mcp import auth, browser_cookies, config, server
from canvas_mcp.client import CanvasClient, Credentials
from canvas_mcp.errors import AuthError

from fake_canvas import HOST, PROFILE


# --------------------------------------------------------------------------- #
# Capturing the remember-me cookie
# --------------------------------------------------------------------------- #

def _firefox_profile(root, cookies):
    profile = root / "abc.default-release"
    profile.mkdir(parents=True)
    conn = sqlite3.connect(profile / "cookies.sqlite")
    conn.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, host TEXT, name TEXT, value TEXT)")
    conn.executemany("INSERT INTO moz_cookies (host, name, value) VALUES (?, ?, ?)", cookies)
    conn.commit()
    conn.close()


def test_self_hosted_canvas_keeps_its_remember_me_cookie(tmp_path, monkeypatch):
    """A host that isn't *.instructure.com is found by cookie name.

    Finding it that way must not mean we only take the cookies we searched by -
    the remember-me cookie is the one that outlives the session.
    """
    root = tmp_path / "firefox"
    _firefox_profile(
        root,
        [
            ("lms.college.edu", "canvas_session", "sess"),
            ("lms.college.edu", "pseudonym_credentials", "remember-me-value"),
            ("lms.college.edu", "_csrf_token", "csrf"),
            (".google.com", "SID", "unrelated"),
        ],
    )
    monkeypatch.setattr(browser_cookies, "_firefox_profile_roots", lambda: [root])

    sessions = browser_cookies.firefox_sessions()
    assert len(sessions) == 1
    session = sessions[0]
    assert session.cookies["pseudonym_credentials"] == "remember-me-value"
    assert session.has_remember_me()
    assert "SID" not in session.cookies  # still nothing from other sites


def test_remember_me_detection_handles_suffixed_names():
    assert browser_cookies.is_remember_me("pseudonym_credentials")
    assert browser_cookies.is_remember_me("pseudonym_credentials_1a2b3c")
    assert not browser_cookies.is_remember_me("canvas_session")


def test_a_login_with_remember_me_is_preferred(monkeypatch):
    plain = browser_cookies.BrowserSession("chrome", "a.instructure.com", {"canvas_session": "x"})
    durable = browser_cookies.BrowserSession(
        "firefox", "b.instructure.com", {"canvas_session": "y", "pseudonym_credentials": "z"}
    )
    monkeypatch.setattr(browser_cookies, "firefox_sessions", lambda host=None: [plain, durable])
    monkeypatch.setattr(browser_cookies, "bc3_sessions", lambda host=None: ([], []))

    sessions, _notes = browser_cookies.discover_sessions()
    assert sessions[0].host == "b.instructure.com"


# --------------------------------------------------------------------------- #
# Persisting the rotated cookie jar
# --------------------------------------------------------------------------- #

def test_refresh_stored_cookies_writes_the_new_jar():
    config.save_session(
        config.StoredSession(base_url=HOST, cookies={"canvas_session": "old"}, source="test")
    )
    assert auth.refresh_stored_cookies(HOST, {"canvas_session": "new"}) is True
    assert config.load_session().cookies == {"canvas_session": "new"}


def test_refresh_stored_cookies_is_a_no_op_when_nothing_changed():
    config.save_session(
        config.StoredSession(base_url=HOST, cookies={"canvas_session": "same"}, source="test")
    )
    before = config.load_session().saved_at
    assert auth.refresh_stored_cookies(HOST, {"canvas_session": "same"}) is False
    assert config.load_session().saved_at == before


def test_refresh_stored_cookies_ignores_a_different_host():
    config.save_session(
        config.StoredSession(base_url=HOST, cookies={"canvas_session": "mine"}, source="test")
    )
    assert auth.refresh_stored_cookies("https://elsewhere.edu", {"canvas_session": "theirs"}) is False
    assert config.load_session().cookies == {"canvas_session": "mine"}


async def test_client_exposes_the_live_cookie_jar():
    """Canvas rotates the session cookie; what we send next is not what we started with."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=PROFILE,
            headers={"Set-Cookie": "canvas_session=rotated; Path=/"},
        )

    creds = Credentials(base_url=HOST, cookies={"canvas_session": "original"})
    async with CanvasClient(creds, transport=httpx.MockTransport(handler)) as client:
        assert client.current_cookies()["canvas_session"] == "original"
        await client.profile()
        assert client.current_cookies()["canvas_session"] == "rotated"


# --------------------------------------------------------------------------- #
# Keepalive
# --------------------------------------------------------------------------- #

async def test_keepalive_pings_and_saves_the_refreshed_jar():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, json=PROFILE, headers={"Set-Cookie": f"canvas_session=tick{calls['n']}; Path=/"}
        )

    config.save_session(
        config.StoredSession(base_url=HOST, cookies={"canvas_session": "start"}, source="test")
    )
    creds = Credentials(base_url=HOST, cookies={"canvas_session": "start"})
    client = CanvasClient(creds, transport=httpx.MockTransport(handler))
    server._state.client = client
    try:
        await server._keepalive_tick()
    finally:
        await client.aclose()
        server._state.client = None

    assert calls["n"] == 1
    assert config.load_session().cookies["canvas_session"] == "tick1"


async def test_keepalive_marks_the_session_stale_instead_of_reconnecting():
    """A background task must not start a browser scan behind the student's back."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": "unauthenticated"})

    creds = Credentials(base_url=HOST, cookies={"canvas_session": "dead"})
    client = CanvasClient(creds, transport=httpx.MockTransport(handler))
    server._state.client = client
    server._state.revalidate_after = time.monotonic() + 9999
    try:
        await server._keepalive_tick()
        assert server._state.revalidate_after == 0.0
    finally:
        await client.aclose()
        server._state.client = None
        server._state.revalidate_after = 0.0


async def test_keepalive_tick_is_harmless_with_no_connection():
    server._state.client = None
    await server._keepalive_tick()  # must not raise


async def test_keepalive_loop_runs_the_tick(monkeypatch):
    ticks = {"n": 0}

    async def counting_tick():
        ticks["n"] += 1

    monkeypatch.setattr(server, "_keepalive_tick", counting_tick)
    task = asyncio.create_task(server._keepalive_loop(0))
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert ticks["n"] > 0


def test_keepalive_interval_is_configurable(monkeypatch):
    monkeypatch.setenv("CANVAS_MCP_KEEPALIVE_SECONDS", "60")
    assert server._keepalive_interval() == 60
    monkeypatch.setenv("CANVAS_MCP_KEEPALIVE_SECONDS", "0")
    assert server._keepalive_interval() == 0
    monkeypatch.setenv("CANVAS_MCP_KEEPALIVE_SECONDS", "nonsense")
    assert server._keepalive_interval() == 600


def test_keepalive_is_not_started_when_disabled(monkeypatch):
    monkeypatch.setenv("CANVAS_MCP_KEEPALIVE_SECONDS", "0")
    server._state.keepalive = None
    server._ensure_keepalive()
    assert server._state.keepalive is None


# --------------------------------------------------------------------------- #
# Silent re-authentication
# --------------------------------------------------------------------------- #

async def test_a_tool_retries_once_on_an_expired_session(monkeypatch):
    """A cookie dying mid-conversation should cost the student nothing."""
    attempts = {"n": 0}
    reconnects = {"n": 0}

    @server.handle_errors
    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise AuthError("session expired")
        return "here are your courses"

    async def fake_reset():
        return None

    async def fake_get_client():
        reconnects["n"] += 1
        return None

    monkeypatch.setattr(server, "_reset_client", fake_reset)
    monkeypatch.setattr(server, "_get_client", fake_get_client)

    assert await flaky() == "here are your courses"
    assert attempts["n"] == 2
    assert reconnects["n"] == 1


async def test_the_retry_gives_up_gracefully_if_reconnecting_fails(monkeypatch):
    @server.handle_errors
    async def always_expired() -> str:
        raise AuthError("session expired")

    async def fake_reset():
        return None

    async def failing_get_client():
        raise AuthError("no session anywhere", hint="log in again")

    monkeypatch.setattr(server, "_reset_client", fake_reset)
    monkeypatch.setattr(server, "_get_client", failing_get_client)

    out = await always_expired()
    assert "Not connected to Canvas." in out
    assert "log in again" in out


async def test_connection_tools_do_not_retry(monkeypatch):
    """`connect` reconnecting inside its own failure handler would be a loop."""
    attempts = {"n": 0}

    @server.handle_errors(reconnect=False)
    async def connect_like() -> str:
        attempts["n"] += 1
        raise AuthError("nothing found", hint="try the browser")

    out = await connect_like()
    assert attempts["n"] == 1
    assert "try the browser" in out


async def test_non_auth_errors_are_not_retried(monkeypatch):
    from canvas_mcp.errors import CanvasMCPError

    attempts = {"n": 0}

    @server.handle_errors
    async def broken() -> str:
        attempts["n"] += 1
        raise CanvasMCPError("Canvas returned 500")

    out = await broken()
    assert attempts["n"] == 1
    assert "500" in out
