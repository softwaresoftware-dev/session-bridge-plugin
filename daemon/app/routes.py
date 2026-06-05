"""API route definitions."""

import json
import urllib.request

from fastapi import APIRouter, HTTPException

from app.models import (
    HealthResponse,
    MessageRequest,
    RegisterRequest,
    SessionResponse,
)
from app.monitor import SessionMonitor, TrackedSession
from app.telemetry import send_event

router = APIRouter()

monitor: SessionMonitor | None = None


def _get_monitor() -> SessionMonitor:
    if monitor is None:
        raise HTTPException(status_code=503, detail="monitor not initialized")
    return monitor


def _resolve(name_or_id: str) -> TrackedSession:
    """Resolve a session by UUID, UUID prefix, or name.

    Errors:
      400 — prefix or name is ambiguous (matches more than one session).
      404 — no match.
    """
    m = _get_monitor()

    # UUID-exact and UUID-prefix first; UUIDs are globally unique so name
    # lookup only kicks in when there's no UUID match.
    if (s := m.get_session_by_id(name_or_id)):
        return s
    uuid_matches = [s for s in m.list_sessions() if s.session_id.startswith(name_or_id)]
    if len(uuid_matches) == 1:
        return uuid_matches[0]
    if len(uuid_matches) > 1:
        raise HTTPException(status_code=400, detail=f"ambiguous prefix '{name_or_id}' — matches {len(uuid_matches)} sessions")

    matches = m.find_by_name(name_or_id)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"ambiguous name '{name_or_id}' — matches {len(matches)} sessions; use a UUID",
        )
    raise HTTPException(status_code=404, detail=f"session '{name_or_id}' not found")


def _forward_to_channel(port: int, text: str, from_name: str | None = None, from_id: str | None = None) -> str:
    """POST a message to a session's channel HTTP server."""
    payload = json.dumps({"text": text, "from_name": from_name, "from_id": from_id})
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        data=payload.encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode()


def _to_response(s: TrackedSession) -> SessionResponse:
    return SessionResponse(
        name=s.name,
        session_id=s.session_id,
        project_dir=s.project_dir,
        claude_pid=s.pid,
        last_change=s.last_change,
        channel_port=s.channel_port,
    )


def _check_name_collision(name: str | None, owner_session_id: str) -> None:
    """Raise 409 if `name` is already registered to a different session."""
    if not name:
        return
    for ex in _get_monitor().find_by_name(name):
        if ex.session_id != owner_session_id:
            raise HTTPException(
                status_code=409,
                detail=f"name '{name}' already registered (session {ex.session_id[:8]})",
            )


@router.get("/health")
def health() -> HealthResponse:
    return HealthResponse(sessions=len(_get_monitor().list_sessions()))


@router.get("/sessions")
def list_sessions() -> list[SessionResponse]:
    return [_to_response(s) for s in _get_monitor().list_sessions()]


@router.get("/sessions/{name_or_id}")
def get_session(name_or_id: str) -> SessionResponse:
    return _to_response(_resolve(name_or_id))


@router.post("/register")
def register_channel(body: RegisterRequest) -> dict:
    _check_name_collision(body.name, body.session_id)
    existed = _get_monitor().register_channel(
        body.session_id,
        body.channel_port,
        body.pid,
        name=body.name,
        cwd=body.cwd or "",
    )
    send_event("channel_registered", port=body.channel_port, target_session=body.session_id, name=body.name)
    return {
        "registered": True,
        "session_id": body.session_id,
        "channel_port": body.channel_port,
        "name": body.name,
        "session_found": existed,
    }


@router.post("/sessions/{name_or_id}/message")
def send_message(name_or_id: str, body: MessageRequest) -> dict:
    session = _resolve(name_or_id)
    if session.channel_port is None:
        raise HTTPException(
            status_code=409,
            detail=f"session '{session.name}' has no channel — not started with the session-bridge channel loaded",
        )

    from_name = None
    from_id = body.from_session
    if from_id:
        sender = _get_monitor().get_session_by_id(from_id)
        if sender:
            from_name = sender.name

    try:
        result = _forward_to_channel(session.channel_port, body.text, from_name=from_name, from_id=from_id)
    except Exception as e:
        send_event("message_failed", error=str(e), to=session.name)
        raise HTTPException(status_code=502, detail=f"channel error: {e}")

    send_event("message_sent", to=session.name)
    return {"sent": body.text, "session": session.name, "session_id": session.session_id, "channel": result}
