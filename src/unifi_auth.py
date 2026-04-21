"""
unifi_auth.py — minimal, local-only UniFi Protect API client.

Purpose:
    Automate the two parts of camera adoption that normally require
    poking around the Protect UI:

      1. Getting the adoption token (normally: "scan this QR code")
      2. Accepting a pending camera as adopted (normally: "click adopt")

Everything happens against the local UniFi controller / UNVR.
No external services, no third-party APIs.

Usage:
    async with UniFiProtectClient(host, username, password) as client:
        token = await client.fetch_adoption_token()
        ...
        await client.approve_pending("AA:BB:CC:11:22:33", "Front Door")
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import aiohttp

logger = logging.getLogger("unifi_auth")


class UniFiAuthError(Exception):
    """Raised when we can't talk to the UniFi controller."""


class UniFiProtectClient:
    """
    Talks to a local UniFi OS / Protect controller. Keeps the surface small:
    just login, fetch-token, list-cameras, approve-adoption.

    Protect's public API is technically undocumented and has shifted across
    versions, so every call is tried defensively and failures are logged
    rather than fatal.
    """

    def __init__(
        self,
        host: str,
        username: str = "",
        password: str = "",
        api_key: str = "",
    ):
        self.host = host if host.startswith("http") else f"https://{host}"
        self.username = username
        self.password = password
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None
        self._csrf: Optional[str] = None

    # ─── Lifecycle ──────────────────────────────────────────────────────────

    async def __aenter__(self) -> "UniFiProtectClient":
        # UniFi ships self-signed certs on the local controller. That's fine
        # for a machine sat on the same LAN.
        connector = aiohttp.TCPConnector(ssl=False)
        self._session = aiohttp.ClientSession(connector=connector)
        try:
            # Always log in when we have username + password. The legacy
            # /proxy/protect/api/* endpoints (notably manage-payload) only
            # accept cookie + CSRF auth and reject X-API-KEY, so we need
            # the session cookie regardless of whether an api_key is set.
            # The api_key (if any) is attached via _headers() and will be
            # picked up by the integration API.
            if self.username and self.password:
                await self._login()
        except BaseException:
            # If login fails, __aexit__ never runs, so we'd leak the session
            # and aiohttp would print a noisy "Unclosed client session" at
            # garbage-collect time. Close it ourselves before re-raising.
            await self._session.close()
            self._session = None
            raise
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    # ─── Auth ───────────────────────────────────────────────────────────────

    async def _login(self) -> None:
        url = f"{self.host}/api/auth/login"
        try:
            async with self._session.post(
                url,
                json={"username": self.username, "password": self.password},
                headers={"Content-Type": "application/json"},
            ) as r:
                if r.status != 200:
                    body = (await r.text())[:200]
                    raise UniFiAuthError(
                        f"Login to {self.host} failed ({r.status}): {body}"
                    )
                # UDM / UNVR hands us the CSRF token in a response header.
                self._csrf = (
                    r.headers.get("X-CSRF-Token")
                    or r.headers.get("x-csrf-token")
                )
                logger.info("Logged in to UniFi controller at %s", self.host)
        except aiohttp.ClientError as e:
            raise UniFiAuthError(f"Could not reach {self.host}: {e}") from e

    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["X-API-KEY"] = self.api_key
        if self._csrf:
            h["X-CSRF-Token"] = self._csrf
        if extra:
            h.update(extra)
        return h

    # ─── Adoption token ─────────────────────────────────────────────────────

    async def fetch_adoption_token(self) -> str:
        """
        Try several known endpoints to retrieve a fresh adoption token.

        Strategy, in order:
          1. GET /proxy/protect/api/cameras/manage-payload — keshavdv upstream
             path; reads ``mgmt.token``. Most likely to still work on older
             Protect builds and the canonical field name for adoption.
          2. GET /proxy/protect/api/bootstrap — older field-name variants
             (authToken, adoptionToken, accessKey) inside the top-level or
             nested ``nvr`` object.
          3. GET /proxy/protect/api/cameras/qr — returns JSON on some builds.
          4. Same endpoint but decoded as a QR PNG via OpenCV.

        On failure we log the status / content-type / top-level JSON keys
        from every attempt so the user (or a future maintainer) can see
        what their Protect build actually returned — the docs only cover
        what *used* to exist, not what ships today.

        Raises UniFiAuthError if every path fails.
        """
        attempts: list[str] = []

        # ── 1. manage-payload (upstream path) ──────────────────────────────
        mp_url = f"{self.host}/proxy/protect/api/cameras/manage-payload"
        try:
            async with self._session.get(mp_url, headers=self._headers()) as r:
                attempts.append(f"manage-payload → HTTP {r.status}")
                if r.status == 200:
                    data = await r.json(content_type=None)
                    # keshavdv docs: response["mgmt"]["token"]
                    mgmt = (data or {}).get("mgmt") if isinstance(data, dict) else None
                    token = (mgmt or {}).get("token") if isinstance(mgmt, dict) else None
                    if isinstance(token, str) and len(token) > 8:
                        logger.info("Fetched adoption token from /cameras/manage-payload")
                        return token
                    # Some builds put the token at the top level instead.
                    token = self._extract_token(data)
                    if token:
                        logger.info("Fetched adoption token from /cameras/manage-payload (top-level)")
                        return token
                    if isinstance(data, dict):
                        attempts[-1] += f" keys={sorted(data.keys())[:10]}"
        except Exception as e:
            attempts.append(f"manage-payload → error: {e}")

        # ── 2. bootstrap JSON ──────────────────────────────────────────────
        bootstrap_url = f"{self.host}/proxy/protect/api/bootstrap"
        try:
            async with self._session.get(bootstrap_url, headers=self._headers()) as r:
                attempts.append(f"bootstrap → HTTP {r.status}")
                if r.status == 200:
                    data = await r.json(content_type=None)
                    token = self._extract_token(data)
                    if token:
                        logger.info("Fetched adoption token from /bootstrap")
                        return token
                    if isinstance(data, dict):
                        attempts[-1] += f" keys={sorted(data.keys())[:10]}"
        except Exception as e:
            attempts.append(f"bootstrap → error: {e}")

        # ── 3/4. /cameras/qr as JSON or PNG ────────────────────────────────
        qr_url = f"{self.host}/proxy/protect/api/cameras/qr"
        try:
            async with self._session.get(qr_url, headers=self._headers()) as r:
                ctype = r.headers.get("Content-Type", "")
                attempts.append(f"cameras/qr → HTTP {r.status} ({ctype})")
                if r.status == 200 and "json" in ctype:
                    data = await r.json(content_type=None)
                    token = self._extract_token(data)
                    if token:
                        logger.info("Fetched adoption token from /cameras/qr JSON")
                        return token
                elif r.status == 200 and ("image" in ctype or "octet" in ctype):
                    png_bytes = await r.read()
                    token = self._decode_qr(png_bytes)
                    if token:
                        logger.info("Decoded adoption token from QR image")
                        return token
        except Exception as e:
            attempts.append(f"cameras/qr → error: {e}")

        logger.warning(
            "Adoption-token auto-fetch failed. Endpoint attempts: %s",
            "; ".join(attempts) or "(none tried)",
        )
        raise UniFiAuthError(
            "Could not fetch an adoption token automatically. Your Protect "
            "version may not expose any of the known endpoints. Open "
            f"{self.host}/proxy/protect/api/cameras/manage-payload in a browser "
            "(while logged in) and paste the mgmt.token value into the UniFi "
            "tab's 'Adoption token' field."
        )

    @staticmethod
    def _extract_token(data) -> Optional[str]:
        """
        Walk a bootstrap / qr-response blob looking for something that looks
        like an adoption token. Protect has used several field names over
        the years.
        """
        if not isinstance(data, dict):
            return None

        candidate_keys = (
            "authToken",
            "adoptionToken",
            "accessKey",
            "token",
            "a",  # the QR payload sometimes uses short keys
        )

        # Top-level
        for key in candidate_keys:
            val = data.get(key)
            if isinstance(val, str) and len(val) > 8:
                return val

        # /bootstrap nests most of its info under `nvr`
        nvr = data.get("nvr") or {}
        for key in candidate_keys:
            val = nvr.get(key)
            if isinstance(val, str) and len(val) > 8:
                return val

        return None

    @staticmethod
    def _decode_qr(png_bytes: bytes) -> Optional[str]:
        """
        Decode a QR code image locally using OpenCV's built-in QRCodeDetector
        (already a dep via opencv-python-headless — no extra packages).
        """
        try:
            import cv2
            import numpy as np

            arr = np.frombuffer(png_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                return None
            payload, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
            if not payload:
                return None

            # QR payload is typically JSON, e.g. {"h": "...", "a": "<token>"}
            import json

            try:
                decoded = json.loads(payload)
                token = UniFiProtectClient._extract_token(decoded)
                if token:
                    return token
            except ValueError:
                pass

            # Or a URL with ?token=... / ?a=...
            m = re.search(r"[?&](?:token|a)=([^&\s]+)", payload)
            if m:
                return m.group(1)

            # Last resort: strip known prefixes
            if len(payload) > 20 and all(c.isalnum() or c in "-_." for c in payload):
                return payload
        except Exception as e:
            logger.debug("QR decode failed: %s", e)
        return None

    # ─── Auto-adoption ──────────────────────────────────────────────────────

    async def list_cameras(self) -> list:
        """Return every camera Protect currently knows about."""
        url = f"{self.host}/proxy/protect/api/cameras"
        try:
            async with self._session.get(url, headers=self._headers()) as r:
                if r.status != 200:
                    logger.debug("list_cameras returned %s", r.status)
                    return []
                data = await r.json(content_type=None)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and "cameras" in data:
                    return data["cameras"]
                return []
        except Exception as e:
            logger.debug("list_cameras failed: %s", e)
            return []

    async def find_pending(self, mac: str) -> Optional[dict]:
        """Find a pending-adoption camera by fake MAC address."""
        target = mac.lower().replace(":", "")
        for cam in await self.list_cameras():
            cam_mac = (cam.get("mac") or "").lower().replace(":", "")
            if cam_mac == target:
                return cam
        return None

    async def approve_pending(self, mac: str, name: str, timeout: float = 60.0) -> bool:
        """
        Wait for a camera with this MAC to appear in Protect as pending,
        then PATCH it to accepted. Returns True on success.
        """
        cam = None
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            cam = await self.find_pending(mac)
            if cam and (cam.get("isAdopting") or not cam.get("isAdopted")):
                break
            await asyncio.sleep(2.0)
        else:
            logger.warning("No pending camera found for %s after %ss", mac, timeout)
            return False

        cam_id = cam.get("id")
        if not cam_id:
            return False

        url = f"{self.host}/proxy/protect/api/cameras/{cam_id}"
        headers = self._headers({"Content-Type": "application/json"})
        payload = {"name": name, "isAdopting": False, "isAdopted": True}
        try:
            async with self._session.patch(url, json=payload, headers=headers) as r:
                if r.status == 200:
                    logger.info("Auto-adopted camera %s (%s)", name, mac)
                    return True
                logger.warning(
                    "Adoption PATCH for %s returned %s", mac, r.status
                )
                return False
        except Exception as e:
            logger.warning("Adoption PATCH for %s failed: %s", mac, e)
            return False
