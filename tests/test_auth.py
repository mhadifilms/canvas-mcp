import pytest

from canvas_mcp import auth, config
from canvas_mcp.errors import AuthError

from fake_canvas import HOST, PROFILE, FakeCanvas


def test_parse_cookie_input_accepts_every_shape_a_student_might_paste():
    assert auth.parse_cookie_input("rawvalue") == {"canvas_session": "rawvalue"}
    assert auth.parse_cookie_input("canvas_session=abc") == {"canvas_session": "abc"}
    assert auth.parse_cookie_input("canvas_session=abc; _csrf_token=xyz") == {
        "canvas_session": "abc",
        "_csrf_token": "xyz",
    }
    assert auth.parse_cookie_input("Cookie: canvas_session=abc; other=1")["canvas_session"] == "abc"
    assert auth.parse_cookie_input("   ") == {}


def test_env_token_requires_a_base_url(monkeypatch):
    monkeypatch.setenv("CANVAS_API_TOKEN", "1234~secret")
    with pytest.raises(AuthError) as excinfo:
        auth.credentials_from_env()
    assert "CANVAS_BASE_URL" in str(excinfo.value)


def test_env_token_is_used_when_present(monkeypatch):
    monkeypatch.setenv("CANVAS_API_TOKEN", "1234~secret")
    monkeypatch.setenv("CANVAS_BASE_URL", "school.instructure.com")
    creds = auth.credentials_from_env()
    assert creds.token == "1234~secret"
    assert creds.base_url == "https://school.instructure.com"


def test_stored_session_round_trips_and_is_owner_only():
    stored = config.StoredSession(
        base_url=HOST, cookies={"canvas_session": "abc"}, source="browser:chrome"
    )
    path = config.save_session(stored)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, f"session file should be private, got {oct(mode)}"

    loaded = config.load_session()
    assert loaded is not None
    assert loaded.base_url == HOST
    assert loaded.cookies == {"canvas_session": "abc"}

    assert config.clear_session() is True
    assert config.load_session() is None


def test_normalize_base_url_tolerates_pasted_urls():
    assert config.normalize_base_url("school.instructure.com") == "https://school.instructure.com"
    assert config.normalize_base_url("https://School.Instructure.com/") == "https://school.instructure.com"
    assert config.normalize_base_url("https://school.instructure.com/courses/12") == "https://school.instructure.com"
    assert config.normalize_base_url("") == ""


async def test_connect_with_manual_cookie_validates_and_saves(monkeypatch):
    fake = FakeCanvas()

    async def fake_validate(creds, *, timeout=20.0):
        assert creds.cookies["canvas_session"] == "pasted"
        return PROFILE

    monkeypatch.setattr(auth, "validate", fake_validate)
    connection, _notes = await auth.connect(base_url=HOST, session_cookie="pasted")

    assert connection.display_name == "Sam"
    saved = config.load_session()
    assert saved is not None and saved.source == "manual cookie"
    assert saved.user["login_id"] == "srivera"
    assert fake.requests == []  # validation was stubbed; nothing else hit the network


async def test_connect_falls_back_to_browser_scan(monkeypatch):
    from canvas_mcp import browser_cookies

    session = browser_cookies.BrowserSession(
        browser="firefox", host="school.instructure.com", cookies={"canvas_session": "fromff"}
    )
    monkeypatch.setattr(browser_cookies, "discover_sessions", lambda host=None: ([session], []))

    async def fake_validate(creds, *, timeout=20.0):
        assert creds.cookies == {"canvas_session": "fromff"}
        return PROFILE

    monkeypatch.setattr(auth, "validate", fake_validate)
    connection, _ = await auth.connect()
    assert connection.credentials.source == "browser:firefox"
    assert config.load_session().base_url == "https://school.instructure.com"


async def test_connect_explains_what_it_tried_when_nothing_works(monkeypatch):
    from canvas_mcp import browser_cookies

    monkeypatch.setattr(
        browser_cookies, "discover_sessions", lambda host=None: ([], ["chrome: profile is locked"])
    )
    with pytest.raises(AuthError) as excinfo:
        await auth.connect()
    message = excinfo.value.full_message()
    assert "chrome: profile is locked" in message
    assert "canvas-mcp login" in message


async def test_expired_stored_session_triggers_a_rescan(monkeypatch):
    from canvas_mcp import browser_cookies

    config.save_session(
        config.StoredSession(base_url=HOST, cookies={"canvas_session": "stale"}, source="saved")
    )
    fresh = browser_cookies.BrowserSession(
        browser="chrome", host="school.instructure.com", cookies={"canvas_session": "fresh"}
    )
    monkeypatch.setattr(browser_cookies, "discover_sessions", lambda host=None: ([fresh], []))

    async def fake_validate(creds, *, timeout=20.0):
        if creds.cookies.get("canvas_session") == "stale":
            raise AuthError("expired")
        return PROFILE

    monkeypatch.setattr(auth, "validate", fake_validate)
    connection, notes = await auth.connect()
    assert connection.credentials.cookies["canvas_session"] == "fresh"
    assert any("expired" in note for note in notes)
