"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Valid values for PAUSE_MODE. See Config.pause_mode.
#
# * "all"          — fully stop every torrent (both downloading and seeding).
# * "keep-seeding" — stop *downloading* but keep completed torrents *seeding*.
#                    Achieved by setting qBittorrent's global
#                    ``max_active_downloads`` to 0 (with torrent queueing
#                    enabled), which halts active downloads while finished
#                    torrents continue to upload.
PAUSE_MODE_ALL = "all"
PAUSE_MODE_KEEP_SEEDING = "keep-seeding"
_VALID_PAUSE_MODES = {PAUSE_MODE_ALL, PAUSE_MODE_KEEP_SEEDING}


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_positive_int(name: str, default: int) -> int:
    """Read a positive integer env var, falling back to default on unset/empty.

    Raises on a set-but-non-integer or non-positive value so a misconfiguration
    surfaces at startup.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"{name}={raw!r} is invalid; expected a positive integer")
    if value <= 0:
        raise ValueError(f"{name}={raw!r} is invalid; expected a positive integer")
    return value


def _get_pause_mode(name: str, default: str) -> str:
    """Read and validate PAUSE_MODE, falling back to the default on unset.

    An explicitly-set but invalid value is a configuration error and raises so
    the misconfiguration surfaces at startup rather than silently pausing the
    wrong way (or nothing).
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

    # What a pause does. One of "all" (default) or "keep-seeding".
    #
    # * "all"          — fully stop/start every torrent (hashes=all). The
    #                    historical behaviour: downloading and seeding both halt.
    # * "keep-seeding" — stop downloading but keep completed torrents seeding.
    #                    Pausarr sets qBittorrent's global
    #                    ``max_active_downloads`` to 0 (and enables queueing if
    #                    needed), snapshotting the originals first and restoring
    #                    them on resume so it never clobbers your settings.
    pause_mode: str

    # Crash-recovery fallback for "keep-seeding" mode.
    #
    # Normally Pausarr snapshots your real ``max_active_downloads`` before
    # zeroing it, and restores that on resume. But if it reads a value of 0 —
    # which means qBittorrent is *already* stopped (e.g. Pausarr crashed while
    # paused and the snapshot was lost) — caching 0 would mean downloads never
    # resume. In that case Pausarr restores this value instead. Defaults to
    # 100000 (qBittorrent's own "effectively unlimited" sentinel); set it to
    # match your normal ``max_active_downloads`` so crash-recovery restores the
    # right number.
    keep_seeding_max_active_downloads: int

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
            pause_mode=_get_pause_mode("PAUSE_MODE", PAUSE_MODE_ALL),
            keep_seeding_max_active_downloads=_get_positive_int(
                "KEEP_SEEDING_MAX_ACTIVE_DOWNLOADS", 100000
            ),
            state_file=os.getenv("STATE_FILE", "/data/state.json"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
