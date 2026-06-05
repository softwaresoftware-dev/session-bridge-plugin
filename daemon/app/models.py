"""Pydantic models for the API."""

from pydantic import BaseModel


class SessionResponse(BaseModel):
    name: str
    session_id: str
    project_dir: str
    claude_pid: int
    last_change: float
    channel_port: int | None = None


class MessageRequest(BaseModel):
    text: str
    from_session: str | None = None


class RegisterRequest(BaseModel):
    session_id: str
    pid: int
    channel_port: int
    name: str | None = None
    cwd: str | None = None


class HealthResponse(BaseModel):
    ok: bool = True
    sessions: int = 0
