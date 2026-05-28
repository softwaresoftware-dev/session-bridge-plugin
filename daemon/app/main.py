"""FastAPI application with background session polling."""

import asyncio
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import routes
from app.discovery import discover_sessions
from app.monitor import SessionMonitor
from app.telemetry import send_event

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
