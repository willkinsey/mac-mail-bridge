"""Apple Mail access. Only the helper process imports this.

Scripts live in mailbridge/scripts/ and take their inputs as arguments
(never spliced into script text), so input cannot change what a script does.
"""

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import List

from . import config

log = logging.getLogger("mailbridge.mail")
SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
OSASCRIPT = "/usr/bin/osascript"


class MailError(Exception):
    """Mail could not be reached or answered unexpectedly. Message is safe to show."""


def allowed_accounts() -> List[str]:
    path = config.settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MailError("no account allowlist configured (missing %s)" % path)
    except ValueError:
        raise MailError("account allowlist file is not valid JSON")
    accounts = data.get("allowed_accounts") if isinstance(data, dict) else None
    if (
        not isinstance(accounts, list)
        or not accounts
        or not all(isinstance(a, str) and "@" in a for a in accounts)
    ):
        raise MailError("account allowlist must list at least one email address")
    return [a.strip().lower() for a in accounts]


def run_script(name: str, *args: str) -> str:
    script = SCRIPTS_DIR / (name + ".applescript")
    try:
        proc = subprocess.run(
            [OSASCRIPT, str(script)] + list(args),
            capture_output=True,
            text=True,
            timeout=config.MAIL_SCRIPT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        log.warning("%s timed out after %ss", name, config.MAIL_SCRIPT_TIMEOUT_SEC)
        raise MailError("Mail did not answer within %d seconds" % config.MAIL_SCRIPT_TIMEOUT_SEC)
    if proc.returncode != 0:
        # osascript errors end with the AppleScript error number, e.g. "... (-1743)".
        codes = re.findall(r"\((-\d+)\)", proc.stderr or "")
        err = codes[-1] if codes else ""
        # Raw detail goes to the helper's own log only, never back to the caller.
        log.warning("%s failed: %s", name, (proc.stderr or "").strip()[:500])
        if err == "-1743":
            raise MailError(
                "macOS has not allowed the helper to control Mail. Allow it in System "
                "Settings > Privacy & Security > Automation (Python > Mail)."
            )
        if err == "-600":
            raise MailError("Mail is not running")
        if err == "-1712":
            raise MailError("Mail timed out answering the request")
        raise MailError("Mail script failed (osascript exit %d)" % proc.returncode)
    return proc.stdout.strip()
