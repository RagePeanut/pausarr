"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Valid values for PAUSE_MODE. See Config.pause_mode.
PAUSE_MODE_BOTH = "both"
PAUSE_MODE_UPLOAD = "upload"
PAUSE_MODE_DOWNLOAD = "download"
_VALID_PAUSE_MODES = {PAUSE_MODE_BOTH, PAUSE_MODE_UPLOAD, PAUSE_MODE_DOWNLOAD}


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_pause_mode(name: str, default: str) -> str:
    """Read and validate PAUSE_MODE, falling back to the default on unset.

    An explicitly-set but invalid value is a configuration error and raises so
    the misconfiguration surfaces at startup rather than silently pausing the
    wrong direction (or nothing).
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value not in _VALID_PAUSE_MODES:
        raise ValueError(
            f"{name}={raw!r} is invalid; expected one of "
            f"{', '.join(sorted(_VALID_PAUSE_MODES))}"
        )
    return value


@dataclass(frozen=True)
class Config:
    """Runtime configuration for Pausarr.

    All values are read once at startup from environment variables so the
    behaviour is fully declarative from the docker-compose file.
    """

    # qBittorrent Web UI connection. User/pass are optional: leave them unset
    # if qBittorrent bypasses authentication for Pausarr's IP/subnet.
    qbittorrent_url: str
    qbittorrent_user: str
    qbittorrent_pass: str

    # A heartbeat tag is considered active until this many seconds have
    # elapsed since its last heartbeat. Global for all heartbeat sources.
    heartbeat_timeout: float

    # How often the watchdog re-evaluates state and expires stale heartbeats.
    poll_interval: float

    # Which direction(s) to pause. One of "both" (default), "upload" or
    # "download".
    #
    # * "both"     — fully stop/start torrents (hashes=all). Simplest, and the
    #                historical behaviour.
    # * "upload"   — leave downloading running but throttle *uploads* to the
    #                minimum (1 B/s) while paused.
    # * "download" — leave uploading (seeding) running but throttle *downloads*
    #                to the minimum (1 B/s) while paused.
    #
    # In the directional modes Pausarr snapshots each torrent's original
    # per-torrent rate limit before throttling and restores it on resume, so it
    # never clobbers limits you set manually.
    pause_mode: str

    # Where the flag state is persisted so it survives restarts.
    state_file: str

    # If a qBittorrent call fails we retry reconciliation on the next poll.
    # This bounds how noisy the logs get.
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            qbittorrent_url=os.getenv("QBITTORRENT_URL", "http://localhost:8080"),
            # Empty by default — only needed when qBittorrent requires auth.
            qbittorrent_user=os.getenv("QBITTORRENT_USER", ""),
            qbittorrent_pass=os.getenv("QBITTORRENT_PASS", ""),
            heartbeat_timeout=float(os.getenv("HEARTBEAT_TIMEOUT", "180")),
            poll_interval=float(os.getenv("POLL_INTERVAL", "15")),
            pause_mode=_get_pause_mode("PAUSE_MODE", PAUSE_MODE_BOTH),
            state_file=os.getenv("STATE_FILE", "/data/state.json"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
