"""Session registry — tracks channel-registered Claude sessions.

Registration is authoritative: a session exists here iff its `channel.mjs`
has POSTed `/register`. There is no background discovery — dead sessions
(pid gone) are reaped lazily whenever the registry is read.
"""

import os
import time
from dataclasses import dataclass, field


@dataclass
class TrackedSession:
    session_id: str
    name: str
    pid: int
    project_dir: str
    channel_port: int | None = None
    last_change: float = field(default_factory=time.time)


def _is_pid_alive(pid: int) -> bool:
    if not pid:
        return True  # nothing to check; keep the registration
    # Signal 0 probes existence without delivering a signal — portable across
    # Linux and macOS, unlike reading /proc (which doesn't exist on macOS).
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _basename(path: str) -> str:
    if not path:
        return "unknown"
    return os.path.basename(path.rstrip("/")) or "unknown"


class SessionMonitor:
    """In-memory registry of channel-registered sessions."""

    def __init__(self):
        self.sessions: dict[str, TrackedSession] = {}

    def _live(self) -> dict[str, TrackedSession]:
        """Drop sessions whose pid is dead, then return the survivors."""
        for sid in [sid for sid, s in self.sessions.items() if not _is_pid_alive(s.pid)]:
            del self.sessions[sid]
        return self.sessions

    def _derive_name(self, cwd: str, owner_session_id: str) -> str:
        """Name a session from its cwd basename, deduped against live peers."""
        base = _basename(cwd)
        taken = {s.name for sid, s in self._live().items() if sid != owner_session_id}
        if base not in taken:
            return base
        i = 2
        while f"{base}-{i}" in taken:
            i += 1
        return f"{base}-{i}"

    def register_channel(
        self,
        session_id: str,
        channel_port: int,
        pid: int,
        name: str | None = None,
        cwd: str = "",
    ) -> bool:
        """Create or update a session's registration.

        Returns True if this updated an existing registration (re-register),
        False if it created a new one. An explicit `name` always wins; on
        re-register without one, the existing name is kept.
        """
        existing = self._live().get(session_id)
        if existing:
            existing.channel_port = channel_port
            existing.pid = pid
            if cwd:
                existing.project_dir = cwd
            if name:
                existing.name = name
            return True
        self.sessions[session_id] = TrackedSession(
            session_id=session_id,
            name=name or self._derive_name(cwd, session_id),
            pid=pid,
            project_dir=cwd,
            channel_port=channel_port,
        )
        return False

    def find_by_name(self, name: str) -> list[TrackedSession]:
        return [s for s in self._live().values() if s.name == name]

    def get_session_by_id(self, session_id: str) -> TrackedSession | None:
        return self._live().get(session_id)

    def list_sessions(self) -> list[TrackedSession]:
        return list(self._live().values())
