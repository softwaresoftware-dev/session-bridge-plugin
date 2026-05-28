"""Fire-and-forget telemetry for session-bridge.

Stdlib only. Sends events in daemon threads so request latency is unaffected.
Silent on all failures — telemetry must never break the proxy.
"""

import json
import platform
import threading
import urllib.request
import uuid

ENDPOINT = "https://telemetry.softwaresoftware.dev/api/events"
SESSION_ID = str(uuid.uuid4())
TIMEOUT = 2


def send_event(event_type: str, **kwargs):
    """Send a telemetry event in a background thread."""
    payload = {
        "event_type": event_type,
        "source": "session-bridge",
        "session_id": SESSION_ID,
        "metadata": {"os": platform.system().lower(), **kwargs},
    }
    threading.Thread(target=_post, args=(payload,), daemon=True).start()


def _post(payload):
    try:
        req = urllib.request.Request(
            ENDPOINT,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=TIMEOUT)
    except Exception:
        pass
