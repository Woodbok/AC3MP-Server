"""The player directory: everyone this server has ever seen log in, kept in DATA_DIR/players.json.

A friends list has to name people who are not connected, so the per-connection identities (players/identity.py)
are not enough: every login is recorded here by principal id, with the account name, the display name and when the
player was last seen - the same idea as acb-rdv's local `users` table. The file is re-read when it changes on disk
(a hand edit), so the next login does not write a removed player back.
"""

import json
import os
import time

from ac3server import storage

from ac3server.storage import atomic_write, set_aside


PLAYER_DIRECTORY = {}       # pid -> {"pid", "name", "account", "last_seen"}
_DIRECTORY_MTIME = [None]   # the file as last read or written: an edit made elsewhere is read again


def _directory_path():
    return os.path.join(storage.DATA_DIR, "players.json")


def _load_player_directory():
    """{pid: entry}, or None when the file will not read (it is then moved aside, storage.set_aside)."""
    path = _directory_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)
        return {int(p["pid"]): p for p in stored}
    except Exception as exc:
        set_aside(path, exc)
        return None


def refresh_player_directory():
    """Load the roster once, and again whenever players.json changed on disk."""
    try:
        mtime = os.stat(_directory_path()).st_mtime_ns
    except OSError:
        mtime = None
    if PLAYER_DIRECTORY and (mtime is None or mtime == _DIRECTORY_MTIME[0]):
        return
    if mtime is None:
        return
    fresh = _load_player_directory()
    if fresh is None:
        # the file was damaged (an edit made elsewhere) and is set aside: keep the roster this process has, and
        # write it back so the file exists again
        _DIRECTORY_MTIME[0] = None
        if PLAYER_DIRECTORY:
            _save_player_directory()
        return
    if PLAYER_DIRECTORY and fresh.keys() != PLAYER_DIRECTORY.keys():
        print(f"[directory] players.json changed on disk: {len(fresh)} player(s) known")
    PLAYER_DIRECTORY.clear()
    PLAYER_DIRECTORY.update(fresh)
    _DIRECTORY_MTIME[0] = mtime


def _save_player_directory():
    try:
        os.makedirs(storage.DATA_DIR, exist_ok=True)
        atomic_write(_directory_path(),
                     json.dumps(sorted(PLAYER_DIRECTORY.values(), key=lambda p: p["pid"]), indent=1))
        _DIRECTORY_MTIME[0] = os.stat(_directory_path()).st_mtime_ns
    except OSError as exc:
        print(f"[directory] could not save ({exc}) - kept in memory only")


def remember_player(identity):
    """Record a player in the roster, so they can be listed while offline."""
    refresh_player_directory()
    pid = identity["pid"]
    existing = PLAYER_DIRECTORY.get(pid, {})
    entry = {"pid": pid, "name": identity["name"],
             "account": identity.get("account", ""),
             "last_seen": int(time.time())}
    if existing != entry:
        PLAYER_DIRECTORY[pid] = entry
        _save_player_directory()
        if pid not in existing:
            print(f"[directory] {identity['name']!r} (pid {pid}) added to the "
                  f"roster - {len(PLAYER_DIRECTORY)} player(s) known")
    return entry


def find_player(pid=None, name=None):
    """A directory entry by pid, or by display or account name (any case)."""
    refresh_player_directory()
    if pid is not None:
        return PLAYER_DIRECTORY.get(pid)
    if name:
        wanted = name.lower()
        for entry in PLAYER_DIRECTORY.values():
            if entry["name"].lower() == wanted or \
                    str(entry.get("account", "")).lower() == wanted:
                return entry
    return None
