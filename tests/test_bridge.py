"""Tests for the mac-mail-bridge helper, socket client, MCP adapter and launchers.

Standard library only. Never touches Apple Mail. Every socket lives in a short
private directory under <project>/.run/ (macOS caps Unix socket paths at 103 bytes).

Run from the project root with:
    /usr/bin/python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from mailbridge import client, config, daemon, mail, mcp_server, operations  # noqa: E402

RUN_DIR = PROJECT / ".run"
BIN_DAEMON = PROJECT / "bin" / "mailbridge-daemon"
BIN_MCP = PROJECT / "bin" / "mailbridge-mcp"
MAX_SOCKET_PATH_BYTES = 103


def _new_socket_dir():
    RUN_DIR.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(dir=str(RUN_DIR)))


def _raw_exchange(sock_path, payload):
    """Send raw bytes to the helper and return the parsed JSON reply line."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(sock_path))
        s.sendall(payload)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8"))


def _call_msg(name, arguments=None, msg_id=1):
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


def _init_msg(version, msg_id=1):
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "initialize",
        "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    }


def _kill_if_running(proc):
    if proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


class TempDirCase(unittest.TestCase):
    """Gives each test a short private directory under .run/, removed afterwards."""

    def setUp(self):
        super().setUp()
        self.dir = _new_socket_dir()
        self.addCleanup(shutil.rmtree, str(self.dir), True)


class LiveServerCase(TempDirCase):
    """Runs an in-process helper serving self.sock in a background thread."""

    def setUp(self):
        super().setUp()
        self.sock = self.dir / "bridge.sock"
        self.server = daemon.make_server(self.sock)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.addCleanup(self._stop_server)
        self.thread.start()

    def _stop_server(self):
        if self.thread.ident is not None:  # shutdown() would block if serve_forever never ran
            self.server.shutdown()
            self.thread.join(timeout=5)


def _isolate_mail(case):
    """Points settings at a file in the case's temp dir and makes any real osascript call fail.

    Returns the settings path. Every account_present test goes through this, so the real
    settings file is never read and an unpatched Mail call fails the test instead of running.
    """
    settings = case.dir / "settings.json"
    env = mock.patch.dict(os.environ, {"MAILBRIDGE_SETTINGS": str(settings)})
    env.start()
    case.addCleanup(env.stop)
    guard = mock.patch(
        "mailbridge.mail.subprocess.run",
        side_effect=AssertionError("test tried to run osascript without a patch"),
    )
    guard.start()
    case.addCleanup(guard.stop)
    return settings


def _write_settings(path, data):
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class IsolatedMailCase(TempDirCase):
    """TempDirCase with an isolated settings file and a guard against real osascript."""

    def setUp(self):
        super().setUp()
        self.settings = _isolate_mail(self)


# --------------------------------------------------------------------------- operations


class OperationsTests(unittest.TestCase):
    def test_ping_returns_pong_and_service_name(self):
        result = operations.run("ping", {})
        self.assertIs(result["pong"], True)
        self.assertEqual(result["service"], "mac-mail-bridge")
        self.assertIn("version", result)
        self.assertIn("time", result)

    def test_ping_accepts_none_args(self):
        result = operations.run("ping", None)
        self.assertIs(result["pong"], True)
        self.assertEqual(result["service"], "mac-mail-bridge")

    def test_unknown_operation_raises(self):
        with self.assertRaises(operations.OperationError):
            operations.run("no_such_op", {})

    def test_non_object_args_raise(self):
        for bad in (["x"], "text", 42):
            with self.subTest(args=bad):
                with self.assertRaises(operations.OperationError):
                    operations.run("ping", bad)

    def test_unexpected_argument_key_raises(self):
        with self.assertRaises(operations.OperationError) as ctx:
            operations.run("ping", {"surprise": 1})
        self.assertIn("surprise", str(ctx.exception))


# --------------------------------------------------------------------------- handle_line


class HandleLineTests(unittest.TestCase):
    def test_invalid_json_returns_error_dict(self):
        resp = daemon.handle_line(b"{not json")
        self.assertIsInstance(resp, dict)
        self.assertIs(resp["ok"], False)

    def test_non_utf8_bytes_return_error_dict(self):
        resp = daemon.handle_line(b"\xff\xfe\x00\x81garbage")
        self.assertIsInstance(resp, dict)
        self.assertIs(resp["ok"], False)

    def test_json_list_returns_error_dict(self):
        resp = daemon.handle_line(b'[1, 2, "ping"]')
        self.assertIsInstance(resp, dict)
        self.assertIs(resp["ok"], False)

    def test_missing_op_returns_error_dict(self):
        resp = daemon.handle_line(b'{"args": {}}')
        self.assertIsInstance(resp, dict)
        self.assertIs(resp["ok"], False)

    def test_non_string_op_returns_error_dict(self):
        resp = daemon.handle_line(b'{"op": 5}')
        self.assertIsInstance(resp, dict)
        self.assertIs(resp["ok"], False)

    def test_unknown_op_returns_error_dict(self):
        resp = daemon.handle_line(b'{"op": "delete_everything", "args": {}}')
        self.assertIs(resp["ok"], False)

    def test_non_object_args_return_error_dict(self):
        resp = daemon.handle_line(b'{"op": "ping", "args": [1]}')
        self.assertIs(resp["ok"], False)

    def test_valid_ping_returns_result(self):
        resp = daemon.handle_line(b'{"op": "ping", "args": {}}\n')
        self.assertIs(resp["ok"], True)
        self.assertIs(resp["result"]["pong"], True)


# --------------------------------------------------------------------------- live socket server


class SocketServerTests(LiveServerCase):
    def test_client_ping_round_trip(self):
        resp = client.call("ping", path=self.sock)
        self.assertIs(resp["ok"], True)
        self.assertIs(resp["result"]["pong"], True)

    def test_socket_mode_0600_and_new_parent_dir_mode_0700(self):
        # Use a parent directory that make_server has to create itself.
        sub = self.dir / "app"
        sock = sub / "bridge.sock"
        server = daemon.make_server(sock)
        self.addCleanup(server.server_close)
        self.assertEqual(stat.S_IMODE(os.stat(str(sock)).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(str(sub)).st_mode), 0o700)

    def test_request_over_limit_gets_too_large_error(self):
        # Exactly MAX+1 bytes including the trailing newline, so the server reads
        # the whole line and nothing is left unread when it closes the connection.
        prefix = b'{"op": "ping", "args": {"pad": "'
        suffix = b'"}}\n'
        pad = config.MAX_REQUEST_BYTES + 1 - len(prefix) - len(suffix)
        payload = prefix + b"x" * pad + suffix
        self.assertEqual(len(payload), config.MAX_REQUEST_BYTES + 1)
        resp = _raw_exchange(self.sock, payload)
        self.assertEqual(resp, {"ok": False, "error": "request too large"})

    def test_request_at_limit_is_not_rejected_as_too_large(self):
        prefix = b'{"op": "ping", "args": {"pad": "'
        suffix = b'"}}\n'
        pad = config.MAX_REQUEST_BYTES - len(prefix) - len(suffix)
        payload = prefix + b"x" * pad + suffix
        self.assertEqual(len(payload), config.MAX_REQUEST_BYTES)
        resp = _raw_exchange(self.sock, payload)
        self.assertIs(resp["ok"], False)
        self.assertNotEqual(resp["error"], "request too large")
        self.assertTrue(resp["error"].startswith("unexpected arguments"), resp)


class StaleSocketPathTests(TempDirCase):
    def test_stale_regular_file_is_replaced_by_socket(self):
        path = self.dir / "bridge.sock"
        path.write_text("stale leftover")
        server = daemon.make_server(path)
        self.addCleanup(server.server_close)
        self.assertTrue(stat.S_ISSOCK(os.stat(str(path)).st_mode))

    def test_socket_left_by_dead_helper_is_replaced(self):
        path = self.dir / "bridge.sock"
        first = daemon.make_server(path)
        first.server_close()  # closes the listener but leaves the file behind
        self.assertTrue(path.exists())
        second = daemon.make_server(path)
        self.addCleanup(second.server_close)
        self.assertTrue(stat.S_ISSOCK(os.stat(str(path)).st_mode))


class SocketPathLimitTests(TempDirCase):
    def _path_of_length(self, total):
        base = os.fsencode(str(self.dir)) + b"/"
        name_len = total - len(base) - len(b".sock")
        self.assertGreater(name_len, 0)
        return Path(os.fsdecode(base + b"s" * name_len + b".sock"))

    def test_path_of_103_bytes_is_accepted(self):
        path = self._path_of_length(MAX_SOCKET_PATH_BYTES)
        self.assertEqual(len(os.fsencode(str(path))), MAX_SOCKET_PATH_BYTES)
        server = daemon.make_server(path)
        self.addCleanup(server.server_close)
        self.assertTrue(path.exists())

    def test_path_of_104_bytes_raises_system_exit(self):
        path = self._path_of_length(MAX_SOCKET_PATH_BYTES + 1)
        self.assertEqual(len(os.fsencode(str(path))), MAX_SOCKET_PATH_BYTES + 1)
        with self.assertRaises(SystemExit):
            daemon.make_server(path)
        self.assertFalse(path.exists())

    def test_much_longer_path_raises_system_exit(self):
        path = self.dir / ("long_" + "x" * 150 + ".sock")
        with self.assertRaises(SystemExit):
            daemon.make_server(path)
        self.assertFalse(path.exists())


# --------------------------------------------------------------------------- client


class ClientTests(TempDirCase):
    def test_nonexistent_socket_raises_bridge_unavailable(self):
        with self.assertRaises(client.BridgeUnavailable):
            client.call("ping", path=self.dir / "nope.sock")

    def test_dead_socket_file_raises_bridge_unavailable(self):
        path = self.dir / "dead.sock"
        server = daemon.make_server(path)
        server.server_close()  # file remains, nothing listens
        with self.assertRaises(client.BridgeUnavailable):
            client.call("ping", path=path)


# --------------------------------------------------------------------------- MCP adapter


class McpServerTests(LiveServerCase):
    def test_initialize_echoes_each_supported_protocol_version(self):
        for version in mcp_server.SUPPORTED_PROTOCOLS:
            with self.subTest(version=version):
                resp = mcp_server.handle(_init_msg(version))
                self.assertEqual(resp["id"], 1)
                self.assertEqual(resp["result"]["protocolVersion"], version)
                self.assertEqual(resp["result"]["serverInfo"]["name"], "mac-mail-bridge")

    def test_initialize_falls_back_for_unknown_protocol_version(self):
        resp = mcp_server.handle(_init_msg("1999-01-01"))
        self.assertEqual(resp["result"]["protocolVersion"], "2025-06-18")

    def test_notification_without_id_returns_none(self):
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        # A tools/call sent as a notification gets no reply either.
        note = _call_msg("mail_bridge_ping")
        del note["id"]
        self.assertIsNone(mcp_server.handle(note, socket_path=self.sock))

    def test_tools_list_contains_exactly_the_three_tools(self):
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = resp["result"]["tools"]
        self.assertEqual(
            sorted(t["name"] for t in tools),
            ["mail_bridge_account_present", "mail_bridge_latest_subject", "mail_bridge_ping"],
        )
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_tools_call_ping_over_live_socket_succeeds(self):
        resp = mcp_server.handle(_call_msg("mail_bridge_ping"), socket_path=self.sock)
        result = resp["result"]
        self.assertIs(result["isError"], False)
        text = result["content"][0]["text"]
        self.assertIn("pong", text)
        self.assertIs(json.loads(text)["pong"], True)

    def test_tools_call_unknown_tool_is_jsonrpc_error(self):
        for name in ("mail_bridge_nope", "other_ping", ""):
            with self.subTest(name=name):
                resp = mcp_server.handle(_call_msg(name), socket_path=self.sock)
                self.assertNotIn("result", resp)
                self.assertEqual(resp["error"]["code"], -32602)

    def test_tools_call_when_helper_down_returns_error_result(self):
        resp = mcp_server.handle(_call_msg("mail_bridge_ping"), socket_path=self.dir / "missing.sock")
        self.assertIs(resp["result"]["isError"], True)
        self.assertIn("not reachable", resp["result"]["content"][0]["text"])

    def test_unknown_method_returns_method_not_found(self):
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
        self.assertEqual(resp["error"]["code"], -32601)
        self.assertEqual(resp["id"], 9)


# --------------------------------------------------------------------------- subprocess end to end


class EndToEndSubprocessTests(TempDirCase):
    def test_daemon_and_mcp_adapter_round_trip_then_sigterm_removes_socket(self):
        sock = self.dir / "e2e.sock"
        # Settings point at a missing file so nothing in this run reads the real allowlist.
        env = dict(os.environ, MAILBRIDGE_SOCKET=str(sock),
                   MAILBRIDGE_SETTINGS=str(self.dir / "settings.json"))
        log_path = self.dir / "daemon.log"
        log_file = open(str(log_path), "w")
        self.addCleanup(log_file.close)

        daemon_proc = subprocess.Popen(
            [str(BIN_DAEMON)], cwd=str(PROJECT), env=env,
            stdout=subprocess.DEVNULL, stderr=log_file,
        )
        self.addCleanup(_kill_if_running, daemon_proc)

        deadline = time.monotonic() + 5
        while not sock.exists() and time.monotonic() < deadline and daemon_proc.poll() is None:
            time.sleep(0.05)
        if not sock.exists():
            self.fail("helper socket did not appear; daemon exit=%s log=%r"
                      % (daemon_proc.poll(), log_path.read_text()))

        requests = [
            _init_msg("2025-06-18", msg_id=1),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            _call_msg("mail_bridge_ping", {}, msg_id=3),
        ]
        stdin_data = "".join(json.dumps(r) + "\n" for r in requests).encode("utf-8")
        mcp_proc = subprocess.Popen(
            [str(BIN_MCP)], cwd=str(PROJECT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.addCleanup(_kill_if_running, mcp_proc)
        out, err = mcp_proc.communicate(stdin_data, timeout=20)

        lines = [ln for ln in out.decode("utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(lines), 3, "stdout=%r stderr=%r" % (out, err))
        replies = [json.loads(ln) for ln in lines]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "mac-mail-bridge")
        self.assertEqual(
            sorted(t["name"] for t in replies[1]["result"]["tools"]),
            ["mail_bridge_account_present", "mail_bridge_latest_subject", "mail_bridge_ping"],
        )
        ping = replies[2]["result"]
        self.assertIs(ping["isError"], False, ping)
        self.assertIn("pong", ping["content"][0]["text"])

        daemon_proc.send_signal(signal.SIGTERM)
        try:
            rc = daemon_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("daemon did not exit within 5s of SIGTERM; log=%r" % log_path.read_text())
        self.assertEqual(rc, 0, "log=%r" % log_path.read_text())
        self.assertFalse(sock.exists(), "socket file left behind after SIGTERM")


class McpRobustnessTests(unittest.TestCase):
    """Bad input must produce an error reply, never crash the adapter."""

    def test_array_params_is_invalid_params_error(self):
        out = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1]})
        self.assertEqual(out["error"]["code"], -32602)

    def test_handle_raw_non_utf8_is_parse_error(self):
        out = mcp_server.handle_raw(b"\xff\xfe\n")
        self.assertEqual(out["error"]["code"], -32700)

    def test_handle_raw_non_object_is_invalid_request(self):
        out = mcp_server.handle_raw(b"[1,2]\n")
        self.assertEqual(out["error"]["code"], -32600)

    def test_adapter_survives_bad_lines_then_answers(self):
        bin_mcp = str(Path(__file__).resolve().parent.parent / "bin" / "mailbridge-mcp")
        stdin = (b"\xff\xfe\n"
                 b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":[1]}\n'
                 b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
        proc = subprocess.run([bin_mcp], input=stdin, capture_output=True, timeout=20)
        lines = [json.loads(l) for l in proc.stdout.decode().splitlines() if l.strip()]
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[2], {"jsonrpc": "2.0", "id": 2, "result": {}})


class McpInstructionsTests(unittest.TestCase):
    def test_initialize_includes_nonblank_instructions_naming_ping(self):
        out = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-06-18"}})
        text = out["result"].get("instructions", "")
        self.assertIn("Apple Mail", text)
        self.assertIn("mail_bridge_ping", text)

    def test_initialize_instructions_mention_account_present(self):
        out = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-06-18"}})
        self.assertIn("mail_bridge_account_present", out["result"].get("instructions", ""))


# --------------------------------------------------------------------------- account_present (Mail)


class AllowlistTests(IsolatedMailCase):
    def test_missing_allowlist_file_raises_mail_error_mentioning_allowlist(self):
        self.assertFalse(self.settings.exists())
        with self.assertRaises(mail.MailError) as ctx:
            mail.allowed_accounts()
        self.assertIn("allowlist", str(ctx.exception))

    def test_invalid_json_allowlist_raises_mail_error(self):
        self.settings.write_text("{not json", encoding="utf-8")
        with self.assertRaises(mail.MailError) as ctx:
            mail.allowed_accounts()
        self.assertIn("allowlist", str(ctx.exception))

    def test_bad_allowlist_shapes_raise_mail_error(self):
        bad_shapes = [
            {"allowed_accounts": []},
            {"allowed_accounts": "a@example.org"},
            {"allowed_accounts": [5]},
            {"allowed_accounts": [None]},
            {"allowed_accounts": ["example.org"]},
            {"allowed_accounts": ["a@example.org", 7]},
            {"other_key": ["a@example.org"]},
            ["a@example.org"],
        ]
        for bad in bad_shapes:
            with self.subTest(data=bad):
                _write_settings(self.settings, bad)
                with self.assertRaises(mail.MailError):
                    mail.allowed_accounts()

    def test_valid_allowlist_is_lowercased_and_stripped(self):
        _write_settings(self.settings, {"allowed_accounts": ["  A@Example.ORG ", "b@example.org"]})
        self.assertEqual(mail.allowed_accounts(), ["a@example.org", "b@example.org"])


class RunScriptTests(IsolatedMailCase):
    SCRIPT = str(mail.SCRIPTS_DIR / "account_present.applescript")

    def test_address_is_passed_as_argument_not_spliced_into_script_text(self):
        with mock.patch("mailbridge.mail.subprocess.run", return_value=_completed("absent\n")) as run:
            mail.run_script("account_present", "a@example.org")
        self.assertEqual(run.call_args[0][0], [mail.OSASCRIPT, self.SCRIPT, "a@example.org"])
        self.assertNotIn("a@example.org", Path(self.SCRIPT).read_text(encoding="utf-8"))

    def test_returns_stripped_stdout(self):
        with mock.patch("mailbridge.mail.subprocess.run", return_value=_completed("  present_enabled\n")):
            self.assertEqual(mail.run_script("account_present", "a@example.org"), "present_enabled")

    def test_automation_denied_error_mentions_automation(self):
        failed = _completed(stderr="execution error: Not authorized. (-1743)", returncode=1)
        with mock.patch("mailbridge.mail.subprocess.run", return_value=failed):
            with self.assertRaises(mail.MailError) as ctx:
                mail.run_script("account_present", "a@example.org")
        self.assertIn("Automation", str(ctx.exception))

    def test_mail_not_running_error_says_not_running(self):
        failed = _completed(stderr="execution error: Application isn't running. (-600)", returncode=1)
        with mock.patch("mailbridge.mail.subprocess.run", return_value=failed):
            with self.assertRaises(mail.MailError) as ctx:
                mail.run_script("account_present", "a@example.org")
        self.assertIn("not running", str(ctx.exception))

    def test_other_failure_gives_generic_mail_error(self):
        failed = _completed(stderr="execution error: Something odd. (-2700)", returncode=1)
        with mock.patch("mailbridge.mail.subprocess.run", return_value=failed):
            with self.assertRaises(mail.MailError) as ctx:
                mail.run_script("account_present", "a@example.org")
        self.assertIn("failed", str(ctx.exception))
        self.assertNotIn("Automation", str(ctx.exception))
        self.assertNotIn("not running", str(ctx.exception))

    def test_timeout_error_mentions_seconds(self):
        timeout = subprocess.TimeoutExpired(["osascript"], 30)
        with mock.patch("mailbridge.mail.subprocess.run", side_effect=timeout):
            with self.assertRaises(mail.MailError) as ctx:
                mail.run_script("account_present", "a@example.org")
        self.assertIn("seconds", str(ctx.exception))


class AccountPresentOperationTests(IsolatedMailCase):
    def setUp(self):
        super().setUp()
        _write_settings(self.settings, {"allowed_accounts": ["a@example.org"]})

    def _run_with_answer(self, answer):
        with mock.patch("mailbridge.mail.run_script", return_value=answer) as script:
            result = operations.run("account_present", {})
        return result, script

    def test_present_enabled(self):
        result, _ = self._run_with_answer("present_enabled")
        self.assertEqual(result, {
            "mail_running": True,
            "accounts": [{"account": "a@example.org", "present": True, "enabled": True}],
        })

    def test_present_disabled(self):
        result, _ = self._run_with_answer("present_disabled")
        self.assertEqual(result, {
            "mail_running": True,
            "accounts": [{"account": "a@example.org", "present": True, "enabled": False}],
        })

    def test_absent(self):
        result, _ = self._run_with_answer("absent")
        self.assertEqual(result, {
            "mail_running": True,
            "accounts": [{"account": "a@example.org", "present": False, "enabled": None}],
        })

    def test_not_running(self):
        result, _ = self._run_with_answer("not_running")
        self.assertEqual(result, {"mail_running": False, "accounts": []})

    def test_not_running_stops_after_first_probe(self):
        _write_settings(self.settings, {"allowed_accounts": ["a@example.org", "b@example.org"]})
        result, script = self._run_with_answer("not_running")
        self.assertEqual(result, {"mail_running": False, "accounts": []})
        self.assertEqual(script.call_count, 1)

    def test_unexpected_answer_raises_operation_error(self):
        with self.assertRaises(operations.OperationError):
            self._run_with_answer("weird output")

    def test_missing_allowlist_raises_operation_error_and_never_probes_mail(self):
        self.settings.unlink()
        with mock.patch("mailbridge.mail.run_script") as script:
            with self.assertRaises(operations.OperationError):
                operations.run("account_present", {})
        script.assert_not_called()

    def test_unexpected_argument_raises_operation_error(self):
        with mock.patch("mailbridge.mail.run_script") as script:
            with self.assertRaises(operations.OperationError) as ctx:
                operations.run("account_present", {"account": "x"})
        self.assertIn("account", str(ctx.exception))
        script.assert_not_called()

    def test_result_only_contains_allowlisted_addresses(self):
        _write_settings(self.settings, {"allowed_accounts": ["  A@Example.org", "b@example.org"]})
        answers = {"a@example.org": "present_enabled", "b@example.org": "absent"}

        def fake_script(name, address):
            self.assertEqual(name, "account_present")
            return answers[address]  # KeyError for any address outside the allowlist

        with mock.patch("mailbridge.mail.run_script", side_effect=fake_script) as script:
            result = operations.run("account_present", {})
        self.assertEqual([c[0][1] for c in script.call_args_list], ["a@example.org", "b@example.org"])
        self.assertEqual([a["account"] for a in result["accounts"]], ["a@example.org", "b@example.org"])
        self.assertNotIn("other", json.dumps(result))


class AccountPresentScriptTests(unittest.TestCase):
    SCRIPT = PROJECT / "mailbridge" / "scripts" / "account_present.applescript"

    def test_script_file_exists(self):
        self.assertTrue(self.SCRIPT.is_file())

    def test_not_running_check_comes_before_any_tell_to_mail(self):
        text = self.SCRIPT.read_text(encoding="utf-8")
        check = text.find('application "Mail" is not running')
        tell = text.find('tell application "Mail"')
        self.assertNotEqual(check, -1)
        self.assertNotEqual(tell, -1)
        self.assertLess(check, tell)

    def test_script_never_activates_mail(self):
        self.assertNotIn("activate", self.SCRIPT.read_text(encoding="utf-8").lower())


class McpAccountPresentTests(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.settings = _isolate_mail(self)
        _write_settings(self.settings, {"allowed_accounts": ["a@example.org"]})

    def test_tools_call_account_present_over_live_socket_returns_allowlisted_only(self):
        with mock.patch("mailbridge.mail.run_script", return_value="absent") as script:
            resp = mcp_server.handle(_call_msg("mail_bridge_account_present"), socket_path=self.sock)
        result = resp["result"]
        self.assertIs(result["isError"], False, result)
        self.assertEqual(json.loads(result["content"][0]["text"]), {
            "mail_running": True,
            "accounts": [{"account": "a@example.org", "present": False, "enabled": None}],
        })
        script.assert_called_once_with("account_present", "a@example.org")


class LatestSubjectOperationTests(IsolatedMailCase):
    def setUp(self):
        super().setUp()
        _write_settings(self.settings, {"allowed_accounts": ["a@example.org"]})

    def _run(self, answer):
        with mock.patch("mailbridge.mail.run_script", return_value=answer) as script:
            result = operations.run("latest_subject", {})
        return result, script

    def test_ok_returns_subject_and_time_only(self):
        result, script = self._run("ok\x1eTest subject\x1e2025-01-02 03:04")
        script.assert_called_once_with("latest_subject", "a@example.org")
        self.assertEqual(result, {"mail_running": True, "accounts": [{
            "account": "a@example.org", "status": "ok",
            "subject": "Test subject", "received": "2025-01-02 03:04"}]})

    def test_empty_subject_is_allowed(self):
        result, _ = self._run("ok\x1e\x1e2025-01-02 03:04")
        self.assertEqual(result["accounts"][0]["subject"], "")

    def test_long_subject_is_capped(self):
        result, _ = self._run("ok\x1e" + "x" * 1000 + "\x1e2025-01-02 03:04")
        self.assertEqual(len(result["accounts"][0]["subject"]), 300)

    def test_status_answers(self):
        for status in ("absent", "no_inbox", "empty"):
            result, _ = self._run(status)
            self.assertEqual(result["accounts"][0]["status"], status)
            self.assertIsNone(result["accounts"][0]["subject"])

    def test_not_running(self):
        result, _ = self._run("not_running")
        self.assertEqual(result, {"mail_running": False, "accounts": []})

    def test_unexpected_answer_raises(self):
        for answer in ("garbage", "ok\x1eonly-two", "ok\x1ea\x1eb\x1ec"):
            with self.assertRaises(operations.OperationError):
                self._run(answer)

    def test_script_checks_running_before_tell_and_never_activates(self):
        text = (Path(mail.__file__).resolve().parent / "scripts" / "latest_subject.applescript").read_text()
        self.assertLess(text.index("is not running"), text.index('tell application "Mail"'))
        self.assertNotIn("activate", text)
        self.assertNotIn("set read status", text)


if __name__ == "__main__":
    unittest.main()
