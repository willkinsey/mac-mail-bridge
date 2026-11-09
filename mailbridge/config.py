"""Shared settings for the helper and its MCP adapter."""

import os
from pathlib import Path

DEFAULT_SOCKET = (
    Path.home() / "Library" / "Application Support" / "mac-mail-bridge" / "bridge.sock"
)

# Largest request the helper will read, in bytes. Requests are tiny JSON lines.
MAX_REQUEST_BYTES = 64 * 1024

# How long the adapter waits for the helper before giving up, in seconds.
CLIENT_TIMEOUT_SEC = 45.0  # longer than MAIL_SCRIPT_TIMEOUT_SEC so Mail timeouts are reported, not masked


DEFAULT_SETTINGS = DEFAULT_SOCKET.parent / "settings.json"

# How long one Mail script may run before it is stopped, in seconds.
MAIL_SCRIPT_TIMEOUT_SEC = 30.0


def settings_path() -> Path:
    """Local, uncommitted settings (account allowlist); MAILBRIDGE_SETTINGS overrides it."""
    override = os.environ.get("MAILBRIDGE_SETTINGS")
    return Path(override) if override else DEFAULT_SETTINGS


def socket_path() -> Path:
    """Socket location; MAILBRIDGE_SOCKET overrides it (used by tests)."""
    override = os.environ.get("MAILBRIDGE_SOCKET")
    return Path(override) if override else DEFAULT_SOCKET
