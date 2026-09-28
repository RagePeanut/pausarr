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

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from .config import Config, validate_pause_mode
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


def _normalise_pause_mode(value: str | None) -> str | None:
    """Validate an optional request-body pause mode, returning None if absent.

    A malformed value (e.g. "loud") is rejected as a 422 by pydantic; the
    "missing when required" case is handled per-request against the config so
    the error message can explain the global-vs-per-request rule.
    """
    if value is None:
        return None
    try:
        return validate_pause_mode(value)
    except ValueError as exc:
        raise ValueError(str(exc))


class PauseRequest(BaseModel):
    tag: str = Field(..., min_length=1, description="Source identifier, e.g. 'plex'")
    request: Literal["pause", "resume"] = Field(
        ..., description="Whether this source wants torrents paused or resumed"
    )
    pause_mode: str | None = Field(
        None,
        description=(
            "Pause mode for this source ('all' or 'keep-seeding'). Required "
            "when the server has no global PAUSE_MODE configured; ignored when "
            "it does (the global mode always wins). Only relevant for a "
            "'pause' request."
        ),
    )

    @field_validator("pause_mode")
    @classmethod
    def _check_pause_mode(cls, v: str | None) -> str | None:
        return _normalise_pause_mode(v)


class HeartbeatRequest(BaseModel):
    tag: str = Field(..., min_length=1, description="Source identifier, e.g. 'youtube-tv'")
    pause_mode: str | None = Field(
        None,
        description=(
            "Pause mode for this source ('all' or 'keep-seeding'). Required "
            "when the server has no global PAUSE_MODE configured; ignored when "
            "it does (the global mode always wins)."
        ),
    )

    @field_validator("pause_mode")
    @classmethod
    def _check_pause_mode(cls, v: str | None) -> str | None:
        return _normalise_pause_mode(v)


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
    reconciler = Reconciler(
        store,
        qbt,
        config.poll_interval,
        config.pause_mode,
        config.keep_seeding_max_active_downloads,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Expire anything that went stale while we were down, then converge
        # qBittorrent to the persisted desired state before serving traffic.
        store.expire_stale()
        await reconciler.reconcile()
        reconciler.start()
        logger.info(
            "Pausarr started (pause mode: %s)",
            config.pause_mode
            if config.pause_mode is not None
            else "per-request (no global PAUSE_MODE set)",
        )
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

    def _resolve_request_pause_mode(requested: str | None) -> str | None:
        """Decide which pause mode to store for an incoming pause/heartbeat.

        * A global ``PAUSE_MODE`` overrides everything — the request's mode is
          ignored (we store None; the reconciler uses the global mode).
        * Otherwise the request must supply a mode, else it's a 400: with no
          global default there's no way to know how to pause.
        """
        if config.pause_mode is not None:
            return None
        if requested is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "pause_mode is required: the server has no global "
                    "PAUSE_MODE configured, so each request must specify "
                    "'pause_mode' ('all' or 'keep-seeding')."
                ),
            )
        return requested

    @app.post("/pause")
    async def pause(req: PauseRequest):
        pausing = req.request == "pause"
        # Only a 'pause' needs a mode; a 'resume' clears the flag regardless.
        mode = _resolve_request_pause_mode(req.pause_mode) if pausing else None
        store.set_push(req.tag, active=pausing, pause_mode=mode)
        await reconciler.reconcile()
        return store.snapshot()

    @app.post("/heartbeat")
    async def heartbeat(req: HeartbeatRequest):
        mode = _resolve_request_pause_mode(req.pause_mode)
        store.heartbeat(req.tag, pause_mode=mode)
        await reconciler.reconcile()
        return store.snapshot()

    @app.get("/status")
    async def status():
        return {"pause_mode": config.pause_mode, **store.snapshot()}

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app


app = create_app()
