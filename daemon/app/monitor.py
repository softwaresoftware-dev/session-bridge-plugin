"""Session state monitoring — tracks claude sessions and classifies state."""

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from app.discovery import DiscoveredSession, assign_names

STATE_DIR = Path(os.environ.get("SESSION_BRIDGE_STATE_DIR", str(Path.home() / ".local/share/session-bridge")))
META_FILE = STATE_DIR / "meta.json"
LEGACY_NAMES_FILE = STATE_DIR / "names.json"


class SessionState(str, Enum):
    WORKING = "working"
    IDLE = "idle"
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
    namespace: str | None = None
    labels: list[str] = field(default_factory=list)


def _is_pid_alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat"):
            return True
    except FileNotFoundError:
        return False


@dataclass
class _SessionMeta:
    """Persistent per-session metadata keyed by session_id."""
    name: str | None = None
    namespace: str | None = None
    labels: list[str] = field(default_factory=list)


class SessionMonitor:
    """Tracks and monitors all discovered sessions."""

    def __init__(self):
        self.sessions: dict[str, TrackedSession] = {}
        self._pending_channels: dict[str, int] = {}
        self._meta: dict[str, _SessionMeta] = self._load_meta()
        self.services: dict[str, TrackedSession] = {}  # standalone services (not claude sessions)
        self._last_persisted_meta: dict | None = None

    @staticmethod
    def _load_meta() -> dict[str, _SessionMeta]:
        try:
            data = json.loads(META_FILE.read_text())
            return {
                sid: _SessionMeta(
                    name=entry.get("name"),
                    namespace=entry.get("namespace"),
                    labels=list(entry.get("labels") or []),
                )
                for sid, entry in data.items()
            }
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        # One-time migration from the legacy names.json (string-keyed name map).
        try:
            legacy = json.loads(LEGACY_NAMES_FILE.read_text())
            migrated = {sid: _SessionMeta(name=name) for sid, name in legacy.items() if isinstance(name, str)}
            return migrated
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _serialize_meta(self) -> dict:
        return {
            sid: {
                k: v
                for k, v in {
                    "name": m.name,
                    "namespace": m.namespace,
                    "labels": m.labels or None,
                }.items()
                if v is not None
            }
            for sid, m in self._meta.items()
        }

    def _persist_meta(self) -> None:
        payload = self._serialize_meta()
        if payload == self._last_persisted_meta:
            return
        try:
            META_FILE.parent.mkdir(parents=True, exist_ok=True)
            META_FILE.write_text(json.dumps(payload, indent=2))
            self._last_persisted_meta = payload
        except OSError:
            pass

    def update_sessions(self, discovered: list[DiscoveredSession]):
        """Sync tracked sessions with discovery results."""
        names = assign_names(discovered)
        current_ids = {s.session_id for s in discovered}

        dead_ids = [sid for sid in self.sessions if sid not in current_ids]
        for sid in dead_ids:
            del self.sessions[sid]

        for ds in discovered:
            meta = self._meta.get(ds.session_id) or _SessionMeta()
            name = meta.name or names[ds.session_id]
            namespace = meta.namespace
            labels = list(meta.labels)
            if ds.session_id in self.sessions:
                tracked = self.sessions[ds.session_id]
                tracked.name = name
                tracked.pid = ds.pid
                tracked.project_dir = ds.cwd
                tracked.namespace = namespace
                tracked.labels = labels
            else:
                self.sessions[ds.session_id] = TrackedSession(
                    session_id=ds.session_id,
                    name=name,
                    pid=ds.pid,
                    project_dir=ds.cwd,
                    channel_port=self._pending_channels.pop(ds.session_id, None),
                    namespace=namespace,
                    labels=labels,
                )

    def poll_all(self):
        """Poll all tracked sessions and update their state."""
        for session in self.sessions.values():
            if not _is_pid_alive(session.pid):
                session.state = SessionState.DEAD
            else:
                session.state = SessionState.WORKING

    def register_service(
        self,
        name: str,
        channel_port: int,
        pid: int = 0,
        namespace: str | None = None,
        labels: list[str] | None = None,
    ) -> str:
        """Register a standalone service (not a claude session)."""
        service_id = f"service:{name}"
        self.services[service_id] = TrackedSession(
            session_id=service_id,
            name=name,
            pid=pid,
            project_dir="",
            channel_port=channel_port,
            state=SessionState.WORKING,
            namespace=namespace,
            labels=list(labels or []),
        )
        return service_id

    def set_name(
        self,
        session_id: str,
        name: str,
        namespace: str | None = None,
        labels: list[str] | None = None,
    ) -> bool:
        """Set custom metadata for a session. Persists to disk."""
        meta = self._meta.setdefault(session_id, _SessionMeta())
        meta.name = name
        if namespace is not None:
            meta.namespace = namespace
        if labels is not None:
            meta.labels = list(labels)
        self._persist_meta()
        session = self.sessions.get(session_id)
        if session:
            session.name = name
            if namespace is not None:
                session.namespace = namespace
            if labels is not None:
                session.labels = list(labels)
            return True
        return False

    def register_channel(
        self,
        session_id: str,
        channel_port: int,
        name: str | None = None,
        namespace: str | None = None,
        labels: list[str] | None = None,
    ) -> bool:
        """Register a channel port for a session. Optionally set name/namespace/labels atomically."""
        if name or namespace is not None or labels is not None:
            meta = self._meta.setdefault(session_id, _SessionMeta())
            if name:
                meta.name = name
            if namespace is not None:
                meta.namespace = namespace
            if labels is not None:
                meta.labels = list(labels)
            self._persist_meta()
        session = self.sessions.get(session_id)
        if session:
            session.channel_port = channel_port
            if name:
                session.name = name
            if namespace is not None:
                session.namespace = namespace
            if labels is not None:
                session.labels = list(labels)
            return True
        self._pending_channels[session_id] = channel_port
        return False

    def set_labels(self, session_id: str, labels: list[str]) -> bool:
        """Replace the label set on a session. Persists to disk."""
        meta = self._meta.setdefault(session_id, _SessionMeta())
        meta.labels = list(labels)
        self._persist_meta()
        session = self.sessions.get(session_id)
        if session:
            session.labels = list(labels)
            return True
        return False

    def find_by_name(self, name: str, namespace: str | None = None) -> list[TrackedSession]:
        """Return all sessions whose name matches, optionally narrowed by namespace."""
        matches: list[TrackedSession] = []
        for s in list(self.sessions.values()) + list(self.services.values()):
            if s.name != name:
                continue
            if namespace is not None and (s.namespace or "") != namespace:
                continue
            matches.append(s)
        return matches

    def find_by_selector(
        self,
        selector_terms: list[str],
        namespace: str | None = None,
    ) -> list[TrackedSession]:
        """Return all sessions whose labels include every selector term (AND).

        Each term is the literal label string (e.g. "kind:service"). Empty
        selector_terms returns every session, optionally narrowed by namespace.
        """
        matches: list[TrackedSession] = []
        for s in list(self.sessions.values()) + list(self.services.values()):
            if namespace is not None and (s.namespace or "") != namespace:
                continue
            session_labels = set(s.labels or [])
            if all(term in session_labels for term in selector_terms):
                matches.append(s)
        return matches

    def get_session_by_name(self, name: str) -> TrackedSession | None:
        """Legacy single-result lookup. Returns first match if unambiguous."""
        matches = self.find_by_name(name)
        return matches[0] if len(matches) == 1 else None

    def get_session_by_id(self, session_id: str) -> TrackedSession | None:
        return self.sessions.get(session_id) or self.services.get(session_id)

    def list_sessions(self) -> list[TrackedSession]:
        return list(self.sessions.values()) + list(self.services.values())
