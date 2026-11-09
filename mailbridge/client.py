"""Tiny client for the helper's socket. Used by the MCP adapter."""

import json
import socket
from typing import Any, Dict, Optional

from . import config


class BridgeUnavailable(Exception):
    """The helper is not running or did not answer."""


def call(op: str, args: Optional[Dict[str, Any]] = None, path=None) -> Dict[str, Any]:
    path = str(path or config.socket_path())
    req = (json.dumps({"op": op, "args": args or {}}) + "\n").encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(config.CLIENT_TIMEOUT_SEC)
            s.connect(path)
            s.sendall(req)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except (OSError, socket.timeout) as exc:
        raise BridgeUnavailable(
            "Mac Mail bridge helper is not reachable at %s (%s)" % (path, exc)
        )
    try:
        return json.loads(buf.decode("utf-8"))
    except ValueError:
        raise BridgeUnavailable("Mac Mail bridge helper sent an unreadable reply")
