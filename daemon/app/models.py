"""Pydantic models for the API."""

from pydantic import BaseModel


class SessionResponse(BaseModel):
    name: str
    session_id: str
    state: str
    project_dir: str
    claude_pid: int
    last_change: float
    channel_port: int | None = None
    namespace: str | None = None
    labels: list[str] = []


class MessageRequest(BaseModel):
    text: str
    from_session: str | None = None


class NameRequest(BaseModel):
    session_id: str
    name: str
    namespace: str | None = None
    labels: list[str] | None = None


class RegisterRequest(BaseModel):
    session_id: str
    pid: int
    channel_port: int
    name: str | None = None
    namespace: str | None = None
    labels: list[str] | None = None


class ServiceRegisterRequest(BaseModel):
    name: str
    channel_port: int
    pid: int = 0
    namespace: str | None = None
    labels: list[str] | None = None


class LabelRequest(BaseModel):
    session_id: str
    labels: list[str]


class BroadcastRequest(BaseModel):
    text: str
    selector: str | None = None
    namespace: str | None = None
    from_session: str | None = None


class HealthResponse(BaseModel):
    ok: bool = True
    sessions: int = 0


class HostResponse(BaseModel):
    host: str
    self: bool
    capabilities: list[str] = []
    ip: str | None = None
    port: int | None = None


class SpawnRequest(BaseModel):
    name: str
    namespace: str | None = None
    labels: list[str] | None = None
    cwd: str | None = None
    plugin_dirs: list[str] | None = None
    channels: list[str] | None = None
    model: str | None = None
    settings: str | None = None
    initial_message: str | None = None
    timeout_seconds: int = 30


class SpawnResponse(BaseModel):
    spawned: bool
    name: str
    namespace: str | None = None
    session_id: str | None = None
    tmux_session: str
    initial_message_delivered: bool = False
