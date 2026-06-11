# session-bridge (plugin)

Channel MCP that lets Claude Code sessions message each other, plus the bundled bridge daemon they all register with.

**Role in the mindframe stack:** session-bridge is the **Mesh** layer — agent↔agent↔human messaging on `127.0.0.1:8910`. It is also the **Agent runtime's delivery transport**: taskpilot reaches a spawned agent by POSTing to that agent's Mesh channel (`/sessions/<id>/message`), so the Mesh is how anything reaches a running agent. Standalone provider; mindframe and taskpilot are consumers.

## Shape

- `channel.mjs` — per-session MCP server. Picks a port, registers with the local bridge daemon at `127.0.0.1:8910`, surfaces `sessions`/`message`/`reply` tools.
- `daemon/` — the FastAPI bridge daemon. `daemon/app/` is the application package; `daemon/run.py` is the cross-platform entry point. Loopback-only (`127.0.0.1:8910`) — a single-host registry, no cross-host routing.
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

The daemon is a name+id session registry, loopback-only. Endpoints:

- `GET /health` — `{ok, sessions}`.
- `GET /sessions` — all tracked sessions.
- `GET /sessions/{name_or_id}` — resolve one by UUID, UUID prefix, or name. Used by channel's 30s heartbeat to detect daemon restarts / port mismatch / forget; re-registers on any of those.
- `POST /register` — channel calls this on boot with `{session_id, pid, channel_port, name?, cwd?}`.
- `POST /sessions/{name_or_id}/message` — sender → daemon → receiver's channel port. Delivery is verified end-to-end: the receiver's channel.mjs answers 503 when the MCP notification into claude fails or times out (3s), the daemon surfaces that as a 502 `channel error`, and callers (taskpilot's `/message`) propagate it — a message that didn't reach the agent never reads as sent.

**Registration is authoritative**: a session exists in the registry iff its `channel.mjs` has POSTed `/register` — there is no background discovery. A name is derived from the `cwd` basename (deduped) unless the session registers a `SESSION_NAME`. The registry is in-memory and not persisted: after a daemon restart it's empty until each channel re-registers on its next 30s heartbeat (the heartbeat GETs `/sessions/{id}` and re-registers on a 404 or a port mismatch). Dead sessions (pid gone) are reaped lazily whenever the registry is read.

If channel.mjs probes `:8910/health` on boot and gets nothing, the MCP `instructions` block adds a hint telling Claude to run `/session-bridge:setup` before trying mesh tools.

## History

v0.4.0 made delivery honest: channel.mjs used to return 200 even when its notification into claude failed or timed out, so a wedged agent silently swallowed messages; it now returns 503 and the failure propagates to the sender. v0.2.0 bundled the daemon + setup skill (previously the plugin shipped only `channel.mjs`). v0.3.0 cut the mesh down to a single-host name+id messaging registry: removed the unused `/spawn`, `/service`, `/name`, `/label`, `/broadcast` endpoints; dropped namespaces / labels / selectors; removed all cross-host functionality (`/hosts`, `peers.py`, Tailscale peer discovery) and bound the daemon to loopback; dropped the unused channel SSE `/events` stream; and replaced KDE/Yakuake tab-title naming with cwd-basename naming. It also made registration authoritative — deleting `~/.claude/sessions` discovery (`discovery.py`), the background poll loop, and the `names.json` persistence in favor of an in-memory registry driven entirely by `/register` with lazy pid-reaping.
