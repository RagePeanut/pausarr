"""Minimal async client for the qBittorrent Web API (v2).

Only what Pausarr needs: authenticate, then pause or resume *all* torrents.

qBittorrent renamed the pause/resume endpoints to stop/start in v5.0. To stay
compatible with both old (v4.x) and new (v5.x) servers, we try the modern
endpoint first and fall back to the legacy one on a 404/405.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class QBittorrentError(RuntimeError):
    pass


class QBittorrentClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        # qBittorrent requires a Referer header matching the host or it
        # rejects requests with 403.
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Referer": self._base_url},
            timeout=15.0,
        )
        self._authenticated = False

    async def close(self) -> None:
        await self._client.aclose()

    async def _login(self) -> None:
        resp = await self._client.post(
            "/api/v2/auth/login",
            data={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200 or resp.text.strip() != "Ok.":
            raise QBittorrentError(
                f"qBittorrent login failed (status {resp.status_code}): {resp.text!r}"
            )
        self._authenticated = True
        logger.info("Authenticated with qBittorrent at %s", self._base_url)

    async def _ensure_auth(self) -> None:
        if not self._authenticated:
            await self._login()

    async def _post_with_reauth(self, path: str, data: dict) -> httpx.Response:
        """POST, transparently re-logging-in if the session cookie expired."""
        await self._ensure_auth()
        resp = await self._client.post(path, data=data)
        if resp.status_code == 403:
            # Cookie likely expired; re-auth once and retry.
            logger.info("qBittorrent session expired; re-authenticating")
            self._authenticated = False
            await self._ensure_auth()
            resp = await self._client.post(path, data=data)
        return resp

    async def _toggle_all(self, modern_path: str, legacy_path: str) -> None:
        resp = await self._post_with_reauth(modern_path, {"hashes": "all"})
        if resp.status_code in (404, 405):
            # Older qBittorrent: fall back to the legacy endpoint name.
            resp = await self._post_with_reauth(legacy_path, {"hashes": "all"})
        if resp.status_code != 200:
            raise QBittorrentError(
                f"qBittorrent call {modern_path} failed (status {resp.status_code}): {resp.text!r}"
            )

    async def pause_all(self) -> None:
        # v5.x: /torrents/stop, v4.x: /torrents/pause
        await self._toggle_all("/api/v2/torrents/stop", "/api/v2/torrents/pause")
        logger.info("Paused all torrents")

    async def resume_all(self) -> None:
        # v5.x: /torrents/start, v4.x: /torrents/resume
        await self._toggle_all("/api/v2/torrents/start", "/api/v2/torrents/resume")
        logger.info("Resumed all torrents")
