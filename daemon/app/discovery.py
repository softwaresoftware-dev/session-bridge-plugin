"""Discover Claude Code sessions from ~/.claude/sessions/*.json."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

SESSIONS_DIR = Path.home() / ".claude" / "sessions"


@dataclass
class DiscoveredSession:
    session_id: str
    pid: int
    cwd: str
    started_at: int


def _is_pid_alive(pid: int) -> bool:
    # Signal 0 probes existence without delivering a signal — portable across
    # Linux and macOS, unlike reading /proc (which doesn't exist on macOS).
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _name_from_path(path: str) -> str:
    if not path:
        return "unknown"
    return os.path.basename(path.rstrip("/")) or "unknown"


def discover_sessions() -> list[DiscoveredSession]:
    if not SESSIONS_DIR.is_dir():
        return []

    sessions = []
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        pid = data.get("pid")
        session_id = data.get("sessionId")
        if not pid or not session_id:
            continue

        if not _is_pid_alive(pid):
            continue

        sessions.append(DiscoveredSession(
            session_id=session_id,
            pid=pid,
            cwd=data.get("cwd", ""),
            started_at=data.get("startedAt", 0),
        ))

    return sessions


def assign_names(sessions: list[DiscoveredSession]) -> dict[str, str]:
    """Derive a name per session from its cwd basename, deduped with a counter.

    Ordered by start time so the numbering is stable as sessions come and go.
    """
    names: dict[str, str] = {}
    name_counts: dict[str, int] = {}

    for s in sorted(sessions, key=lambda s: s.started_at):
        base = _name_from_path(s.cwd)
        count = name_counts.get(base, 0) + 1
        name_counts[base] = count
        names[s.session_id] = base if count == 1 else f"{base}-{count}"

    return names
