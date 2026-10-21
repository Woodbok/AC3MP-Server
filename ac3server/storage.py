"""Where the server keeps its files, and how it writes them.

Everything that cannot be rebuilt - the player directory, friendships, and each player's profile blob,
career, balance and purchases - lives as plain JSON or small binary files in DATA_DIR (`prudp/data/`, or the
AC3_DATA_DIR environment variable). The game settings data the server reads
but never writes (the embedded default, map names) lives in CXB_DIR (`cxb/`).

A state file is written whole or not at all (atomic_write), and one that will not read is moved aside, never
overwritten (set_aside).
"""

import os
import re
import time

import server_config as config


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CXB_DIR = os.path.join(ROOT, "cxb")
# AC3_DATA_DIR moves it.
DATA_DIR = os.environ.get("AC3_DATA_DIR") or os.path.join(ROOT, "prudp", "data")


def atomic_write(path, data):
    """Write a state file whole or not at all: a temp file beside it, then a rename.

    A crash or a full disk in the middle of a plain open("w") leaves a
    truncated career, balance or profile behind; a rename never does.
    """
    tmp = f"{path}.tmp"
    mode, kw = ("wb", {}) if isinstance(data, (bytes, bytearray)) else ("w", {"encoding": "utf-8"})
    with open(tmp, mode, **kw) as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def prune_history(folder, user, ext):
    """Keep the newest HISTORY_KEEP snapshots of `user` in `folder` (0 = keep all)."""
    if config.HISTORY_KEEP <= 0:
        return
    pattern = re.compile(re.escape(user) + r"_\d{8}_\d{6}" + re.escape(ext) + "$")
    try:
        names = sorted(n for n in os.listdir(folder) if pattern.match(n))
        for name in names[:-config.HISTORY_KEEP]:
            os.remove(os.path.join(folder, name))
    except OSError as exc:
        print(f"[history] could not prune {folder} for {user!r}: {exc}")


def set_aside(path, exc):
    """A state file that will not read is moved to <path>.unreadable-<time>, never left to be overwritten.

    Every store starts from nothing when its file is missing, so without this the next save would replace a damaged
    file - and whatever it held - with a near-empty one. Moved aside, it can be repaired and put back (with the server
    stopped). -> the new path, or None if it could not be moved (then it is left where it is)."""
    aside = f"{path}.unreadable-{time.strftime('%Y%m%d_%H%M%S')}"
    n = 1
    while os.path.exists(aside):                 # never over an earlier one
        n += 1
        aside = f"{path}.unreadable-{time.strftime('%Y%m%d_%H%M%S')}-{n}"
    try:
        os.replace(path, aside)
    except OSError as move_exc:
        print(f"*** {path} could not be read ({exc}) and could not be moved aside ({move_exc})")
        return None
    print(f"*** {path} could not be read ({exc}): moved aside to {aside} - nothing in it is lost. Repair it and put "
          f"it back with the server stopped.")
    return aside
