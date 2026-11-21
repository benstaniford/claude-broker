"""claude-broker: Claude Code as a local service for containers, over unix sockets.

Each client app (usually a Docker container) gets its own socket. The app sends prompts,
JSON schemas and text-only profiles; the broker runs `claude -p` as the user who installed
it and streams the reply back. The broker never runs the app's code: an app's tools are an
MCP server inside its container, reached through `claude-broker mcp-proxy`.
"""

try:
    from ._version import VERSION
except ImportError:   # a source checkout: build-deb writes _version.py into the zipapp
    VERSION = "0.0.0-dev"

__version__ = VERSION
