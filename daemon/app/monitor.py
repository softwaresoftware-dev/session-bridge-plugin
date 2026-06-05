"""Session state monitoring — tracks claude sessions and classifies state."""

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from app.discovery import DiscoveredSession, assign_names

STATE_DIR = Path(os.environ.get("SESSION_BRIDGE_STATE_DIR", str(Path.home() / ".local/share/session-bridge")))
NAMES_FILE = STATE_DIR / "names.json"


class SessionState(str, Enum):
    WORKING = "working"
    DEAD = "dead"


@dataclass
class TrackedSession:
    """A session being actively monitored."""
    session_id: str
    name: str
    pid: int
    project_dir: str
    channel_port: int | None = None
    state: SessionState = SessionState.WORKING
    last_change: float = field(default_factory=time.time)


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


class SessionMonitor:
    """Tracks and monitors all discovered sessions."""

    def __init__(self):
        self.sessions: dict[str, TrackedSession] = {}
        self._pending_channels: dict[str, int] = {}
        # Custom names (from SESSION_NAME at /register), keyed by session_id.
        # Persisted so name-addressing survives a daemon restart — channels
        # re-register on their heartbeat too, but this restores names the
        # instant a session is rediscovered, with no 30s gap.
        self._names: dict[str, str] = self._load_names()

    @staticmethod
    def _load_names() -> dict[str, str]:
        try:
            data = json.loads(NAMES_FILE.read_text())
            return {sid: name for sid, name in data.items() if isinstance(name, str)}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_names(self) -> None:
        try:
            NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
            NAMES_FILE.write_text(json.dumps(self._names, indent=2))
        except OSError:
            pass

    def update_sessions(self, discovered: list[DiscoveredSession]):
        """Sync tracked sessions with discovery results."""
        derived = assign_names(discovered)
        current_ids = {s.session_id for s in discovered}

        for sid in [sid for sid in self.sessions if sid not in current_ids]:
            del self.sessions[sid]

        for ds in discovered:
            name = self._names.get(ds.session_id) or derived[ds.session_id]
            if ds.session_id in self.sessions:
                tracked = self.sessions[ds.session_id]
                tracked.name = name
                tracked.pid = ds.pid
                tracked.project_dir = ds.cwd
            else:
                self.sessions[ds.session_id] = TrackedSession(
                    session_id=ds.session_id,
                    name=name,
                    pid=ds.pid,
                    project_dir=ds.cwd,
                    channel_port=self._pending_channels.pop(ds.session_id, None),
                )

    def poll_all(self):
        """Poll all tracked sessions and update their state."""
        for session in self.sessions.values():
            session.state = SessionState.WORKING if _is_pid_alive(session.pid) else SessionState.DEAD

    def register_channel(self, session_id: str, channel_port: int, name: str | None = None) -> bool:
        """Register a channel port for a session. Optionally set its name."""
        if name:
            self._names[session_id] = name
            self._save_names()
        session = self.sessions.get(session_id)
        if session:
            session.channel_port = channel_port
            if name:
                session.name = name
            return True
        self._pending_channels[session_id] = channel_port
        return False

    def find_by_name(self, name: str) -> list[TrackedSession]:
        """Return all sessions whose name matches."""
        return [s for s in self.sessions.values() if s.name == name]

    def get_session_by_id(self, session_id: str) -> TrackedSession | None:
        return self.sessions.get(session_id)

    def list_sessions(self) -> list[TrackedSession]:
        return list(self.sessions.values())
