"""Emby. Same API as Jellyfin for the few calls Shredarr needs; only the API key header differs."""

from __future__ import annotations

from .jellyfin import JellyfinClient


class EmbyClient(JellyfinClient):
    app = "Emby"

    @staticmethod
    def auth_header(api_key: str) -> dict[str, str]:
        return {"X-Emby-Token": api_key}
