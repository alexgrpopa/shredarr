"""With Emby, like Plex, deletes are noticed on disk (Emby can't send them) and Emby is told about Shredarr's own
changes. The scenarios without golden files run for both servers in test_plex.py."""

from __future__ import annotations

import os

from tests.conftest import StartFn
from tests.fakes.lab import Lab
from tests.harness import wait_for
from tests.test_plex import watching


def test_movie_deleted_on_disk(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008, ratio=9, seeding_days=99)
    s = start(media="emby")
    watching(s)
    lab.delete_movie_files(m)
    s.wait_log("^deleted torrent")
    assert len(lab.radarr.requests("DELETE")) == 1 and len(lab.seerr.requests("DELETE")) == 1
    wait_for(lambda: "Deleted in Emby" in lab.ntfy.titles(), what="the notification")
    assert not lab.jellyfin.requests() and not lab.plex.requests()
    golden("log", s.log())


def test_episode_and_season_deleted_on_disk(lab: Lab, start: StartFn, golden) -> None:
    show = lab.add_series("Paper Moons", seasons={1: 3, 2: 2})
    pack = lab.add_release(show, 1, range(1, 4))
    lab.add_release(show, 2, [1, 2])
    s = start(media="emby")
    watching(s)
    lab.delete_episode_files(show, 1, [2])
    s.wait_log("^Emby deleted Episode")
    lab.delete_season_folder(show, 2)
    s.wait_log("^Emby deleted Season")
    s.wait_log("removed 2 episode file record\\(s\\) of 'Paper Moons' S02")
    assert show.record["seasons"][1]["monitored"] is False and show.record["seasons"][0]["monitored"] is True
    assert pack.hash in lab.torrents  # the season pack still has episodes in the library
    golden("log", [x for x in s.log() if not x.startswith("deleted torrent")])


def test_delete_now_and_undo_tell_emby(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008)
    u = lab.add_movie("Raygun", 2024, ratio=0.1)
    s = start(media="emby")
    watching(s)
    assert m.torrent and u.torrent
    golden("plan", s.api("/api/plan", hash=m.torrent.hash))
    s.post("/api/delete-now", {"hash": m.torrent.hash})
    s.wait_log("^  done: Rescan Emby")
    assert lab.emby.updates() == [("Deleted", m.file)]
    assert not lab.jellyfin.requests() and not lab.plex.requests()
    lab.delete_movie_files(u)
    s.wait_status(u.torrent.hash, "^seeding")
    lab.emby.clear_calls()
    s.post("/api/undo", {"hash": u.torrent.hash})
    s.wait_log("^  done: re-imported")
    wait_for(lab.emby.updates, what="an Emby update")
    assert lab.emby.updates() == [("Created", u.folder), ("Created", os.path.join(u.folder, u.torrent.name + ".mkv"))]
    assert not any("Emby deleted Movie: Big Buck Bunny" in x for x in s.log()), "Shredarr's own delete isn't reported"
    golden("log", [x for x in s.log() if "Raygun" not in x or "Undo" in x])


def test_delete_now_unmanaged_torrent_refreshes_emby(lab: Lab, start: StartFn) -> None:
    t = lab.add_unmanaged("Python.Course.2024", ["01 Intro.mp4"])
    s = start(media="emby")
    s.post("/api/delete-now", {"hash": t.hash})
    s.wait_log("^  done: Rescan")
    assert lab.emby.requests("POST", "/Library/Refresh") and t.hash not in lab.torrents


def test_health_checks_emby_only(lab: Lab, start: StartFn) -> None:
    s = start(media="emby")
    h = wait_for(lambda: len(s.api("/api/health")) == 5 and s.api("/api/health"), what="health checks")
    assert h["emby"]["ok"] and h["emby"]["msg"] == "Emby 4.9.1.0 · Lab" and "jellyfin" not in h
    assert not lab.jellyfin.requests()
