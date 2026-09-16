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
        pause_mode: str = PAUSE_MODE_ALL,
    ) -> None:
        self._store = store
        self._qbt = qbt
        self._poll_interval = poll_interval
        # "all" | "keep-seeding". In "all" we stop/start every torrent; in
        # "keep-seeding" we set the global download-queue limit to 0 so
        # downloads stop but completed torrents keep seeding. See _apply_pause /
        # _apply_resume.
        self._pause_mode = pause_mode
        # None = unknown (force a reconcile on first run so we converge the
        # actual qBittorrent state to what Pausarr believes).
        self._last_applied_pause: Optional[bool] = None
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    async def reconcile(self) -> None:
        """Make qBittorrent match desired state. Safe to call concurrently."""
        async with self._lock:
            desired_pause = self._store.should_pause()
            if desired_pause == self._last_applied_pause:
                return
            try:
                if desired_pause:
                    await self._apply_pause()
                else:
                    await self._apply_resume()
                self._last_applied_pause = desired_pause
            except (QBittorrentError, Exception) as exc:  # noqa: BLE001
                # Leave _last_applied_pause unchanged so the next poll retries.
                logger.error("Reconcile failed (will retry): %s", exc)

    async def _apply_pause(self) -> None:
        """Pause according to the configured mode.

        * ``all``          — stop every torrent.
        * ``keep-seeding`` — snapshot the global download-queue preferences,
          persist them, then set ``max_active_downloads`` to 0 so downloads
          stop while completed torrents keep seeding. The snapshot is taken
          *before* changing anything and only when one isn't already cached (a
          re-pause while already paused must not overwrite the originals with
          our zeroed value).
        """
        if self._pause_mode == PAUSE_MODE_ALL:
            await self._qbt.pause_all()
            return
        # keep-seeding
        if self._store.has_cached_prefs():
            # Already paused; the queue limit is already 0, nothing to do.
            return
        original = await self._qbt.stop_downloads()
        self._store.save_cached_prefs(original)

    async def _apply_resume(self) -> None:
        """Resume according to the configured mode (inverse of _apply_pause)."""
        if self._pause_mode == PAUSE_MODE_ALL:
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
