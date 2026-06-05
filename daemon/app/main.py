"""FastAPI application for the session-bridge registry."""

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import routes
from app.monitor import SessionMonitor
from app.telemetry import send_event


def _configure_logging() -> None:
    """Make our log records visible under systemd / launchd. uvicorn doesn't
    touch the root logger, so `log.info(...)` calls drop unless we install a
    handler.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    )


monitor = SessionMonitor()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    routes.monitor = monitor
    send_event("daemon_started")
    print("session-bridge started", file=sys.stderr)
    yield


app = FastAPI(title="session-bridge", lifespan=lifespan)
app.include_router(routes.router)
