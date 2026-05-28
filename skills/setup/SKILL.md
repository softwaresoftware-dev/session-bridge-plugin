---
name: setup
description: Install and start the session-bridge daemon as a reboot-persistent service. Use when asked to "set up session-bridge", "fix session mesh", "start the bridge daemon", or after first install when channels can't reach a daemon at port 8910.
---

# session-bridge setup

Bring up the session-bridge daemon on this machine and make it survive reboot. Channel MCPs (channel.mjs) talk to the daemon at `127.0.0.1:8910`; without the daemon, the mesh is dead.

## Workflow

1. **Locate the bundled daemon.** It lives at `${CLAUDE_PLUGIN_ROOT}/daemon`. Set `DAEMON_DIR="${CLAUDE_PLUGIN_ROOT}/daemon"`.

2. **Find Python ≥3.11.** Run `python3 --version`. If missing or older than 3.11, tell the user to install Python 3.11+ and stop.

3. **Create the venv if needed.**

   ```bash
   if [ ! -x "$DAEMON_DIR/.venv/bin/python" ] && [ ! -x "$DAEMON_DIR/.venv/Scripts/python.exe" ]; then
     python3 -m venv "$DAEMON_DIR/.venv"
     "$DAEMON_DIR/.venv/bin/pip" install --quiet -U pip
     "$DAEMON_DIR/.venv/bin/pip" install --quiet 'fastapi>=0.115.0' 'uvicorn[standard]>=0.30.0'
   fi
   ```

   On Windows the python path is `$DAEMON_DIR/.venv/Scripts/python.exe`; use that instead.

4. **Register the daemon with daemon-manager.** Call:

   ```
   daemon_start(
       daemon_name="session-bridge",
       command="<absolute path to .venv python>",
       args=["<absolute path to daemon/run.py>"],
       cwd="<absolute path to daemon>",
   )
   ```

   Confirm `status` is `started` or `already_running`. The daemon listens on `127.0.0.1:8910` (override with `SESSION_BRIDGE_PORT`).

5. **Install autostart.** Call:

   ```
   daemon_install_autostart(daemon_name="session-bridge")
   ```

   This installs the launchd plist / systemd unit / Scheduled Task using the config saved by step 4. Confirm `status` is `installed`. If `installed_not_loaded`, surface the error and stop.

6. **Verify the mesh.** Use the session-bridge MCP `sessions` tool to list registered sessions. You should at least see this session itself. If the list is empty *and* this session was started without `--dangerously-load-development-channels`, tell the user — channel.mjs only registers when the channel MCP is loaded.

7. **Probe health.**

   ```bash
   curl -sf http://127.0.0.1:8910/health
   ```

   Expect a 200. If the daemon is up but `/health` fails, check `~/.claude/daemons/session-bridge.err.log` (on macOS) or `systemctl --user status session-bridge` (on Linux).

## Troubleshooting

- **`daemon_start` returns `failed` with "Daemon process exited immediately"** — open `~/.claude/daemons/session-bridge.err.log` (Mac) or run the command from step 4 by hand to see the traceback. Common cause: venv missing uvicorn — re-run step 3 with `--force-reinstall`.
- **Port 8910 already bound by something else** — set `SESSION_BRIDGE_PORT` in the env passed to `daemon_start` and tell the user; channel.mjs hardcodes 8910, so this is a workaround, not a fix.
- **Autostart fails to load on macOS** — `launchctl bootstrap` permission issues are common when SIP or MDM profiles restrict LaunchAgents. Surface the `error` field from `daemon_install_autostart` to the user.
