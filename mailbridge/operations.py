"""The fixed list of operations the helper will run.

There is deliberately no generic "run script" or "run command" operation.
Every operation is a named Python function with a fixed input schema.
Adding an operation is a code change, reviewed and committed like any other.
"""

import datetime
from typing import Any, Callable, Dict

from . import __version__


class OperationError(Exception):
    """An operation failed in an expected, reportable way."""


def _ping(args: Dict[str, Any]) -> Dict[str, Any]:
    # Dummy operation: proves a client can reach the helper. Touches nothing.
    return {
        "service": "mac-mail-bridge",
        "version": __version__,
        "pong": True,
        "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }


_ACCOUNT_STATES = {
    "present_enabled": (True, True),
    "present_disabled": (True, False),
    "absent": (False, None),
}


def _account_present(args: Dict[str, Any]) -> Dict[str, Any]:
    # Read-only. Checks only allowlisted addresses; returns nothing about other accounts.
    from . import mail  # imported here so the MCP adapter never loads Mail code

    try:
        accounts = mail.allowed_accounts()
        results = []
        for address in accounts:
            state = mail.run_script("account_present", address)
            if state == "not_running":
                return {"mail_running": False, "accounts": []}
            if state not in _ACCOUNT_STATES:
                raise mail.MailError("Mail returned an unexpected answer")
            present, enabled = _ACCOUNT_STATES[state]
            results.append({"account": address, "present": present, "enabled": enabled})
    except mail.MailError as exc:
        raise OperationError(str(exc))
    return {"mail_running": True, "accounts": results}


_SUBJECT_MAX = 300
_SEP = "\x1e"


def _latest_subject(args: Dict[str, Any]) -> Dict[str, Any]:
    # Read-only. Subject and received time of the newest INBOX message for each
    # allowlisted account. No sender, body, or other accounts.
    from . import mail

    try:
        results = []
        for address in mail.allowed_accounts():
            out = mail.run_script("latest_subject", address)
            if out == "not_running":
                return {"mail_running": False, "accounts": []}
            if out in ("absent", "no_inbox", "empty"):
                results.append({"account": address, "status": out, "subject": None, "received": None})
                continue
            parts = out.split(_SEP)
            if len(parts) != 3 or parts[0] != "ok":
                raise mail.MailError("Mail returned an unexpected answer")
            results.append({
                "account": address,
                "status": "ok",
                "subject": parts[1][:_SUBJECT_MAX],
                "received": parts[2],
            })
    except mail.MailError as exc:
        raise OperationError(str(exc))
    return {"mail_running": True, "accounts": results}


# name -> spec. "schema" is the JSON Schema for the arguments (MCP inputSchema).
OPERATIONS: Dict[str, Dict[str, Any]] = {
    "ping": {
        "description": "Check that the Mac Mail bridge helper is running. Does not touch Mail.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": _ping,
    },
    "account_present": {
        "description": (
            "Read-only. Report whether the allowlisted Mail account is set up and "
            "enabled in Apple Mail. Does not open Mail, read messages, or change anything."
        ),
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": _account_present,
    },
    "latest_subject": {
        "description": (
            "Read-only. Return the subject line and received time (local, yyyy-mm-dd hh:mm) "
            "of the most recent email in the allowlisted account's Inbox. Does not open Mail, "
            "return senders or message bodies, or change anything."
        ),
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": _latest_subject,
    },
}


def run(op: str, args: Any) -> Dict[str, Any]:
    spec = OPERATIONS.get(op)
    if spec is None:
        raise OperationError("unknown operation: %r" % (op,))
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise OperationError("arguments must be an object")
    allowed = set(spec["schema"].get("properties", {}))
    extra = set(args) - allowed
    if extra:
        raise OperationError("unexpected arguments: %s" % ", ".join(sorted(extra)))
    handler: Callable[[Dict[str, Any]], Dict[str, Any]] = spec["handler"]
    return handler(args)
