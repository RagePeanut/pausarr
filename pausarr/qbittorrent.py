"""Minimal async client for the qBittorrent Web API (v2).

Only what Pausarr needs: (optionally) authenticate, then either pause/resume
*all* torrents, or — for "keep-seeding" mode — stop new downloads via the
global download-queue limit while completed torrents keep seeding.

**Authentication is optional.** qBittorrent can be configured to bypass
authentication for clients on localhost or on a whitelisted subnet
("Bypass authentication for clients on localhost / in whitelisted IP subnets").
In that case no username/password is needed. To support both setups:

* If credentials are provided, we log in up front to obtain a session cookie.
* If they are omitted, we skip login and call the API directly.
* Either way, if a request comes back ``403 Forbidden`` we (re-)attempt a login
  and retry once. So a stale cookie *or* a server that unexpectedly requires
  auth is handled transparently — and if auth genuinely isn't required, we
  never bother logging in at all.

qBittorrent renamed the pause/resume endpoints to stop/start in v5.0. To stay
compatible with both old (v4.x) and new (v5.x) servers, we try the modern
endpoint first and fall back to the legacy one on a 404/405.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class QBittorrentError(RuntimeError):
    pass


class QBittorrentClient:
    def __init__(
        self,
        base_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # Treat empty strings the same as "not set" so a blank env var means
        # "no auth" rather than an empty-username login attempt.
        self._username = username or ""
        self._password = password or ""
        # qBittorrent requires a Referer header matching the host or it
        # rejects requests with 403.
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Referer": self._base_url},
            timeout=15.0,
        )
        self._authenticated = False

    @property
    def _has_credentials(self) -> bool:
        # A password alone (with the default empty username) is valid in
        # qBittorrent, so consider auth configured if *either* is set.
        return bool(self._username or self._password)

    async def close(self) -> None:
        await self._client.aclose()

    async def _login(self) -> None:
        """Attempt to obtain a session cookie.

        Only meaningful when credentials are configured. Raises on an explicit
        auth failure so misconfigured credentials surface clearly.
        """
        if not self._has_credentials:
            # Nothing to log in with; rely on qBittorrent's auth bypass.
            return
        resp = await self._client.post(
            "/api/v2/auth/login",
            data={"username": self._username, "password": self._password},
        )
        if resp.status_code == 200 and resp.text.strip() == "Ok.":
            self._authenticated = True
            logger.info("Authenticated with qBittorrent at %s", self._base_url)
            return
        if resp.status_code == 403:
            # Too many failed attempts / banned IP.
            raise QBittorrentError(
                "qBittorrent rejected login with 403 (banned or auth misconfigured)"
            )
        raise QBittorrentError(
            f"qBittorrent login failed (status {resp.status_code}): {resp.text!r}"
        )

    async def _ensure_auth(self) -> None:
        if self._has_credentials and not self._authenticated:
            await self._login()

    async def _post_with_reauth(self, path: str, data: dict) -> httpx.Response:
        """POST, logging in on demand if the server requires it.

        With auth bypass enabled this simply posts and returns. If the server
        replies 403 — because auth is required, or a cookie expired — we try a
        login once and retry.
        """
        await self._ensure_auth()
        resp = await self._client.post(path, data=data)
        if resp.status_code == 403:
            logger.info(
                "qBittorrent returned 403 for %s; attempting (re)authentication", path
            )
            self._authenticated = False
            await self._login()
            resp = await self._client.post(path, data=data)
        return resp

    async def _get_with_reauth(self, path: str, params: dict) -> httpx.Response:
        """GET, logging in on demand if the server requires it (mirror of POST)."""
        await self._ensure_auth()
        resp = await self._client.get(path, params=params)
        if resp.status_code == 403:
            logger.info(
                "qBittorrent returned 403 for %s; attempting (re)authentication", path
            )
            self._authenticated = False
            await self._login()
            resp = await self._client.get(path, params=params)
        return resp

    async def get_preferences(self) -> dict:
        """Return qBittorrent's application preferences as a dict."""
        resp = await self._get_with_reauth("/api/v2/app/preferences", {})
        if resp.status_code != 200:
            raise QBittorrentError(
                f"qBittorrent /app/preferences failed "
                f"(status {resp.status_code}): {resp.text!r}"
            )
        return resp.json()

    async def _set_preferences(self, prefs: dict) -> None:
        """Update qBittorrent preferences.

        The API expects a form field ``json`` containing a JSON object of the
        keys to change; unspecified keys are left untouched.
        """
        resp = await self._post_with_reauth(
            "/api/v2/app/setPreferences", {"json": json.dumps(prefs)}
        )
        if resp.status_code != 200:
            raise QBittorrentError(
                f"qBittorrent /app/setPreferences failed "
                f"(status {resp.status_code}): {resp.text!r}"
            )

    async def stop_downloads(self) -> dict:
        """Stop new/active downloads while letting completed torrents seed.

        Sets the global ``max_active_downloads`` to 0, which — with torrent
        queueing enabled — pauses the *downloading* phase but leaves seeding
        untouched. Queueing is enabled if it wasn't already (otherwise the
        limit is ignored).

        Returns the original values ``{"max_active_downloads": int,
        "queueing_enabled": bool}`` so the caller can cache and later restore
        them.
        """
        prefs = await self.get_preferences()
        original = {
            "max_active_downloads": int(prefs.get("max_active_downloads", 0)),
            "queueing_enabled": bool(prefs.get("queueing_enabled", False)),
        }
        await self._set_preferences(
            {"queueing_enabled": True, "max_active_downloads": 0}
        )
        logger.info(
            "Stopped downloads (max_active_downloads=0; queueing on); "
            "was max_active_downloads=%s, queueing_enabled=%s",
            original["max_active_downloads"],
            original["queueing_enabled"],
        )
        return original

    async def restore_downloads(self, original: dict) -> None:
        """Restore the download-queue preferences captured by stop_downloads."""
        prefs = {
            "queueing_enabled": bool(original.get("queueing_enabled", True)),
            "max_active_downloads": int(original.get("max_active_downloads", 0)),
        }
        await self._set_preferences(prefs)
        logger.info(
            "Restored downloads (max_active_downloads=%s, queueing_enabled=%s)",
            prefs["max_active_downloads"],
            prefs["queueing_enabled"],
        )

    async def _toggle_all(self, modern_path: str, legacy_path: str) -> None:
        resp = await self._post_with_reauth(modern_path, {"hashes": "all"})
        if resp.status_code in (404, 405):
            # Older qBittorrent: fall back to the legacy endpoint name.
            resp = await self._post_with_reauth(legacy_path, {"hashes": "all"})
        if resp.status_code != 200:
            hint = ""
            if resp.status_code == 403:
                hint = (
                    " (403 Forbidden — set QBITTORRENT_USER/QBITTORRENT_PASS, or "
                    "enable 'Bypass authentication for clients in whitelisted IP "
                    "subnets' in qBittorrent for Pausarr's network)"
                )
            raise QBittorrentError(
                f"qBittorrent call {modern_path} failed "
                f"(status {resp.status_code}): {resp.text!r}{hint}"
            )

    async def pause_all(self) -> None:
        # v5.x: /torrents/stop, v4.x: /torrents/pause
        await self._toggle_all("/api/v2/torrents/stop", "/api/v2/torrents/pause")
        logger.info("Paused all torrents")

    async def resume_all(self) -> None:
        # v5.x: /torrents/start, v4.x: /torrents/resume
        await self._toggle_all("/api/v2/torrents/start", "/api/v2/torrents/resume")
        logger.info("Resumed all torrents")
