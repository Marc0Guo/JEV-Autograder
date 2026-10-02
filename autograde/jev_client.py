"""Client for the local open-jev System One server."""

from __future__ import annotations

from typing import Any

import httpx


class JevError(RuntimeError):
    pass


class JevClient:
    def __init__(self, base_url: str, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(base_url=self.base_url, timeout=180)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def health(self) -> dict[str, Any]:
        try:
            response = self.client.get("/health")
        except httpx.HTTPError as exc:
            raise JevError(f"JEV is not reachable at {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            raise JevError(f"JEV health failed: {response.status_code}")
        return response.json()

    def grade(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.post("/v1/systemone", json=request)
        except httpx.HTTPError as exc:
            raise JevError(f"JEV request failed: {exc}") from exc
        if response.status_code >= 400:
            raise JevError(f"JEV {response.status_code}: {response.text[:400]}")
        return response.json()
