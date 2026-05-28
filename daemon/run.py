"""Entry point for the session-bridge daemon.

Cross-platform launcher invoked by daemon-manager. Reads bind host/port from
SESSION_BRIDGE_BIND / SESSION_BRIDGE_PORT (defaults: 0.0.0.0:8910) and execs
uvicorn against app.main:app from this file's directory.
"""

import os
import sys
from pathlib import Path


def main() -> None:
    here = Path(__file__).resolve().parent
    os.chdir(here)
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))

    host = os.environ.get("SESSION_BRIDGE_BIND", "0.0.0.0")
    port = int(os.environ.get("SESSION_BRIDGE_PORT", "8910"))

    import uvicorn

    uvicorn.run("app.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
