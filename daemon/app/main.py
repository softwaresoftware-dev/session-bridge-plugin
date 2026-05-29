"""FastAPI application with background session polling."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import routes
from app.discovery import discover_sessions
from app.monitor import SessionMonitor
from app.telemetry import send_event


def _configure_logging() -> None:
    """Make our log records visible under systemd / launchd. See dispatcher's
    main.py for the rationale — uvicorn doesn't touch the root logger, so
    our `log.info(...)` calls silently drop unless we install a handler.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    )


monitor = SessionMonitor()


async def poll_loop():
    """Background loop: discover sessions and poll their state."""
    while True:
        try:
            discovered = await asyncio.to_thread(discover_sessions)
            monitor.update_sessions(discovered)
            monitor.poll_all()
        except Exception as e:
            print(f"poll error: {e}", file=sys.stderr)
        await asyncio.sleep(2.5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    routes.monitor = monitor
    task = asyncio.create_task(poll_loop())
    send_event("daemon_started")
    print("session-bridge started", file=sys.stderr)
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="session-bridge", lifespan=lifespan)
app.include_router(routes.router)
