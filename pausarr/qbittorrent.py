"""Minimal async client for the qBittorrent Web API (v2).

Only what Pausarr needs: (optionally) authenticate, then pause or resume *all*
torrents.

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

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# The minimum non-zero per-torrent rate limit qBittorrent accepts, in bytes/s.
# We use this to "pause" a single direction: 0 would mean *unlimited*, so 1 B/s
# is as close to stopped as the API allows.
THROTTLE_FLOOR_BYTES = 1


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

    async def fetch_limits(self) -> dict[str, dict[str, int]]:
        """Snapshot every torrent's per-torrent upload/download rate limits.

        Returns ``{"upload": {hash: bytes_per_s}, "download": {hash: ...}}`` with
        0 meaning "no per-torrent limit" (unlimited).

        Read from ``/torrents/info`` because the dedicated
        ``/torrents/uploadLimit`` + ``/torrents/downloadLimit`` endpoints, when
        given ``hashes=all``, return a single aggregate value (``{"all": -1}``)
        rather than a per-hash map — useless for restoring individual limits.
        ``/torrents/info`` exposes the true per-torrent values as ``up_limit`` /
        ``dl_limit``.
        """
        resp = await self._get_with_reauth("/api/v2/torrents/info", {})
        if resp.status_code != 200:
            raise QBittorrentError(
                f"qBittorrent /torrents/info failed "
                f"(status {resp.status_code}): {resp.text!r}"
            )
        upload: dict[str, int] = {}
        download: dict[str, int] = {}
        for torrent in resp.json():
            hash_ = torrent.get("hash")
            if not hash_:
                continue
            # up_limit/dl_limit are bytes/s; qBittorrent reports 0 (and, on some
            # versions, -1) for "no limit". Normalise both to 0 so restore is
            # unambiguous.
            up = torrent.get("up_limit", 0)
            dl = torrent.get("dl_limit", 0)
            upload[hash_] = up if isinstance(up, int) and up > 0 else 0
            download[hash_] = dl if isinstance(dl, int) and dl > 0 else 0
        return {"upload": upload, "download": download}

    async def _set_limit(self, direction: str, hashes: str, limit: int) -> None:
        """Set the per-torrent upload or download limit for ``hashes``.

        ``limit`` is bytes/s; 0 restores unlimited. ``hashes`` is a
        pipe-separated list of hashes or the literal ``"all"``.
        """
        path = (
            "/api/v2/torrents/setUploadLimit"
            if direction == "upload"
            else "/api/v2/torrents/setDownloadLimit"
        )
        resp = await self._post_with_reauth(path, {"hashes": hashes, "limit": limit})
        if resp.status_code != 200:
            raise QBittorrentError(
                f"qBittorrent {path} failed "
                f"(status {resp.status_code}): {resp.text!r}"
            )

    async def throttle_all(self, direction: str) -> None:
        """Throttle one direction for all torrents to the minimum (1 B/s)."""
        await self._set_limit(direction, "all", THROTTLE_FLOOR_BYTES)
        logger.info("Throttled %s for all torrents to %d B/s", direction, THROTTLE_FLOOR_BYTES)

    async def restore_limits(self, direction: str, limits: dict[str, int]) -> None:
        """Restore each torrent's original per-torrent limit for a direction.

        ``limits`` maps hash to original bytes/s (0 = unlimited). To restore
        differing values, torrents are grouped by their original limit and one
        API call is issued per distinct value (the endpoint applies a single
        ``limit`` to a batch of ``hashes``).
        """
        if not limits:
            return
        by_value: dict[int, list[str]] = {}
        for hash_, value in limits.items():
            by_value.setdefault(value, []).append(hash_)
        for value, hashes in by_value.items():
            await self._set_limit(direction, "|".join(hashes), value)
        logger.info(
            "Restored %s limits for %d torrent(s) across %d distinct value(s)",
            direction,
            len(limits),
            len(by_value),
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
