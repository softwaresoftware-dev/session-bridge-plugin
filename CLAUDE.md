# session-bridge (plugin)

Channel MCP that lets Claude Code sessions message each other, plus the bundled bridge daemon they all register with.

## Shape

- `channel.mjs` — per-session MCP server. Picks a port, registers with the local bridge daemon at `127.0.0.1:8910`, surfaces `sessions`/`message`/`reply` tools.
- `daemon/` — the FastAPI bridge daemon. `daemon/app/` is the application package; `daemon/run.py` is the cross-platform entry point. `peers.py` provides Tailscale-aware host discovery for `/hosts` (the softwaresoftware resolver uses it to route capability requests across hosts).
- `skills/setup/SKILL.md` — `/session-bridge:setup`. Creates the venv, registers the daemon with `daemon-manager`, installs launchd/systemd/Task Scheduler autostart.
- `tests/` — manifest tests.

## Why both files exist

`channel.mjs` only runs *inside* a Claude session and only talks to the local daemon. The daemon is the per-host registry — every session's `channel.mjs` registers with it on boot, and senders POST through it to reach receivers. Without the daemon the mesh is offline; channel MCP loads fine but `sessions`/`message`/`reply` silently fail.

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

## HTTP API

The daemon is a name+id session registry. Endpoints:

- `GET /health` — `{ok, sessions}`.
- `GET /hosts` — every host in the mesh with the capabilities it advertises (self via tailscale/`SESSION_BRIDGE_HOST_NAME`, peers from `peers.json`). Consumed by the softwaresoftware resolver for cross-host capability routing.
- `GET /sessions` — all tracked sessions.
- `GET /sessions/{name_or_id}` — resolve one by UUID, UUID prefix, or name. Used by channel's 30s heartbeat to detect daemon restarts / port mismatch / forget; re-registers on any of those.
- `POST /register` — channel calls this on boot with `{session_id, pid, channel_port, name?}`.
- `POST /sessions/{name_or_id}/message` — sender → daemon → receiver's channel port.

Sessions are auto-discovered from `~/.claude/sessions/*.json`; a name is derived from the cwd basename unless the session registers a `SESSION_NAME`. Custom names persist across daemon restarts in `$SESSION_BRIDGE_STATE_DIR/names.json`.

If channel.mjs probes `:8910/health` on boot and gets nothing, the MCP `instructions` block adds a hint telling Claude to run `/session-bridge:setup` before trying mesh tools.

## History

v0.2.0 bundled the daemon + setup skill (previously the plugin shipped only `channel.mjs`). v0.3.0 cut the mesh down to the surface its consumers actually use: removed the unused `/spawn`, `/service`, `/name`, `/label`, `/broadcast` endpoints, dropped namespaces / labels / selectors, dropped cross-host message *forwarding* (kept `/hosts` capability discovery), and replaced KDE/Yakuake tab-title naming with cwd-basename naming.
