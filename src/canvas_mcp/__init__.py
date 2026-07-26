"""canvas-mcp: Canvas LMS over the Model Context Protocol, without an API key."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("canvas-mcp")
except PackageNotFoundError:  # running from a source tree, not installed
    __version__ = "0+unknown"

__all__ = ["__version__"]
