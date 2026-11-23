"""Talking to the garrison control plane (remote mode).

``BundleClient`` fetches ``GET /v1/sdk/bundle`` with the app key, honours
ETags, and keeps a disk cache of the last good bundle. The cache file is
named after a hash of the key and holds the bundle, revision, ETag and URL:
never the key, and the bundle itself only ever names env vars, never
secret values.

Fetching is synchronous (httpx.Client) and run in a worker thread by the
refresh loop, so any httpx transport works, including test clients.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .compat import getenv

DEFAULT_CACHE_DIR = Path(getenv("GARRISON_CACHE_DIR") or Path.home() / ".cache" / "garrison")


class ServiceError(Exception):
    """The service answered, but not with a bundle (e.g. the key was rejected)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class BundleResult:
    app: str
    revision: int
    etag: str
    bundle: dict[str, Any]
    from_cache: bool = False


class BundleClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        cache_dir: str | Path | None = None,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._key = api_key
        self._timeout = timeout
        self._transport = transport
        self.cache_path = Path(cache_dir or DEFAULT_CACHE_DIR) / f"{hashlib.sha256(api_key.encode()).hexdigest()[:16]}.json"

    def fetch(self, etag: str | None = None) -> BundleResult | None:
        """The current bundle, or None if *etag* is still current (304).
        Raises httpx.HTTPError if the service can't be reached, ServiceError
        if it answers with an error."""
        headers = {"Authorization": f"Bearer {self._key}"}
        if etag:
            headers["If-None-Match"] = f'"{etag}"'
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            resp = client.get(f"{self.url}/v1/sdk/bundle", headers=headers)
        if resp.status_code == 304:
            return None
        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise ServiceError(resp.status_code, f"{resp.status_code} from {self.url}: {detail}")
        body = resp.json()
        return BundleResult(app=body["app"], revision=int(body["revision"]), etag=body["etag"], bundle=body["bundle"])

    def load_cache(self) -> BundleResult | None:
        try:
            data = json.loads(self.cache_path.read_text())
            return BundleResult(app=data["app"], revision=int(data["revision"]), etag=data["etag"],
                                bundle=data["bundle"], from_cache=True)
        except (OSError, ValueError, KeyError):
            return None

    def save_cache(self, result: BundleResult) -> None:
        """Atomic write: a crash mid-write never leaves a broken cache."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"app": result.app, "revision": result.revision, "etag": result.etag,
                   "bundle": result.bundle, "url": self.url, "saved_at": time.time()}
        fd, tmp = tempfile.mkstemp(dir=self.cache_path.parent, prefix=".bundle-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self.cache_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
