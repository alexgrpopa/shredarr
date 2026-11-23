"""Emby. A server-side tool with an API key only needs these few calls, so a small httpx client."""

from __future__ import annotations

from ..config import JSON
from .base import JsonApi


class EmbyClient(JsonApi):
    app = "Emby"

    def __init__(self, url: str, api_key: str) -> None:
        # Emby only knows X-Emby-Token (Jellyfin 12 and later reject it, so don't share the Jellyfin client's header).
        super().__init__(url, {"X-Emby-Token": api_key})

    def system_info(self) -> JSON:
        return dict(self.get("/System/Info"))

    def library_locations(self) -> list[str]:
        return [loc for f in self.get("/Library/VirtualFolders") for loc in f.get("Locations", [])]

    def media_updated(self, updates: list[JSON]) -> None:
        self.post("/Library/Media/Updated", {"Updates": updates})

    def refresh_library(self) -> None:
        self.post("/Library/Refresh")
