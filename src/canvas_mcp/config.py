"""On-disk state: where the saved Canvas session lives and how it is written.

Everything is kept in a single directory (``~/.canvas-mcp`` by default) with
restrictive permissions, because the session cookie in there is as good as a
password for the student's Canvas account.
"""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_DIR_ENV = "CANVAS_MCP_HOME"
DEFAULT_DIR_NAME = ".canvas-mcp"
SESSION_FILE = "session.json"


def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / DEFAULT_DIR_NAME


def session_path() -> Path:
    return config_dir() / SESSION_FILE


def _ensure_dir() -> Path:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(stat.S_IRWXU)  # 0700
    except OSError:
        pass  # Windows, or an exotic filesystem. Not fatal.
    return d


@dataclass
class StoredSession:
    """A Canvas session we can replay: a host plus the cookies that authorize it."""

    base_url: str
    cookies: dict[str, str] = field(default_factory=dict)
    token: str | None = None
    source: str = "unknown"
    saved_at: float = 0.0
    user: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "cookies": self.cookies,
            "token": self.token,
            "source": self.source,
            "saved_at": self.saved_at,
            "user": self.user,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "StoredSession":
        return cls(
            base_url=data.get("base_url", ""),
            cookies=dict(data.get("cookies") or {}),
            token=data.get("token"),
            source=data.get("source", "unknown"),
            saved_at=float(data.get("saved_at") or 0.0),
            user=dict(data.get("user") or {}),
        )

    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.saved_at) if self.saved_at else float("inf")


def load_session() -> StoredSession | None:
    path = session_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    session = StoredSession.from_json(data)
    if not session.base_url or (not session.cookies and not session.token):
        return None
    return session


def save_session(session: StoredSession) -> Path:
    _ensure_dir()
    path = session_path()
    session.saved_at = session.saved_at or time.time()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(session.to_json(), indent=2), encoding="utf-8")
    try:
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass
    tmp.replace(path)
    return path


def clear_session() -> bool:
    path = session_path()
    if path.exists():
        path.unlink()
        return True
    return False


def normalize_base_url(url: str) -> str:
    """Turn whatever the student typed into ``https://host`` with no trailing slash."""
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    # Drop any path the student pasted along with the host ("/courses/123").
    scheme, _, rest = url.partition("://")
    host = rest.split("/", 1)[0]
    return f"{scheme.lower()}://{host.lower()}".rstrip("/")


def env_base_url() -> str:
    return normalize_base_url(os.environ.get("CANVAS_BASE_URL", ""))


def read_only() -> bool:
    return os.environ.get("CANVAS_MCP_READ_ONLY", "").strip().lower() in {"1", "true", "yes"}


def auto_import_enabled() -> bool:
    return os.environ.get("CANVAS_MCP_AUTO_IMPORT", "1").strip().lower() not in {"0", "false", "no"}
