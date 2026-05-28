# session-bridge (plugin)

Channel MCP that lets Claude Code sessions message each other, plus the bundled bridge daemon they all register with.

## Shape

- `channel.mjs` — per-session MCP server. Picks a port, registers with the local bridge daemon at `127.0.0.1:8910`, surfaces `sessions`/`message`/`reply`/`label`/`broadcast` tools.
- `daemon/` — the FastAPI bridge daemon. `daemon/app/` is the application package; `daemon/run.py` is the cross-platform entry point.
- `skills/setup/SKILL.md` — `/session-bridge:setup`. Creates the venv, registers the daemon with `daemon-manager`, installs launchd/systemd/Task Scheduler autostart.
- `tests/` — manifest tests.

## Why both files exist

`channel.mjs` only runs *inside* a Claude session and only talks to the local daemon. The daemon is the per-host registry — every session's `channel.mjs` registers with it on boot, and senders POST through it to reach receivers. Without the daemon the mesh is offline; channel MCP loads fine but `sessions`/`message`/`reply` silently fail.

The plugin used to ship only `channel.mjs`, leaving operators to discover the missing daemon by themselves. v0.2.0 bundles the daemon and adds the setup skill.

## First install path

`/softwaresoftware:install session-bridge` pulls in `daemon` (daemon-manager) and surfaces `/session-bridge:setup` via the `post_install_skill` hint. The setup skill is what turns a fresh install into a running mesh.

## Capabilities

- Requires: `daemon` (via daemon-manager) — to register the bridge daemon as a reboot-persistent service.
- Provides: `session-mesh`.
- Environment: needs `python3` (3.11+) for the daemon.

## Where the daemon lives at runtime

After setup:
- Source: `${CLAUDE_PLUGIN_ROOT}/daemon/`
- venv: `${CLAUDE_PLUGIN_ROOT}/daemon/.venv/`
- PID/IPC: `~/.claude/daemons/session-bridge.{pid,sock,json}`
- launchd plist (macOS): `~/Library/LaunchAgents/com.claude.daemon.session-bridge.plist`
- systemd unit (Linux): `~/.config/systemd/user/session-bridge.service`
- Logs (macOS): `~/.claude/daemons/session-bridge.{out,err}.log`

## Channel ↔ daemon contract

See the daemon's own docs in `daemon/app/` for the HTTP API. Highlights:

- `POST /register` — channel calls this on boot with `{session_id, pid, channel_port, name?, namespace?, labels?}`.
- `GET /sessions/{name_or_id}` — used by channel's 30s heartbeat to detect daemon restarts / port mismatch / forget; re-registers when any of those happen.
- `POST /sessions/{name_or_id}/message` — sender → daemon → receiver's channel port.

If channel.mjs probes `:8910/health` on boot and gets nothing, the MCP `instructions` block adds a hint telling Claude to run `/session-bridge:setup` before trying mesh tools.
