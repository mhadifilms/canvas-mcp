import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def no_backoff_sleeps(monkeypatch):
    """Retry backoff is real in production and pointless in tests."""
    import canvas_mcp.client as client_module

    async def instant(_seconds):
        return None

    monkeypatch.setattr(client_module.asyncio, "sleep", instant)


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path, monkeypatch):
    """Never touch the developer's real ~/.canvas-mcp while testing."""
    monkeypatch.setenv("CANVAS_MCP_HOME", str(tmp_path / "canvas-mcp"))
    for var in ("CANVAS_BASE_URL", "CANVAS_API_TOKEN", "CANVAS_SESSION_COOKIE", "CANVAS_MCP_READ_ONLY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CANVAS_MCP_TZ", "UTC")
