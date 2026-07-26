"""Error types shared across the package."""

from __future__ import annotations


class CanvasMCPError(Exception):
    """Base class for everything this package raises on purpose."""


class AuthError(CanvasMCPError):
    """No usable Canvas session, or the one we had stopped working."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint

    def full_message(self) -> str:
        if self.hint:
            return f"{self}\n\n{self.hint}"
        return str(self)


class NotFoundError(CanvasMCPError):
    """Canvas returned 404 for something we asked about."""


class PermissionError_(CanvasMCPError):
    """Canvas says this account may not see the thing (403, not rate limiting)."""


class RateLimitError(CanvasMCPError):
    """Canvas throttled us and retries did not clear it."""


class AmbiguousCourseError(CanvasMCPError):
    """A course name matched more than one course."""


# Wording reused wherever we have to tell the student how to get connected.
CONNECT_HINT = (
    "To connect (no API key needed):\n"
    "  1. Log in to Canvas in your normal browser, then ask me to run `connect`.\n"
    "     I'll pick up the session cookie your browser already has.\n"
    "  2. If that doesn't work, run `canvas-mcp login` in a terminal - it opens a\n"
    "     browser window, you log in through your school's usual page (SSO, Duo,\n"
    "     whatever), and it saves the session.\n"
    "  3. Last resort, paste the cookie manually:\n"
    "     `canvas-mcp set-cookie --base-url https://yourschool.instructure.com`\n"
    "     (it will tell you where to find the value in DevTools)."
)
