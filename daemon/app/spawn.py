"""Local Claude session spawning — invoked by the /spawn route.

The /spawn endpoint takes a request to start a Claude session on this
host and returns once the new session has registered with the local
session-bridge daemon. This module handles command construction and
the post-registration plumbing; the route in routes.py owns the
subprocess + monitor wiring.

Spawn is taskpilot-agnostic. It launches a vanilla Claude session with
session-bridge as a dev channel; lifecycle (Stop/Notification hooks,
respawn, completion classifiers) is the caller's concern. Taskpilot
adds those on top via --settings + a hook-settings.json path.
"""

from __future__ import annotations

import os

# Daemons in different environments load the channel differently. The
# default below works on hosts where session-bridge is installed via the
# softwaresoftware marketplace. Hosts without the marketplace (e.g.
# Termux) override via env var to point at a local server entry, e.g.
# `server:session-bridge` resolved from ~/.claude.json mcpServers.
DEFAULT_CHANNEL = os.environ.get(
    "SESSION_BRIDGE_SPAWN_DEFAULT_CHANNEL",
    "plugin:session-bridge@softwaresoftware-plugins",
)


def build_spawn_command(
    *,
    name: str,
    namespace: str | None = None,
    labels: list[str] | None = None,
    plugin_dirs: list[str] | None = None,
    channels: list[str] | None = None,
    model: str | None = None,
    settings: str | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Build (env, argv) for the spawned tmux session.

    Returns (env_dict, claude_argv). The caller passes env via
    `tmux new-session -e KEY=val` flags and the start directory via `-c`.
    Returning structured values rather than a bash string means none of
    the inputs flow through a shell — no quoting, no escapes, no
    injection from a `name` or `cwd` containing shell metacharacters.

    `SESSION_NAME`, `SESSION_NAMESPACE`, and `SESSION_LABELS` are read by
    session-bridge's channel.mjs at registration time.
    """
    env: dict[str, str] = {"SESSION_NAME": name}
    if namespace:
        env["SESSION_NAMESPACE"] = namespace
    if labels:
        env["SESSION_LABELS"] = ",".join(labels)

    all_channels = [DEFAULT_CHANNEL]
    for ch in channels or []:
        if ch not in all_channels:
            all_channels.append(ch)

    argv: list[str] = [
        "claude",
        "--dangerously-skip-permissions",
        "--dangerously-load-development-channels",
        *all_channels,
        "--name", name,
    ]
    if settings:
        argv += ["--settings", settings]
    for p in plugin_dirs or []:
        argv += ["--plugin-dir", p]
    if model:
        argv += ["--model", model]

    return env, argv


def tmux_session_name(name: str, namespace: str | None = None) -> str:
    """Return a unique tmux session name for the spawn.

    Uses `--` to separate name from namespace because tmux treats `.`
    in `-t <target>` syntax as a `session.window` selector — sessions
    named `foo.bar` can't be targeted as `-t foo.bar`. Prefixed with
    `spawn-` so it's easy to grep / distinguish from taskpilot sessions.
    """
    suffix = f"{name}--{namespace}" if namespace else name
    return f"spawn-{suffix}"
