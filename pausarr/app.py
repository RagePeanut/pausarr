"""Pausarr HTTP API.

Endpoints
---------
* ``POST /pause``      — push sources (e.g. Tautulli): {"tag": "plex", "request": "pause"|"resume"}
* ``POST /heartbeat``  — heartbeat sources (e.g. Tasker/HA): {"tag": "youtube-tv"}
* ``GET  /status``     — current flags and computed decision (debugging)
* ``GET  /healthz``    — liveness probe
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .config import Config
from .qbittorrent import QBittorrentClient
from .reconciler import Reconciler
from .state import StateStore

logger = logging.getLogger(__name__)


class _HealthzAccessLogFilter(logging.Filter):
    """Drop uvicorn access-log lines for the health-check endpoint.

    The Docker HEALTHCHECK hits ``GET /healthz`` every 30s, which otherwise
    floods the access log and drowns out real ``/pause`` and ``/heartbeat``
    traffic. Uvicorn's access logger emits records whose ``args`` are
    ``(client_addr, method, full_path, http_version, status_code)``; we filter
    on the request path (index 2). The health-check itself keeps working — only
    its log line is suppressed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            path = args[2]
            if isinstance(path, str) and path.split("?", 1)[0] == "/healthz":
                return False
        return True


def _install_healthz_log_filter() -> None:
    """Attach the /healthz filter to uvicorn's access logger (idempotent)."""
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _HealthzAccessLogFilter) for f in access_logger.filters):
        access_logger.addFilter(_HealthzAccessLogFilter())


class PauseRequest(BaseModel):
    tag: str = Field(..., min_length=1, description="Source identifier, e.g. 'plex'")
    request: Literal["pause", "resume"] = Field(
        ..., description="Whether this source wants torrents paused or resumed"
    )


class HeartbeatRequest(BaseModel):
    tag: str = Field(..., min_length=1, description="Source identifier, e.g. 'youtube-tv'")


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    # Keep the health-check working but stop it spamming the access log.
    _install_healthz_log_filter()

    store = StateStore(config.state_file, config.heartbeat_timeout)
    qbt = QBittorrentClient(
        config.qbittorrent_url, config.qbittorrent_user, config.qbittorrent_pass
    )
    reconciler = Reconciler(store, qbt, config.poll_interval)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Expire anything that went stale while we were down, then converge
        # qBittorrent to the persisted desired state before serving traffic.
        store.expire_stale()
        await reconciler.reconcile()
        reconciler.start()
        logger.info("Pausarr started")
        try:
            yield
        finally:
            await reconciler.stop()
            await qbt.close()

    app = FastAPI(title="Pausarr", version="1.0.0", lifespan=lifespan)

    # Expose collaborators for tests / introspection.
    app.state.store = store
    app.state.reconciler = reconciler
    app.state.config = config

    @app.post("/pause")
    async def pause(req: PauseRequest):
        store.set_push(req.tag, active=(req.request == "pause"))
        await reconciler.reconcile()
        return store.snapshot()

    @app.post("/heartbeat")
    async def heartbeat(req: HeartbeatRequest):
        store.heartbeat(req.tag)
        await reconciler.reconcile()
        return store.snapshot()

    @app.get("/status")
    async def status():
        return store.snapshot()

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app


app = create_app()
