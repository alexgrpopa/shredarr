"""rTorrent's XML-RPC API (the /RPC2 address of its web server or a proxy such as nginx or ruTorrent's). Torrents come
back in qBittorrent's shape. A small client of its own: XML-RPC is a few lines of httpx and the standard library.

rTorrent can't delete a torrent's data, so Shredarr does that itself, on the disk it sees (Settings → Path mappings).
"""

from __future__ import annotations

import os
import shutil
import time
import urllib.parse
import xmlrpc.client
from collections.abc import Callable
from typing import Any
from xml.parsers.expat import ExpatError

import httpx

from ...config import JSON
from ..base import TIMEOUT, ServiceError, ServiceHTTPError, ServiceUnreachable, SignInFailed
from .base import Torrent

KEYS = [
    "d.hash=",
    "d.name=",
    "d.base_path=",
    "d.size_bytes=",
    "d.completed_bytes=",
    "d.ratio=",
    "d.timestamp.started=",
    "d.timestamp.finished=",
    "d.state=",
    "d.is_active=",
    "d.complete=",
    "d.up.rate=",
    "d.down.rate=",
    "d.custom1=",
    "t.multicall=,t.url=",
]


def state(active: bool, complete: bool, upspeed: int) -> str:
    if not active:
        return "pausedUP" if complete else "pausedDL"
    return ("uploading" if upspeed else "stalledUP") if complete else "downloading"


class RTorrentClient:
    def __init__(self, conf: JSON, local: Callable[[str], str] | None = None) -> None:
        url = conf["url"].rstrip("/")
        self.url = url if urllib.parse.urlsplit(url).path else url + "/RPC2"
        self.local = local or (lambda p: p)
        user = conf.get("username")
        self._http = httpx.Client(timeout=TIMEOUT, auth=(user, conf.get("password", "")) if user else None)

    def call(self, method: str, *params: Any) -> Any:
        try:
            r = self._http.post(
                self.url, content=xmlrpc.client.dumps(params, method), headers={"Content-Type": "text/xml"}
            )
        except httpx.HTTPError as e:
            raise ServiceUnreachable(str(e) or type(e).__name__) from e
        if r.status_code in (401, 403):
            raise SignInFailed("wrong username or password")
        if r.status_code >= 400:
            raise ServiceHTTPError(r.status_code, r.reason_phrase)
        try:
            return xmlrpc.client.loads(r.content)[0][0]
        except xmlrpc.client.Fault as e:
            raise ServiceError(e.faultString) from e
        except (xmlrpc.client.Error, ExpatError, IndexError) as e:
            raise ServiceError("rTorrent's answer wasn't valid XML-RPC (check the address)") from e

    def torrents(self, hashes: list[str] | None = None) -> list[Torrent]:
        want = {h.lower() for h in hashes or []}
        out: list[Torrent] = []
        for (
            h,
            name,
            base,
            size,
            done,
            ratio,
            started,
            finished,
            st,
            active,
            complete,
            up,
            down,
            label,
            trackers,
        ) in self.call("d.multicall2", "", "main", *KEYS):
            if want and h.lower() not in want:
                continue
            out.append(
                {
                    "hash": h.lower(),
                    "name": name,
                    "save_path": os.path.dirname(base),
                    "content_path": base,
                    "size": size,
                    "progress": done / size if size else 0.0,
                    "ratio": ratio / 1000,
                    # shortcut: time since it finished, which also counts time paused; upgrade if that matters
                    "seeding_time": int(time.time()) - finished if complete and finished else 0,
                    "state": state(bool(st and active), bool(complete), up),
                    "upspeed": up,
                    "dlspeed": down,
                    "added_on": started,
                    "category": urllib.parse.unquote(label),
                    "tracker": trackers[0][0] if trackers else "",
                }
            )
        return out

    def remove(self, hashes: list[str], with_data: bool) -> None:
        if with_data:  # data first: if erasing then fails, the torrents are still there to try again
            for base in {self.call("d.base_path", h) for h in hashes} - {""}:  # cross-seeds can share one path
                self._delete_data(self.local(base))
        for h in hashes:
            self.call("d.erase", h)

    @staticmethod
    def _delete_data(path: str) -> None:
        if os.path.dirname(path) in ("", "/"):
            raise ServiceError(f"won't delete {path}: it's at the top of the disk")
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)
        else:
            raise ServiceError(f"can't see rTorrent's files at {path} to delete them (check Settings → Path mappings)")

    def export(self, h: str) -> bytes | None:
        return None  # rTorrent can't hand out a torrent's .torrent file over XML-RPC

    def add(self, content: bytes, filename: str, category: str) -> None:
        extra = [f"d.custom1.set={urllib.parse.quote(category)}"] if category else []
        self.call("load.raw_start", "", xmlrpc.client.Binary(content), *extra)

    def version(self) -> str:
        return "rTorrent " + str(self.call("system.client_version"))

    def close(self) -> None:
        self._http.close()
