"""rTorrent's XML-RPC API (the /RPC2 address of its web server or a proxy such as nginx or ruTorrent's). Torrents come
back in qBittorrent's shape. A small client of its own: XML-RPC is a few lines of httpx and the standard library.

rTorrent can't delete a torrent's data, so Shredarr does that itself, on the disk it sees (Settings → Path mappings).
It deletes only the torrent's own files (never a whole folder that may be shared) and the folders that leave empty.
"""

from __future__ import annotations

import contextlib
import os
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
    "d.directory=",
    "d.is_multi_file=",
    "d.hashing=",
    "d.state=",
    "d.is_active=",
    "d.complete=",
    "d.up.rate=",
    "d.down.rate=",
    "d.custom1=",
    "t.multicall=,t.url=",
]


class UnknownHash(ServiceError):
    """rTorrent doesn't have this torrent (anymore)."""


def state(active: bool, complete: bool, upspeed: int, hashing: bool) -> str:
    if hashing:
        return "checkingUP" if complete else "checkingDL"
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
            raise (UnknownHash if "info-hash" in e.faultString else ServiceError)(e.faultString) from e
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
            directory,
            multi,
            hashing,
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
            path = base or (directory if multi else os.path.join(directory, name))  # base is empty while closed
            end = finished or started
            out.append(
                {
                    "hash": h.lower(),
                    "name": name,
                    "save_path": os.path.dirname(path),
                    "content_path": path,
                    "size": size,
                    "progress": done / size if size else 0.0,
                    "ratio": ratio / 1000,
                    # shortcut: time since it finished, which also counts time paused; upgrade if that matters
                    "seeding_time": int(time.time()) - end if complete and end else 0,
                    "state": state(bool(st and active), bool(complete), up, bool(hashing)),
                    "upspeed": up,
                    "dlspeed": down,
                    "added_on": started or finished,
                    "category": urllib.parse.unquote(label),
                    "tracker": trackers[0][0] if trackers else "",
                }
            )
        return out

    def remove(self, hashes: list[str], with_data: bool) -> None:
        for h in (h.upper() for h in hashes):
            try:
                if with_data:  # data first: if erasing then fails, the torrent is still there to try again
                    self._delete_data(h)
                self.call("d.erase", h)
            except UnknownHash:  # already removed, e.g. by an earlier try
                pass

    def _delete_data(self, h: str) -> None:
        directory = self.local(self.call("d.directory", h))
        name, multi = self.call("d.name", h), bool(self.call("d.is_multi_file", h))
        # a multi-file torrent's d.directory is its own folder; a single-file one's is the (maybe shared) folder it's in
        files = [f for (f,) in self.call("f.multicall", h, "", "f.path=")] if multi else [name]
        if not os.path.isdir(directory) and not os.path.isdir(os.path.dirname(directory)):
            raise ServiceError(
                f"can't see rTorrent's files at {directory} to delete them (check Settings → Path mappings)"
            )
        folders = {directory} if multi else set()  # a missing file is already deleted; leftover folders go if empty
        for f in files:
            parts = os.path.normpath(f).split(os.sep)
            if os.path.isabs(f) or ".." in parts:
                raise ServiceError(f"won't delete {f}: it points outside the torrent's folder")
            try:
                os.remove(os.path.join(directory, f))  # a symlink is removed itself, never followed
            except FileNotFoundError:
                pass
            except OSError as e:
                raise ServiceError(f"can't delete {os.path.join(directory, f)}: {e.strerror}") from e
            if multi:
                folders.update(os.path.join(directory, *parts[:i]) for i in range(1, len(parts)))
        for d in sorted(folders, key=len, reverse=True):
            with contextlib.suppress(OSError):
                os.rmdir(d)  # only if empty

    def export(self, h: str) -> bytes | None:
        return None  # rTorrent can't hand out a torrent's .torrent file over XML-RPC

    def add(self, content: bytes, filename: str, category: str) -> None:
        extra = [f"d.custom1.set={urllib.parse.quote(category)}"] if category else []
        self.call("load.raw_start", "", xmlrpc.client.Binary(content), *extra)

    def version(self) -> str:
        return "rTorrent " + str(self.call("system.client_version"))

    def close(self) -> None:
        self._http.close()
