# Mac Mail Bridge

A small, read-only bridge that lets local AI agents (for example, the Codex agent in the ChatGPT desktop app, or Hermes Agent) work with Apple Mail on a Mac. They talk to it as an MCP tool server, and it keeps working while the screen is locked.

It solves a specific problem: sandboxed agents often can't drive Mail with AppleScript themselves. They either lack macOS Automation permission or are blocked from reaching Apple's scripting services. Here, one background helper holds the Mail permission, and every agent goes through it.

## How It Works

```
Agent A (e.g. Codex) ──┐                      ┌─> helper (mailbridge.daemon) ──> Apple Mail
                       ├─ mailbridge-mcp ─────┤     Unix socket, owner-only
Agent B (e.g. Hermes) ─┘   (MCP over stdio)   │     fixed operations only
```

- **Helper** (`bin/mailbridge-daemon`): the only program that talks to Mail. It runs as a per-user LaunchAgent and listens on `~/Library/Application Support/mac-mail-bridge/bridge.sock` (mode 0600, no network port). It runs one operation at a time.
- **Adapter** (`bin/mailbridge-mcp`): the MCP stdio server each agent launches. It has no Mail access; it only forwards calls to the helper.
- **Operations** (`mailbridge/operations.py`): a fixed, named list. There is no generic script or shell operation. Every Mail script takes its input as an argument, never pasted into the script text.
- **Account allowlist**: operations only touch the accounts listed in a local settings file.

Python standard library only. Runs on Apple's `/usr/bin/python3` (3.9+). No installs needed.

## Tools

| Tool | What it does |
|---|---|
| `mail_bridge_ping` | Checks the helper is running. Doesn't touch Mail. |
| `mail_bridge_account_present` | Reports whether each allowlisted account is set up and enabled in Mail. |
| `mail_bridge_latest_subject` | Returns the subject and received time of the newest Inbox message for each allowlisted account. No sender or body. |

All three are read-only. None of them launch Mail; if Mail isn't open, they say so.

## Setup

1. **Clone** the repo somewhere permanent. The background service runs from this folder.
2. **Allowlist your account(s):**

   ```bash
   mkdir -p ~/Library/Application\ Support/mac-mail-bridge && cp settings.example.json ~/Library/Application\ Support/mac-mail-bridge/settings.json && chmod 600 ~/Library/Application\ Support/mac-mail-bridge/settings.json
   ```

   Edit that file and list the email address(es) exactly as they appear in Mail.
3. **Install the background service:**

   ```bash
   bin/install-launchagent
   ```

4. **Grant Mail permission.** Open Mail, then run one check. macOS will ask whether "Python" may control "Mail". Click **Allow**.

   ```bash
   /usr/bin/python3 -c "from mailbridge import client; print(client.call('account_present'))"
   ```

5. **Connect your agents** (next section).

## Connecting Agents

**Codex / ChatGPT desktop app**: add this to `~/.codex/config.toml`:

```toml
[mcp_servers.mail_bridge]
command = "/path/to/mac-mail-bridge/bin/mailbridge-mcp"
args = []
startup_timeout_sec = 20
```

**Hermes Agent**:

```bash
hermes -p <profile> mcp add mail_bridge --command /path/to/mac-mail-bridge/bin/mailbridge-mcp
```

Other MCP clients work too: launch `bin/mailbridge-mcp` as a stdio server.

## Lessons Learned (Agent Quirks)

- **Codex desktop app hides MCP servers** behind one-line descriptions ("deferred namespaces") and skips servers with a blank description. That's why the adapter returns `instructions` on `initialize`.
- **Codex scheduled automations** at low reasoning effort may not find the tools on their own and fall back to computer use. Name the route in the automation prompt: use the `exec` tool and call `await tools.mcp__mail_bridge__mail_bridge_latest_subject({})`.
- **Hermes caches each server's tool list.** `/reload-mcp` reports "no server changes" when only a server's tools changed. To pick up new tools, run `hermes -p <profile> mcp remove mail_bridge`, then add it again; the gateway reconciles within seconds.
- **Mail and AppleScript:** copy Mail's lists with `get` and index into them. Iterating live references and coercing them to text fails with error -1700.
- **Unix socket paths** on macOS are limited to 103 bytes.

## Tested Behavior

- Works with the screen locked while the user stays signed in. Tested with direct calls, a Hermes agent, and a Codex scheduled automation.
- Doesn't work when logged out or after a restart before login, because the LaunchAgent only runs in a signed-in desktop session.

## Commands

```bash
/usr/bin/python3 -m unittest discover -s tests -v
```

Restart after code changes:

```bash
launchctl kickstart -k gui/$(id -u)/local.mac-mail-bridge
```

Remove the service (settings and logs are kept):

```bash
bin/uninstall-launchagent
```

Use `MAILBRIDGE_SOCKET=<path>` and `MAILBRIDGE_SETTINGS=<path>` to run against test locations. Helper log: `~/Library/Logs/mac-mail-bridge/helper.log`.

## Adding Operations

Add a named function and schema to `mailbridge/operations.py`, plus a script in `mailbridge/scripts/` that takes its inputs as arguments. Then add tests that patch `mailbridge.mail.run_script`; the suite fails if any test runs `osascript` for real. Keep operations narrow, and design write operations (moving, deleting, sending) with explicit safeguards.

## License

MIT
