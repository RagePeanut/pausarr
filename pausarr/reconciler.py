"""Reconciles desired state (from the flag registry) with qBittorrent.

The reconciler is the only component that talks to qBittorrent. It computes the
desired action from ``StateStore.should_pause()`` and only issues a call when
the state actually changes, so we don't spam the API every poll.

A background watchdog task drives two things on every ``poll_interval``:

1. expire stale heartbeat flags (the TTL mechanism), and
2. reconcile qBittorrent with the resulting desired state.

Reconciliation is also invoked immediately after each inbound request so
push events and fresh heartbeats take effect without waiting for the next poll.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .config import PAUSE_MODE_ALL, PAUSE_MODE_KEEP_SEEDING
from .qbittorrent import QBittorrentClient, QBittorrentError
from .state import StateStore

logger = logging.getLogger(__name__)


class Reconciler:
    def __init__(
        self,
        store: StateStore,
        qbt: QBittorrentClient,
        poll_interval: float,
        pause_mode: Optional[str] = None,
        keep_seeding_max_active_downloads: int = 100000,
    ) -> None:
        self._store = store
        self._qbt = qbt
        self._poll_interval = poll_interval
        # Fixed global mode from PAUSE_MODE, or None. When set it's used for
        # every pause and overrides per-request modes. When None, the effective
        # mode is computed per-reconcile from the active flags (most
        # restrictive wins) via StateStore.effective_pause_mode. "all" stops
        # every torrent; "keep-seeding" sets the global download-queue limit to
        # 0 so downloads stop but completed torrents keep seeding. See
        # _apply_pause / _apply_resume.
        self._global_pause_mode = pause_mode
        # Crash-recovery fallback: what to cache/restore if we read a
        # max_active_downloads of 0 (meaning downloads are already stopped, so
        # the real value is unknown). See _apply_pause.
        self._keep_seeding_max_active_downloads = keep_seeding_max_active_downloads
        # None = unknown (force a reconcile on first run so we converge the
        # actual qBittorrent state to what Pausarr believes). When paused, this
        # records the mode currently applied so we can detect a mid-pause mode
        # change and transition between modes.
        self._last_applied_pause: Optional[bool] = None
        self._last_applied_mode: Optional[str] = None
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def _resolve_pause_mode(self) -> Optional[str]:
        """Determine which mode a pause should use right now.

        A configured global ``PAUSE_MODE`` always wins. Otherwise the most
        restrictive mode among the active flags is used. Returns None only when
        there's no global mode *and* no active flag supplied one — a state the
        caller treats as "cannot pause" (misconfiguration): the request handler
        rejects such requests up front, but the watchdog may still observe it,
        in which case we log and leave qBittorrent untouched.
        """
        if self._global_pause_mode is not None:
            return self._global_pause_mode
        return self._store.effective_pause_mode()

    async def reconcile(self) -> None:
        """Make qBittorrent match desired state. Safe to call concurrently."""
        async with self._lock:
            desired_pause = self._store.should_pause()
            desired_mode = self._resolve_pause_mode() if desired_pause else None

            if desired_pause and desired_mode is None:
                # No global mode and no active flag carries one. We can't know
                # how to pause, so leave qBittorrent as-is rather than guessing.
                # (Request handlers reject mode-less pause requests, so this is
                # only reachable via oddly-persisted state.)
                logger.warning(
                    "Want to pause but no pause mode is resolvable (no global "
                    "PAUSE_MODE and no active flag specified one); leaving "
                    "qBittorrent untouched"
                )
                return

            # Nothing to do when the desired pause state and mode are both
            # already applied.
            if (
                desired_pause == self._last_applied_pause
                and desired_mode == self._last_applied_mode
            ):
                return

            try:
                if not desired_pause:
                    # Resume using whatever mode we last paused with.
                    await self._apply_resume(self._last_applied_mode)
                elif (
                    self._last_applied_pause
                    and self._last_applied_mode is not None
                    and self._last_applied_mode != desired_mode
                ):
                    # Already paused but the mode changed (e.g. keep-seeding ->
                    # all as a more restrictive flag became active). Undo the
                    # old mode, then apply the new one, so their side effects
                    # don't leak into each other.
                    logger.info(
                        "Pause mode changed while paused: %s -> %s",
                        self._last_applied_mode,
                        desired_mode,
                    )
                    await self._apply_resume(self._last_applied_mode)
                    await self._apply_pause(desired_mode)
                else:
                    await self._apply_pause(desired_mode)
                self._last_applied_pause = desired_pause
                self._last_applied_mode = desired_mode
            except (QBittorrentError, Exception) as exc:  # noqa: BLE001
                # Leave _last_applied_* unchanged so the next poll retries.
                logger.error("Reconcile failed (will retry): %s", exc)

    async def _apply_pause(self, mode: str) -> None:
        """Pause according to ``mode``.

        * ``all``          — stop every torrent.
        * ``keep-seeding`` — snapshot the global download-queue preferences,
          persist them, then set ``max_active_downloads`` to 0 so downloads
          stop while completed torrents keep seeding.

        Crash safety. The snapshot is only taken when one isn't already cached
        (a re-pause while already paused must not overwrite the originals with
        our zeroed value), and it is persisted to disk *before* qBittorrent is
        mutated (write-ahead), so a crash between the two can never leave
        qBittorrent stopped with no cached original to restore.

        Sentinel guard. If we read a ``max_active_downloads`` of 0, downloads
        are *already* stopped — most likely we crashed while paused and lost the
        cache. The true original is unknown, and caching 0 would mean downloads
        never resume. So we substitute the configured fallback
        (``KEEP_SEEDING_MAX_ACTIVE_DOWNLOADS``) instead.
        """
        if mode == PAUSE_MODE_ALL:
            await self._qbt.pause_all()
            return
        # keep-seeding
        if self._store.has_cached_prefs():
            # Already paused; the queue limit is already 0, nothing to do.
            return
        original = await self._qbt.read_download_prefs()
        if original["max_active_downloads"] <= 0:
            logger.warning(
                "Read max_active_downloads=%s (downloads already stopped); the "
                "real value is unknown, using fallback %s "
                "(KEEP_SEEDING_MAX_ACTIVE_DOWNLOADS)",
                original["max_active_downloads"],
                self._keep_seeding_max_active_downloads,
            )
            original["max_active_downloads"] = self._keep_seeding_max_active_downloads
        # Persist BEFORE mutating qBittorrent (write-ahead) so a crash between
        # the two still leaves us with the original cached for restore.
        self._store.save_cached_prefs(original)
        await self._qbt.set_downloads_stopped()

    async def _apply_resume(self, mode: Optional[str]) -> None:
        """Resume according to ``mode`` (inverse of _apply_pause).

        ``mode`` is the mode that was in effect when we paused (``None`` only if
        we were never actually paused, in which case there's nothing to undo).
        Resuming must mirror exactly what the pause did, so we key off the mode
        that was applied rather than the currently-desired one.
        """
        if mode is None:
            return
        if mode == PAUSE_MODE_ALL:
            await self._qbt.resume_all()
            return
        # keep-seeding
        prefs = self._store.take_cached_prefs()
        if prefs:
            await self._qbt.restore_downloads(prefs)
        else:
            # No snapshot (e.g. first run, or state lost). We don't know the
            # original max_active_downloads, so leave qBittorrent's current
            # settings untouched rather than guessing a value.
            logger.info(
                "No cached download-queue prefs to restore; leaving qBittorrent "
                "settings as-is (set max_active_downloads manually if needed)"
            )

    async def _run(self) -> None:
        logger.info("Watchdog started (poll interval %.0fs)", self._poll_interval)
        while not self._stop_event.is_set():
            try:
                self._store.expire_stale()
                await self.reconcile()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Unexpected error in watchdog loop: %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass
        logger.info("Watchdog stopped")

    def start(self) -> None:
        if self._task is None:
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None
