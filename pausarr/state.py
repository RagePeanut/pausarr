"""Flag registry: the source of truth for whether torrents should be paused.

Two kinds of flags exist, keyed by an arbitrary, dynamically-created ``tag``:

* **push flags** — set explicitly by event-driven sources (e.g. Tautulli).
  They stay at their last value until the source sends the opposite request.
  They never expire.

* **heartbeat flags** — kept alive by periodic pings from time-driven sources
  (e.g. YouTube/Twitch on a TV via Tasker or Home Assistant). A heartbeat flag
  is "active" only while its last ping is younger than ``heartbeat_timeout``.
  When no ping arrives in time, it expires and clears itself.

The overall rule: torrents are paused while **any** flag is active, and resumed
only once **all** flags are inactive.

All mutating operations are guarded by a lock so overlapping requests from
multiple sources are serialised and cannot race.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

PUSH = "push"
HEARTBEAT = "heartbeat"


@dataclass
class Flag:
    tag: str
    kind: str  # PUSH or HEARTBEAT
    # For push flags: whether the source currently requests a pause.
    # For heartbeat flags: implicitly True while not expired (see is_active).
    active: bool = False
    # Wall-clock time of the last heartbeat (heartbeat flags only).
    last_seen: float = 0.0
    # Wall-clock time this flag was last changed, for observability.
    updated_at: float = field(default_factory=time.time)

    def is_active(self, now: float, heartbeat_timeout: float) -> bool:
        if self.kind == HEARTBEAT:
            return (now - self.last_seen) < heartbeat_timeout
        return self.active


class StateStore:
    """Thread-safe registry of flags with JSON persistence."""

    def __init__(self, state_file: str, heartbeat_timeout: float) -> None:
        self._state_file = state_file
        self._heartbeat_timeout = heartbeat_timeout
        self._lock = threading.RLock()
        self._flags: dict[str, Flag] = {}
        # Snapshot of the qBittorrent download-queue preferences taken when a
        # "keep-seeding" pause is applied and consumed when it is lifted:
        # ``{"max_active_downloads": int, "queueing_enabled": bool}``. Empty
        # when not paused in that mode. Persisted so a restart mid-pause can
        # still restore the originals. See Reconciler for how it's used.
        self._cached_prefs: dict[str, object] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if not os.path.exists(self._state_file):
            logger.info("No existing state file at %s; starting fresh", self._state_file)
            return
        try:
            with open(self._state_file, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            for tag, data in raw.get("flags", {}).items():
                self._flags[tag] = Flag(
                    tag=tag,
                    kind=data["kind"],
                    active=data.get("active", False),
                    last_seen=data.get("last_seen", 0.0),
                    updated_at=data.get("updated_at", time.time()),
                )
            self._cached_prefs = self._sanitize_cached_prefs(
                raw.get("cached_prefs", {})
            )
            logger.info("Loaded %d flag(s) from %s", len(self._flags), self._state_file)
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("Could not load state file %s: %s; starting fresh", self._state_file, exc)
            self._flags = {}
            self._cached_prefs = {}

    @staticmethod
    def _sanitize_cached_prefs(raw: object) -> dict[str, object]:
        """Coerce a loaded cached-prefs blob into a well-typed structure.

        Anything malformed is dropped rather than raising, so a corrupt or
        older cache never blocks startup — the worst case is we lose the
        snapshot (see Reconciler for how that's handled on resume). Only a
        complete snapshot (both keys present and well-typed) is kept.
        """
        if not isinstance(raw, dict):
            return {}
        mad = raw.get("max_active_downloads")
        qe = raw.get("queueing_enabled")
        if (
            isinstance(mad, int)
            and not isinstance(mad, bool)
            and isinstance(qe, bool)
        ):
            return {"max_active_downloads": mad, "queueing_enabled": qe}
        return {}

    def _persist_locked(self) -> None:
        """Atomically write state to disk. Caller must hold the lock."""
        payload = {
            "flags": {
                tag: {
                    "kind": f.kind,
                    "active": f.active,
                    "last_seen": f.last_seen,
                    "updated_at": f.updated_at,
                }
                for tag, f in self._flags.items()
            },
            "cached_prefs": self._cached_prefs,
        }
        directory = os.path.dirname(self._state_file) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            # Write to a temp file then rename for atomicity.
            fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
                os.replace(tmp_path, self._state_file)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        except OSError as exc:
            logger.error("Failed to persist state to %s: %s", self._state_file, exc)

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #
    def set_push(self, tag: str, active: bool) -> None:
        """Set an explicit push flag (pause=True / resume=False)."""
        with self._lock:
            flag = self._flags.get(tag)
            if flag is None or flag.kind != PUSH:
                flag = Flag(tag=tag, kind=PUSH)
                self._flags[tag] = flag
            flag.active = active
            flag.updated_at = time.time()
            self._persist_locked()
            logger.info("Push flag %r set to %s", tag, "pause" if active else "resume")

    def heartbeat(self, tag: str) -> None:
        """Record a heartbeat, (re)creating the flag and refreshing its TTL."""
        now = time.time()
        with self._lock:
            flag = self._flags.get(tag)
            if flag is None or flag.kind != HEARTBEAT:
                flag = Flag(tag=tag, kind=HEARTBEAT)
                self._flags[tag] = flag
                logger.info("Heartbeat flag %r created", tag)
            flag.last_seen = now
            flag.updated_at = now
            self._persist_locked()

    def clear(self, tag: str) -> bool:
        """Remove a flag entirely. Returns True if it existed."""
        with self._lock:
            existed = self._flags.pop(tag, None) is not None
            if existed:
                self._persist_locked()
                logger.info("Flag %r cleared", tag)
            return existed

    def save_cached_prefs(self, prefs: dict) -> None:
        """Persist the pre-pause snapshot of download-queue preferences.

        ``prefs`` is ``{"max_active_downloads": int, "queueing_enabled": bool}``.
        Replaces any previous snapshot.
        """
        with self._lock:
            self._cached_prefs = dict(prefs) if prefs else {}
            self._persist_locked()

    def take_cached_prefs(self) -> dict:
        """Return and clear the download-queue snapshot (empty if none).

        Clearing on read makes restore idempotent: once the prefs are handed
        back to the reconciler they won't be applied again on a later resume.
        """
        with self._lock:
            prefs = self._cached_prefs
            self._cached_prefs = {}
            if prefs:
                self._persist_locked()
            return dict(prefs)

    def has_cached_prefs(self) -> bool:
        """Whether a snapshot exists (without consuming it)."""
        with self._lock:
            return bool(self._cached_prefs)

    def expire_stale(self, now: Optional[float] = None) -> list[str]:
        """Remove heartbeat flags whose TTL has elapsed. Returns expired tags."""
        now = now if now is not None else time.time()
        expired: list[str] = []
        with self._lock:
            for tag, flag in list(self._flags.items()):
                if flag.kind == HEARTBEAT and (now - flag.last_seen) >= self._heartbeat_timeout:
                    del self._flags[tag]
                    expired.append(tag)
            if expired:
                self._persist_locked()
                logger.info("Expired stale heartbeat flag(s): %s", ", ".join(expired))
        return expired

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def should_pause(self, now: Optional[float] = None) -> bool:
        """True if any flag is currently active."""
        now = now if now is not None else time.time()
        with self._lock:
            return any(
                f.is_active(now, self._heartbeat_timeout) for f in self._flags.values()
            )

    def snapshot(self, now: Optional[float] = None) -> dict:
        """A serialisable view of current state for the /status endpoint."""
        now = now if now is not None else time.time()
        with self._lock:
            flags = {}
            for tag, f in self._flags.items():
                entry = {
                    "kind": f.kind,
                    "active": f.is_active(now, self._heartbeat_timeout),
                    "updated_at": f.updated_at,
                }
                if f.kind == HEARTBEAT:
                    entry["last_seen"] = f.last_seen
                    entry["seconds_since_last_seen"] = round(now - f.last_seen, 1)
                    entry["expires_in"] = round(
                        max(0.0, self._heartbeat_timeout - (now - f.last_seen)), 1
                    )
                flags[tag] = entry
            return {
                "should_pause": any(
                    f.is_active(now, self._heartbeat_timeout) for f in self._flags.values()
                ),
                "heartbeat_timeout": self._heartbeat_timeout,
                "flags": flags,
            }
