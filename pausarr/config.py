"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
            state_file=os.getenv("STATE_FILE", "/data/state.json"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
