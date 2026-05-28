"""API route definitions."""

import functools
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException

from app import peers, spawn
from app.models import (
    BroadcastRequest,
    HealthResponse,
    HostResponse,
    LabelRequest,
    MessageRequest,
    NameRequest,
    RegisterRequest,
    ServiceRegisterRequest,
    SessionResponse,
    SpawnRequest,
    SpawnResponse,
)
from app.monitor import SessionMonitor, TrackedSession
from app.peers import Peer
from app.telemetry import send_event

router = APIRouter()

monitor: SessionMonitor | None = None


def _get_monitor() -> SessionMonitor:
    if monitor is None:
        raise HTTPException(status_code=503, detail="monitor not initialized")
    return monitor


def _parse_selector(name_or_id: str) -> tuple[str, str | None]:
    """Split a 'name.namespace' selector. Returns (name, namespace_or_None).

    Bare 'name' returns (name, None) — caller treats None as "any namespace,
    must be unambiguous". Explicit empty namespace 'name.' is preserved as ''
    so callers can address the no-namespace bucket.
    """
    if "." in name_or_id:
        name, _, ns = name_or_id.rpartition(".")
        if name:
            return name, ns
    return name_or_id, None


@functools.lru_cache(maxsize=1)
def _self_host() -> str | None:
    """Cached canonical Tailscale hostname for this daemon, or None."""
    return peers.get_self_host()


@functools.lru_cache(maxsize=1)
def _peers_index() -> dict[str, Peer]:
    """Cached {hostname: Peer} for everything in peers.json."""
    return {p.host: p for p in peers.load_peers()}


def _resolve_target(name_or_id: str) -> tuple[Peer | None, str]:
    """Classify a session address. Returns (peer_or_None, local_part).

    - 1- or 2-part address: returns (None, name_or_id) → resolve here.
    - 3-part FQDN whose host matches self: returns (None, name.namespace) → resolve here.
    - 3-part FQDN whose host is in peers.json: returns (peer, name.namespace) → forward.
    - 3-part FQDN whose host is unknown: raises 404.
    - Malformed FQDN (empty name): raises 400.
    """
    parts = name_or_id.split(".")
    if len(parts) < 3:
        return None, name_or_id
    name, namespace, host = parts[0], parts[1], parts[2]
    if not name:
        raise HTTPException(status_code=400, detail=f"malformed address '{name_or_id}' — empty name")
    local_part = f"{name}.{namespace}" if namespace else name
    if not host or host == _self_host():
        return None, local_part
    peer = _peers_index().get(host)
    if not peer:
        raise HTTPException(
            status_code=404,
            detail=f"unknown host '{host}' — not in peers.json",
        )
    return peer, local_part


def _forward_to_peer(peer: Peer, path: str, method: str = "GET", json_body=None) -> tuple[str, int]:
    """Proxy an HTTP request to a peer daemon. Returns (body, status_code).

    Network errors map to 502; HTTPError (non-2xx with body) is returned as-is
    so the caller can surface the peer's detail to the user.
    """
    url = f"{peer.url}{path}"
    headers = {"Accept": "application/json"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode(), resp.status
    except urllib.error.HTTPError as e:
        return e.read().decode(), e.code
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"peer {peer.host} unreachable: {e.reason}")


def _proxy_response(body: str, status: int) -> dict:
    """Decode a peer JSON response, raising HTTPException on non-2xx."""
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail=f"peer returned non-JSON: {body[:200]}")
    if status >= 400:
        detail = decoded.get("detail") if isinstance(decoded, dict) else body
        raise HTTPException(status_code=status, detail=detail)
    return decoded


def _validate_label(term: str, *, kind: str = "label") -> None:
    """Reject a label or selector term that isn't 'key:value'."""
    if ":" not in term:
        raise HTTPException(
            status_code=400,
            detail=f"invalid {kind} '{term}' — expected 'key:value'",
        )


def _parse_label_selector(selector: str | None) -> list[str]:
    """Parse a comma-separated label selector. Each term must be 'key:value'.

    Empty input returns []. Whitespace around commas and within terms is
    trimmed. Raises 400 on any malformed term so callers see typos early.
    """
    if not selector:
        return []
    terms: list[str] = []
    for raw in selector.split(","):
        term = raw.strip()
        if not term:
            continue
        _validate_label(term, kind="selector term")
        terms.append(term)
    return terms


def _candidates_label(matches: list[TrackedSession]) -> str:
    return ", ".join(
        f"{m.name}.{m.namespace}" if m.namespace else f"{m.name} (no namespace)"
        for m in matches
    )


def _resolve(name_or_id: str) -> TrackedSession:
    """Resolve a session by 'name', 'name.namespace', UUID, or UUID prefix.

    Errors:
      400 — bare 'name' is ambiguous across multiple namespaces.
      404 — no match.
    """
    m = _get_monitor()

    # Try UUID-exact and UUID-prefix first; UUIDs are globally unique so
    # "name.namespace" parsing only kicks in if no UUID match exists.
    if (s := m.get_session_by_id(name_or_id)):
        return s
    uuid_matches = [s for s in m.list_sessions() if s.session_id.startswith(name_or_id)]
    if len(uuid_matches) == 1:
        return uuid_matches[0]
    if len(uuid_matches) > 1:
        raise HTTPException(status_code=400, detail=f"ambiguous prefix '{name_or_id}' — matches {len(uuid_matches)} sessions")

    name, namespace = _parse_selector(name_or_id)

    if namespace is not None:
        # Explicit namespace selector — exact match required.
        scoped = m.find_by_name(name, namespace=namespace)
        if len(scoped) == 1:
            return scoped[0]
        if len(scoped) > 1:
            raise HTTPException(
                status_code=400,
                detail=f"ambiguous '{name_or_id}' — multiple sessions match (use UUID): {_candidates_label(scoped)}",
            )
        raise HTTPException(status_code=404, detail=f"session '{name_or_id}' not found")

    # Bare name lookup.
    matches = m.find_by_name(name)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                f"ambiguous name '{name}' — matches {len(matches)} sessions across namespaces. "
                f"Disambiguate with name.namespace: {_candidates_label(matches)}"
            ),
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
        state=s.state.value,
        project_dir=s.project_dir,
        claude_pid=s.pid,
        last_change=s.last_change,
        channel_port=s.channel_port,
        namespace=s.namespace,
        labels=list(s.labels or []),
    )


@router.get("/health")
def health() -> HealthResponse:
    m = _get_monitor()
    return HealthResponse(sessions=len(m.list_sessions()))


@router.get("/hosts")
def list_hosts() -> list[HostResponse]:
    """List every host in the mesh with the capabilities it advertises.

    Self appears first when the daemon can identify itself (via tailscale or
    SESSION_BRIDGE_HOST_NAME). Peers come from peers.json. Unreachable peers
    (declared but not currently in the tailnet) are omitted — the registry
    surfaces only hosts the resolver could plausibly dispatch to right now.
    Liveness beyond reachability (peer daemon up?) is checked at spawn time.
    """
    out: list[HostResponse] = []
    self_host = peers.get_self_host()
    if self_host:
        out.append(HostResponse(
            host=self_host,
            self=True,
            capabilities=peers.get_self_capabilities(),
            ip=peers.get_self_ip(),
            port=peers.DEFAULT_PEER_PORT,
        ))
    for p in peers.load_peers():
        out.append(HostResponse(
            host=p.host,
            self=False,
            capabilities=list(p.capabilities),
            ip=p.ip,
            port=p.port,
        ))
    return out


@router.get("/sessions")
def list_sessions(
    namespace: str | None = None,
    selector: str | None = None,
) -> list[SessionResponse]:
    """List sessions, optionally narrowed by namespace and/or label selector."""
    terms = _parse_label_selector(selector)
    sessions = _get_monitor().find_by_selector(terms, namespace=namespace)
    return [_to_response(s) for s in sessions]


@router.get("/sessions/{name_or_id}")
def get_session(name_or_id: str) -> SessionResponse:
    peer, local = _resolve_target(name_or_id)
    if peer:
        body, status = _forward_to_peer(peer, f"/sessions/{urllib.parse.quote(local)}")
        return SessionResponse(**_proxy_response(body, status))
    return _to_response(_resolve(local))


def _check_name_collision(name: str | None, namespace: str | None, owner_session_id: str) -> None:
    """Raise 409 if (name, namespace) is already taken by a different session."""
    if not name:
        return
    for ex in _get_monitor().find_by_name(name, namespace=namespace):
        if ex.session_id != owner_session_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"name '{name}' already registered in namespace "
                    f"'{namespace or ''}' (session {ex.session_id[:8]})"
                ),
            )


@router.post("/register")
def register_channel(body: RegisterRequest) -> dict:
    _check_name_collision(body.name, body.namespace, body.session_id)
    found = _get_monitor().register_channel(
        body.session_id,
        body.channel_port,
        body.name,
        namespace=body.namespace,
        labels=body.labels,
    )
    send_event("channel_registered", port=body.channel_port, target_session=body.session_id, name=body.name, namespace=body.namespace)
    return {
        "registered": True,
        "session_id": body.session_id,
        "channel_port": body.channel_port,
        "name": body.name,
        "namespace": body.namespace,
        "labels": body.labels or [],
        "session_found": found,
    }


@router.post("/service")
def register_service(body: ServiceRegisterRequest) -> dict:
    _check_name_collision(body.name, body.namespace, f"service:{body.name}")
    service_id = _get_monitor().register_service(body.name, body.channel_port, body.pid, namespace=body.namespace, labels=body.labels)
    send_event("service_registered", name=body.name, port=body.channel_port, namespace=body.namespace)
    return {
        "registered": True,
        "service_id": service_id,
        "name": body.name,
        "channel_port": body.channel_port,
        "namespace": body.namespace,
        "labels": body.labels or [],
    }


@router.post("/name")
def set_name(body: NameRequest) -> dict:
    _check_name_collision(body.name, body.namespace, body.session_id)
    found = _get_monitor().set_name(body.session_id, body.name, namespace=body.namespace, labels=body.labels)
    return {
        "named": True,
        "session_id": body.session_id,
        "name": body.name,
        "namespace": body.namespace,
        "labels": body.labels or [],
        "session_found": found,
    }


@router.post("/label")
def set_labels(body: LabelRequest) -> dict:
    """Replace the labels on a session."""
    for term in body.labels:
        _validate_label(term)
    found = _get_monitor().set_labels(body.session_id, body.labels)
    return {
        "labeled": True,
        "session_id": body.session_id,
        "labels": body.labels,
        "session_found": found,
    }


@router.post("/broadcast")
def broadcast(body: BroadcastRequest) -> dict:
    """Send a message to every session matching the selector + namespace.

    Returns per-recipient delivery status. A recipient with no channel is
    skipped (status='skipped'); a forwarding error is reported (status='error').
    """
    m = _get_monitor()
    terms = _parse_label_selector(body.selector)
    targets = m.find_by_selector(terms, namespace=body.namespace)

    from_name = None
    from_id = body.from_session
    if from_id:
        sender = m.get_session_by_id(from_id)
        if sender:
            from_name = sender.name

    deliveries = []
    for s in targets:
        if s.session_id == from_id:
            continue
        if s.channel_port is None:
            deliveries.append({"name": s.name, "session_id": s.session_id, "status": "skipped", "reason": "no-channel"})
            continue
        try:
            _forward_to_channel(s.channel_port, body.text, from_name=from_name, from_id=from_id)
            deliveries.append({"name": s.name, "session_id": s.session_id, "status": "sent"})
        except Exception as e:
            deliveries.append({"name": s.name, "session_id": s.session_id, "status": "error", "reason": str(e)})

    sent = sum(1 for d in deliveries if d["status"] == "sent")
    send_event("broadcast", selector=body.selector, namespace=body.namespace, matched=len(targets), sent=sent)
    return {
        "selector": body.selector,
        "namespace": body.namespace,
        "matched": len(targets),
        "sent": sent,
        "deliveries": deliveries,
    }


@router.post("/spawn")
def spawn_session(body: SpawnRequest) -> SpawnResponse:
    """Start a Claude session on this host and wait for it to register.

    Cross-host spawning works by addressing this endpoint on the target
    host's daemon. Local taskpilot forwards `host=<name>` create_task
    requests here; the spawned agent then registers with this daemon
    and is mesh-addressable as `<name>.<namespace>.<host>`.

    Returns 200 with the new session info when registration succeeds,
    409 if (name, namespace) is already taken,
    502 if the tmux invocation fails,
    504 if the agent doesn't register within `timeout_seconds`.
    """
    m = _get_monitor()
    _check_name_collision(body.name, body.namespace, owner_session_id="<new-spawn>")

    env, argv = spawn.build_spawn_command(
        name=body.name,
        namespace=body.namespace,
        labels=body.labels,
        plugin_dirs=body.plugin_dirs,
        channels=body.channels,
        model=body.model,
        settings=body.settings,
    )
    tmux_session = spawn.tmux_session_name(body.name, body.namespace)

    # Build the tmux invocation as a pure argv list. cwd goes through
    # `tmux -c`, env vars through `-e KEY=val`, and the claude argv is
    # appended directly — nothing runs through a shell, so name/cwd/etc.
    # cannot inject commands.
    tmux_cmd: list[str] = ["tmux", "new-session", "-d", "-s", tmux_session]
    if body.cwd:
        tmux_cmd += ["-c", body.cwd]
    for k, v in env.items():
        tmux_cmd += ["-e", f"{k}={v}"]
    tmux_cmd += argv

    result = subprocess.run(tmux_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        send_event("spawn_failed", reason="tmux", name=body.name, stderr=result.stderr[:200])
        raise HTTPException(status_code=502, detail=f"tmux new-session failed: {result.stderr.strip() or 'no stderr'}")

    # Poll the monitor for the agent's channel registration. The agent's
    # channel.mjs registers via /register on its own once claude boots and
    # the MCP starts; we just wait for the row to appear.
    deadline = time.monotonic() + body.timeout_seconds
    registered: TrackedSession | None = None
    while time.monotonic() < deadline:
        matches = m.find_by_name(body.name, namespace=body.namespace)
        if matches:
            registered = matches[0]
            break
        time.sleep(1)

    if registered is None:
        send_event("spawn_failed", reason="timeout", name=body.name, tmux_session=tmux_session)
        raise HTTPException(
            status_code=504,
            detail=(
                f"agent '{body.name}' did not register within {body.timeout_seconds}s. "
                f"tmux session '{tmux_session}' may still be coming up — check `tmux ls`."
            ),
        )

    delivered = False
    if body.initial_message and registered.channel_port is not None:
        try:
            _forward_to_channel(registered.channel_port, body.initial_message)
            delivered = True
        except Exception as e:
            send_event("spawn_message_failed", name=body.name, error=str(e))

    send_event("spawn_succeeded", name=body.name, namespace=body.namespace, session_id=registered.session_id)
    return SpawnResponse(
        spawned=True,
        name=registered.name,
        namespace=registered.namespace,
        session_id=registered.session_id,
        tmux_session=tmux_session,
        initial_message_delivered=delivered,
    )


@router.post("/sessions/{name_or_id}/message")
def send_message(name_or_id: str, body: MessageRequest) -> dict:
    peer, local = _resolve_target(name_or_id)
    if peer:
        result, status = _forward_to_peer(
            peer,
            f"/sessions/{urllib.parse.quote(local)}/message",
            method="POST",
            json_body=body.model_dump(),
        )
        return _proxy_response(result, status)
    session = _resolve(local)
    if session.channel_port is None:
        raise HTTPException(
            status_code=409,
            detail=f"session '{session.name}' has no channel — not started with --dangerously-load-development-channels server:session-bridge",
        )

    from_name = None
    from_id = body.from_session
    if from_id:
        m = _get_monitor()
        sender = m.get_session_by_id(from_id)
        if sender:
            from_name = sender.name

    try:
        result = _forward_to_channel(session.channel_port, body.text, from_name=from_name, from_id=from_id)
    except Exception as e:
        send_event("message_failed", error=str(e), to=session.name)
        raise HTTPException(status_code=502, detail=f"channel error: {e}")

    send_event("message_sent", to=session.name)
    return {"sent": body.text, "session": session.name, "session_id": session.session_id, "channel": result}
