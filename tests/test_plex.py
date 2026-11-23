"""With Plex (and Emby, see test_emby.py), deletes are noticed on disk (they can't send them), and the server is
rescanned after Shredarr's own changes. Tests without golden files run for both."""

from __future__ import annotations

import os
import time

import pytest

from tests.conftest import StartFn
from tests.fakes.lab import Lab
from tests.harness import Shredarr, wait_for

SERVERS = pytest.mark.parametrize("media", ["plex", "emby"])


def watching(s: Shredarr) -> None:
    """The first look at the library is the baseline; deletes are what disappears after it."""
    wait_for(lambda: os.path.exists(os.path.join(s.config_dir, "library-files.json")), what="first library scan")


def test_movie_deleted_on_disk(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008, ratio=9, seeding_days=99)
    s = start(media="plex")
    watching(s)
    lab.delete_movie_files(m)
    s.wait_log("^deleted torrent")
    assert len(lab.radarr.requests("DELETE")) == 1 and len(lab.seerr.requests("DELETE")) == 1
    wait_for(lambda: "Deleted in Plex" in lab.ntfy.titles(), what="the notification")
    golden("log", s.log())


@SERVERS
def test_upgrade_or_rename_is_not_a_delete(lab: Lab, start: StartFn, media: str) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008)
    show = lab.add_series("Paper Moons", seasons={1: 2})
    lab.add_release(show, 1, [1, 2])
    s = start(media=media)
    watching(s)
    os.rename(m.file, m.file.replace("Bluray-1080p", "Remux-1080p"))  # renamed: same inode
    lab.add_release(show, 1, [1], release="Paper.Moons.S01E01.2160p-UPGRADE")  # replaced by a new file

    time.sleep(4)
    assert not any("deleted" in x for x in s.log()), s.log()


def test_episode_and_season_deleted_on_disk(lab: Lab, start: StartFn, golden) -> None:
    show = lab.add_series("Paper Moons", seasons={1: 3, 2: 2})
    lab.add_release(show, 1, range(1, 4))
    lab.add_release(show, 2, [1, 2])
    s = start(media="plex")
    watching(s)
    lab.delete_episode_files(show, 1, [2])
    s.wait_log("^Plex deleted Episode")
    lab.delete_season_folder(show, 2)
    s.wait_log("^Plex deleted Season")
    s.wait_log("removed 2 episode file record\\(s\\) of 'Paper Moons' S02")
    assert show.record["seasons"][1]["monitored"] is False and show.record["seasons"][0]["monitored"] is True
    golden("log", [x for x in s.log() if not x.startswith("deleted torrent")])


def writes(lab: Lab) -> list[tuple[str, str, str]]:
    return [
        (f.name, r.method, r.path) for f in (lab.sonarr, lab.radarr, lab.seerr) for r in f.calls if r.method != "GET"
    ]


@SERVERS
def test_deletes_noticed_in_dry_run_change_nothing_and_work_once_live(lab: Lab, start: StartFn, media: str) -> None:
    movie, other = (
        lab.add_movie(t, y, ratio=9, seeding_days=99) for t, y in (("Big Buck Bunny", 2008), ("Sintel", 2010))
    )
    show, other_show = (
        lab.add_series("Paper Moons", seasons={1: 3, 2: 2}),
        lab.add_series("Spring Tide", seasons={1: 3, 2: 2}),
    )
    for x in (show, other_show):
        lab.add_release(x, 1, range(1, 4), ratio=9, seeding_days=99)
        lab.add_release(x, 2, [1, 2], ratio=9, seeding_days=99)
    s = start(media=media, dry_run=True)
    watching(s)
    torrents = set(lab.torrents)
    lab.delete_movie_files(movie)
    s.wait_log(r"^  \[dry run\] would have removed 'Big Buck Bunny' from Seerr")
    lab.delete_episode_files(show, 1, [2])
    s.wait_log(r"^  \[dry run\] would have removed 1 episode file record\(s\) of 'Paper Moons' S01E02")
    lab.delete_season_folder(show, 2)
    s.wait_log(r"^  \[dry run\] would have removed 2 episode file record\(s\) of 'Paper Moons' S02")
    log = s.log()
    assert sum("[dry run] would have queued 1 torrent(s) for cleanup after seeding" in x for x in log) == 3, log
    assert not any("newly queued" in x for x in log), log
    s.sweep()
    assert not s.pending() and not writes(lab) and set(lab.torrents) == torrents
    assert not lab.qbittorrent.requests("POST", "/api/v2/torrents/delete")
    # nothing was queued, so the torrents of what was deleted show up as leftovers the user can still clean up
    rows = {t["hash"]: t for t in s.api("/api/torrents")["torrents"]}
    assert movie.torrent and rows[movie.torrent.hash]["leftover"] is True

    # live: the same kind of deletes now run the whole chain
    s.post("/rules", data={"mode": "either", "min_seed_days": "14", "min_ratio": "2"})
    lab.delete_movie_files(other)
    s.wait_log(r"^deleted torrent 'Sintel")
    lab.delete_episode_files(other_show, 1, [2])
    wait_for(lambda: [p["source"] for p in s.pending().values()] == ["episode"], what="the season pack queued")
    s.sweep()  # it stays until its other episodes are gone from the library too
    assert s.pending()
    lab.delete_season_folder(other_show, 2)
    s.wait_log(r"^deleted torrent 'Spring.Tide.S02")
    assert not any("dry run" in x for x in s.log()[len(log) :])
    assert len(lab.radarr.requests("DELETE")) == 1 and len(lab.seerr.requests("DELETE")) == 1
    assert [(r.method, r.path) for r in lab.sonarr.calls if r.method != "GET"] == [
        ("PUT", "/api/v3/episode/monitor"),
        ("DELETE", "/api/v3/episodefile/bulk"),
        ("PUT", "/api/v3/episode/monitor"),
        ("PUT", "/api/v3/series/2"),
        ("DELETE", "/api/v3/episodefile/bulk"),
    ]
    assert other.torrent and other.torrent.hash not in lab.torrents


def test_dry_run_delete_of_an_already_queued_torrent_reports_no_new_one(lab: Lab, start: StartFn) -> None:
    show = lab.add_series("Paper Moons", seasons={1: 3})
    lab.add_release(show, 1, range(1, 4), ratio=1, seeding_days=1)
    s = start(media="plex")
    watching(s)
    lab.delete_episode_files(show, 1, [1])
    wait_for(lambda: len(s.pending()) == 1, what="the season pack queued")
    s.post("/rules", data={"mode": "either", "min_seed_days": "14", "min_ratio": "2", "dry_run": "on"})
    mark = len(s.log())
    lab.delete_episode_files(show, 1, [2])
    s.wait_log(r"^  \[dry run\] would have removed 1 episode file record\(s\) of 'Paper Moons' S01E02", after=mark)
    assert not any("would have queued" in x for x in s.log()[mark:])
    assert len(s.pending()) == 1


@SERVERS
def test_whole_show_deleted_on_disk(lab: Lab, start: StartFn, media: str) -> None:
    show = lab.add_series("Paper Moons", seasons={1: 2})
    lab.add_release(show, 1, [1, 2])
    s = start(media=media)
    watching(s)
    lab.delete_series_folder(show)
    s.wait_log(f"^{media.title()} deleted Series: Paper Moons")
    s.wait_log("removed 'Paper Moons' from Sonarr")


@SERVERS
def test_disk_going_offline_is_not_a_delete(lab: Lab, start: StartFn, media: str) -> None:
    for i in range(25):
        lab.add_movie(f"Movie {i:02d}", 2000 + i, torrent=False)
    s = start(media=media)
    watching(s)
    for m in list(lab.movie_refs.values())[:24]:
        lab.delete_movie_files(m)
    s.wait_log("looks like a disk going offline")
    time.sleep(2)  # a few more looks at the library: the message isn't repeated on each one
    lab.delete_movie_files(list(lab.movie_refs.values())[24])  # nor when the count changes while it's still away
    time.sleep(2)
    assert sum("looks like a disk going offline" in x for x in s.log()) == 1
    assert not lab.radarr.requests("DELETE")
    wait_for(lambda: "Shredarr hit a problem" in lab.ntfy.titles(), what="a notification, in case it was a real delete")


@SERVERS
def test_no_leftovers_queued_while_the_library_watch_sees_a_disk_offline(lab: Lab, start: StartFn, media: str) -> None:
    for i in range(25):
        lab.add_movie(f"Movie {i:02d}", 2000 + i, torrent=False)
    m = lab.add_movie("Spring", 2019)
    assert m.torrent
    s = start(media=media, behavior={"auto_queue_leftovers": True}, env={"SHREDARR_LEFTOVER_WAIT": "0"})
    watching(s)
    s.sweep()  # the sweep at startup is done before the disk goes away
    for x in lab.movie_refs.values():
        lab.delete_movie_files(x)
    s.wait_log("looks like a disk going offline")
    s.sweep()  # only Spring has a torrent: one leftover isn't too many, but the library watch says a disk is away
    assert not s.pending()


def test_delete_now_and_undo_rescan_plex(lab: Lab, start: StartFn, golden) -> None:
    m = lab.add_movie("Big Buck Bunny", 2008)
    u = lab.add_movie("Raygun", 2024, ratio=0.1)
    s = start(media="plex")
    watching(s)
    assert m.torrent and u.torrent
    golden("plan", s.api("/api/plan", hash=m.torrent.hash))
    s.post("/api/delete-now", {"hash": m.torrent.hash})
    s.wait_log("^  done: Rescan Plex")
    assert lab.plex.refreshes() == [("1", None)]
    assert not lab.jellyfin.requests()
    lab.delete_movie_files(u)
    s.wait_status(u.torrent.hash, "^seeding")
    lab.plex.clear_calls()
    s.post("/api/undo", {"hash": u.torrent.hash})
    s.wait_log("^  done: re-imported")
    wait_for(lab.plex.refreshes, what="a Plex scan")
    assert lab.plex.refreshes() == [("1", u.folder)]

    time.sleep(3)
    assert not any("Plex deleted Movie: Big Buck Bunny" in x for x in s.log()), "Shredarr's own delete isn't reported"
    golden("log", [x for x in s.log() if "Raygun" not in x or "Undo" in x])
