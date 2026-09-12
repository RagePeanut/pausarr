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
