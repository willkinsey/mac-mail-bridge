"""MCP adapter (stdio). Hermes Agent (or any MCP client) launches this as a tool server.

It holds no Mail access itself: it only lists the helper's fixed operations as
tools and forwards calls to the helper's socket. Standard library only.

Run:  /usr/bin/python3 -m mailbridge.mcp_server
"""

import json
import sys
from typing import Any, Dict, Optional

from . import __version__, client, operations

TOOL_PREFIX = "mail_bridge_"

# Shown to the agent as this tool server's description. Some MCP clients hide
# tool servers behind these descriptions and skip ones that are blank.
INSTRUCTIONS = (
    "Apple Mail bridge on this Mac. Use these tools for any Apple Mail task "
    "instead of computer use, AppleScript, or osascript. Tools: mail_bridge_ping "
    "checks that the Mail bridge helper is running (it does not touch Mail); "
    "mail_bridge_account_present reports whether the allowlisted Mail account is set up "
    "and enabled (read-only); mail_bridge_latest_subject returns the subject line and "
    "received time of the newest email in the allowlisted account's Inbox (read-only)."
)
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


def tool_list():
    return [
        {
            "name": TOOL_PREFIX + name,
            "description": spec["description"],
            "inputSchema": spec["schema"],
        }
        for name, spec in operations.OPERATIONS.items()
    ]


def _text_result(payload: Any, is_error: bool = False) -> Dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle(msg: Dict[str, Any], socket_path=None) -> Optional[Dict[str, Any]]:
    """Handle one JSON-RPC message. Returns a response, or None for notifications."""
    method = msg.get("method")
    msg_id = msg.get("id")
    is_request = "id" in msg
    params = msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    if not is_request:
        return None  # notifications (e.g. notifications/initialized) need no reply
    if not isinstance(params, dict):
        return err(-32602, "params must be an object")

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return ok(
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mac-mail-bridge", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        )
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        name = params.get("name", "")
        if not isinstance(name, str) or not name.startswith(TOOL_PREFIX):
            return err(-32602, "unknown tool: %r" % (name,))
        op = name[len(TOOL_PREFIX):]
        if op not in operations.OPERATIONS:
            return err(-32602, "unknown tool: %r" % (name,))
        try:
            resp = client.call(op, params.get("arguments") or {}, path=socket_path)
        except client.BridgeUnavailable as exc:
            return ok(_text_result(str(exc), is_error=True))
        if resp.get("ok"):
            return ok(_text_result(resp.get("result")))
        return ok(_text_result(resp.get("error", "helper reported an error"), is_error=True))
    return err(-32601, "method not found: %s" % (method,))


def handle_raw(raw: bytes, socket_path=None) -> Optional[Dict[str, Any]]:
    """Handle one raw stdin line. Never raises, so bad input cannot kill the adapter."""
    try:
        msg = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    try:
        return handle(msg, socket_path=socket_path)
    except Exception as exc:
        if "id" not in msg:
            return None
        return {"jsonrpc": "2.0", "id": msg.get("id"),
                "error": {"code": -32603, "message": "internal error: %s" % type(exc).__name__}}


def main() -> int:
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        out = handle_raw(raw)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
