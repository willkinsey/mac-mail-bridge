"""The helper: a small local server that is the only thing allowed to talk to Mail.

It listens on a Unix socket readable only by this user (no network port).
Protocol: one JSON line per connection.
  request:  {"op": "ping", "args": {}}
  response: {"ok": true, "result": {...}}  or  {"ok": false, "error": "..."}

Run:  /usr/bin/python3 -m mailbridge.daemon
"""

import json
import logging
import os
import signal
import socketserver
import sys
import threading
from pathlib import Path

from . import config, operations

log = logging.getLogger("mailbridge.daemon")

# Mail's scripting interface is not safe to drive from several threads at once,
# so operations run one at a time.
_op_lock = threading.Lock()


def handle_line(raw: bytes) -> dict:
    """Turn one request line into one response object. Never raises."""
    try:
        req = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"ok": False, "error": "request is not valid JSON"}
    if not isinstance(req, dict) or not isinstance(req.get("op"), str):
        return {"ok": False, "error": "request must be an object with a string 'op'"}
    try:
        with _op_lock:
            result = operations.run(req["op"], req.get("args"))
        return {"ok": True, "result": result}
    except operations.OperationError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:  # keep the helper alive; details go to the log only
        log.exception("operation %s crashed", req.get("op"))
        return {"ok": False, "error": "internal error in helper (see helper log)"}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(config.CLIENT_TIMEOUT_SEC)
        try:
            raw = self.rfile.readline(config.MAX_REQUEST_BYTES + 1)
        except OSError:
            return
        if len(raw) > config.MAX_REQUEST_BYTES:
            resp = {"ok": False, "error": "request too large"}
        else:
            resp = handle_line(raw)
        log.info("op=%s ok=%s", _op_name(raw), resp["ok"])
        try:
            self.wfile.write((json.dumps(resp) + "\n").encode("utf-8"))
        except OSError:
            pass


def _op_name(raw: bytes) -> str:
    try:
        return str(json.loads(raw).get("op"))[:40]
    except Exception:
        return "?"


class BridgeServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def make_server(path: Path) -> BridgeServer:
    if len(os.fsencode(str(path))) > 103:  # macOS sun_path limit is 104 bytes incl. NUL
        raise SystemExit("socket path too long for macOS (max 103 bytes): %s" % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists() or path.is_symlink():
        path.unlink()  # stale socket from a previous run
    old_umask = os.umask(0o177)
    try:
        server = BridgeServer(str(path), _Handler)
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o600)
    return server


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    path = config.socket_path()
    server = make_server(path)

    def _stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("listening on %s", path)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
