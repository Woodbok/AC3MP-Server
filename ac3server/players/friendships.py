"""Friendships: the server's own friends table, and telling friends about each other.

Who is friends with whom is this server's decision, not Uplay's, and is mutual. `friends` in server_config.json
picks how it is decided:

    "everyone"  (default) every other player the server has ever seen.
    "requests"  the table in DATA_DIR/friends.json, plus any pairs listed in `friendships`. A friend request IS a
                friendship at once: the game has no RendezVous method to accept one with (the Friends protocol
                compiled into it is methods 3, 4 and 12 only). Requests arrive as Friends m3/m4 AddFriend calls, and
                a player's Ubisoft Connect friends who play here are added from ContactsExtensions m3
                (services/contacts.py).
    "explicit"  only the pairs listed in `friendships`.

Friends are told about each other by notification (notifications.py): a request (type 1001, param2 = 2) and a
status change (type 5001, param2 = 1 online / 0 offline), shaped as acb-rdv shapes them and as the game's handler
(FUN_0117CD30) reads them. Off with `friend_notifications` false.
"""

import json
import os

import server_config as config
from ac3server import notifications
from ac3server import storage

from ac3server.players.directory import PLAYER_DIRECTORY, refresh_player_directory
from ac3server.players.identity import addr_of_pid, contact_safe_name, online_pids
from ac3server.storage import atomic_write, set_aside


FRIENDSHIPS = {}  # pid -> set of pids
_FRIENDSHIPS_LOADED = []
_FRIENDSHIPS_MTIME = [None]   # the file as last read or written: an edit made elsewhere is read again


def _friendships_path():
    return os.path.join(storage.DATA_DIR, "friends.json")


def _load_friendships():
    """Read friends.json once, and again whenever it changed on disk (a hand edit) - never cleared when the file is
    missing."""
    path = _friendships_path()
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        mtime = None
    if _FRIENDSHIPS_LOADED and (mtime is None or mtime == _FRIENDSHIPS_MTIME[0]):
        return
    again = bool(_FRIENDSHIPS_LOADED)
    if not again:
        _FRIENDSHIPS_LOADED.append(True)
    if mtime is None:
        return
    try:
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)
        table = {}
        for a, bs in stored.items():
            for b in bs:
                table.setdefault(int(a), set()).add(int(b))
                table.setdefault(int(b), set()).add(int(a))
        FRIENDSHIPS.clear()
        FRIENDSHIPS.update(table)
        _FRIENDSHIPS_MTIME[0] = mtime
        print(f"[friends] {sum(len(v) for v in FRIENDSHIPS.values()) // 2} friendship(s) "
              f"{'re-read (the file changed)' if again else 'loaded'} from {path}")
    except Exception as exc:
        set_aside(path, exc)
        _FRIENDSHIPS_MTIME[0] = None
        if again:                       # keep the table this process has, and write it back
            _save_friendships()


def _save_friendships():
    try:
        os.makedirs(storage.DATA_DIR, exist_ok=True)
        atomic_write(_friendships_path(),
                      json.dumps({str(a): sorted(bs) for a, bs in sorted(FRIENDSHIPS.items()) if bs}, indent=1))
        _FRIENDSHIPS_MTIME[0] = os.stat(_friendships_path()).st_mtime_ns
    except OSError as exc:
        print(f"[friends] could not save ({exc}) - kept in memory only")


def add_friendship(a, b):
    """Make a and b friends, both ways. True if that is new."""
    _load_friendships()
    if a == b or b in FRIENDSHIPS.get(a, ()):
        return False
    FRIENDSHIPS.setdefault(a, set()).add(b)
    FRIENDSHIPS.setdefault(b, set()).add(a)
    _save_friendships()
    return True


def friends_of(pid):
    """Everyone `pid` should see in their friends list, by the `friends` policy (see the module docstring)."""
    refresh_player_directory()
    others = [p for p in PLAYER_DIRECTORY.values() if p["pid"] != pid]
    if config.FRIENDS_POLICY == "everyone":
        return others
    allowed = config.EXPLICIT_FRIENDSHIPS.get(str(pid), [])
    names = {str(a).lower() for a in allowed}
    explicit = [p for p in others
                if str(p["pid"]) in names or p["name"].lower() in names
                or str(p.get("account", "")).lower() in names]
    if config.FRIENDS_POLICY == "explicit":
        return explicit
    _load_friendships()
    table = FRIENDSHIPS.get(pid, set())
    return explicit + [p for p in others if p["pid"] in table and p not in explicit]


FRIENDS_NOTIFICATION_TYPE = 1001
FRIEND_STATUS_NOTIFICATION_TYPE = 5001
FRIENDSHIP_REQUESTED, FRIENDSHIP_ACCEPTED, FRIENDSHIP_DECLINED = 2, 1, 0


def name_of_pid(pid):
    """A player's display name: the directory's, else the pid."""
    entry = PLAYER_DIRECTORY.get(pid)
    if entry:
        return entry["name"]
    return str(pid)


def notify_friendship(target_pid, sender_pid, param2, what):
    """Push a Friends notification to `target_pid` if it is connected."""
    if not config.FRIEND_NOTIFICATIONS:
        return False
    target = addr_of_pid(target_pid)
    if target is None:
        return False
    return notifications.send_notification(target, pid_source=sender_pid, ntype=FRIENDS_NOTIFICATION_TYPE,
                             param1=sender_pid, param2=param2,
                             str_param=contact_safe_name(name_of_pid(sender_pid)), param3=0,
                             label=f"Friends: {what} from {name_of_pid(sender_pid)!r}")


def notify_friend_status(target_pid, friend_pid, online):
    if not config.FRIEND_NOTIFICATIONS:
        return False
    target = addr_of_pid(target_pid)
    if target is None:
        return False
    return notifications.send_notification(target, pid_source=friend_pid, ntype=FRIEND_STATUS_NOTIFICATION_TYPE,
                             param1=friend_pid, param2=1 if online else 0,
                             str_param=contact_safe_name(name_of_pid(friend_pid)), param3=0,
                             label=f"Friends: {name_of_pid(friend_pid)!r} {'online' if online else 'offline'}")


def announce_friend_status(pid, online):
    """A player logged in / dropped: tell each of their friends who is online."""
    if not config.FRIEND_NOTIFICATIONS or pid is None:
        return
    here = online_pids()
    told = [f["name"] for f in friends_of(pid)
            if f["pid"] in here and notify_friend_status(f["pid"], pid, online)]
    if told:
        print(f"      {name_of_pid(pid)!r} {'online' if online else 'offline'}: "
              f"{len(told)} friend(s) told {told}")


def friendship_made(requester_pid, target_pid):
    """After add_friendship() returned True: the other player hears of the
    request, and the requester's screen gets the new friend's status so its
    list refreshes at once (the friendship is mutual already)."""
    notify_friendship(target_pid, requester_pid, FRIENDSHIP_REQUESTED, "friend request")
    if target_pid in online_pids():
        notify_friend_status(requester_pid, target_pid, True)
