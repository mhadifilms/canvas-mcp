import sqlite3

from canvas_mcp import browser_cookies


def _make_firefox_profile(root, cookies):
    profile = root / "abc.default-release"
    profile.mkdir(parents=True)
    db = profile / "cookies.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, host TEXT, name TEXT, value TEXT)"
    )
    conn.executemany("INSERT INTO moz_cookies (host, name, value) VALUES (?, ?, ?)", cookies)
    conn.commit()
    conn.close()
    return db


def test_looks_like_canvas():
    assert browser_cookies.looks_like_canvas("school.instructure.com", set())
    assert browser_cookies.looks_like_canvas("canvas.mit.edu", set())
    assert browser_cookies.looks_like_canvas("lms.college.edu", {"canvas_session"})
    assert not browser_cookies.looks_like_canvas("mail.google.com", {"SID"})


def test_host_matches_handles_domain_cookies():
    assert browser_cookies.host_matches(".instructure.com", "school.instructure.com")
    assert browser_cookies.host_matches("school.instructure.com", "school.instructure.com")
    assert not browser_cookies.host_matches("other.instructure.com", "school.instructure.com")


def test_firefox_reader_finds_canvas_cookies(tmp_path, monkeypatch):
    root = tmp_path / "firefox"
    _make_firefox_profile(
        root,
        [
            ("school.instructure.com", "canvas_session", "sess-value"),
            ("school.instructure.com", "_csrf_token", "csrf-value"),
            (".google.com", "SID", "not-canvas"),
        ],
    )
    monkeypatch.setattr(browser_cookies, "_firefox_profile_roots", lambda: [root])

    sessions = browser_cookies.firefox_sessions()
    assert len(sessions) == 1
    session = sessions[0]
    assert session.host == "school.instructure.com"
    assert session.base_url == "https://school.instructure.com"
    assert session.has_session()
    assert session.cookies["_csrf_token"] == "csrf-value"
    assert "SID" not in session.cookies


def test_firefox_reader_respects_a_host_filter(tmp_path, monkeypatch):
    root = tmp_path / "firefox"
    _make_firefox_profile(
        root,
        [
            ("school.instructure.com", "canvas_session", "a"),
            ("other.instructure.com", "canvas_session", "b"),
        ],
    )
    monkeypatch.setattr(browser_cookies, "_firefox_profile_roots", lambda: [root])

    sessions = browser_cookies.firefox_sessions("other.instructure.com")
    assert [s.host for s in sessions] == ["other.instructure.com"]


def test_discover_orders_real_sessions_first(monkeypatch):
    with_session = browser_cookies.BrowserSession("firefox", "a.instructure.com", {"canvas_session": "x"})
    without = browser_cookies.BrowserSession("chrome", "b.instructure.com", {"_csrf_token": "y"})
    monkeypatch.setattr(browser_cookies, "firefox_sessions", lambda host=None: [without, with_session])
    monkeypatch.setattr(browser_cookies, "bc3_sessions", lambda host=None: ([], []))

    sessions, _notes = browser_cookies.discover_sessions()
    assert sessions[0].has_session()


def test_missing_browser_cookie3_is_reported_not_fatal(monkeypatch):
    monkeypatch.setattr(browser_cookies, "firefox_sessions", lambda host=None: [])
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def blocked(name, *args, **kwargs):
        if name == "browser_cookie3":
            raise ImportError("no module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked)
    sessions, notes = browser_cookies.discover_sessions()
    assert sessions == []
    assert any("browser_cookie3" in note for note in notes)
