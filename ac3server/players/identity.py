"""Who is on each connection: the identity a login resolves to, and the tables the services look players up in.

An identity is {"account", "name", "pid"}: the Ubisoft name the login sent, the name other players see, and the
principal id (pid) Quazal knows them by. It is held per connection address, so two clients signed in to the same
Ubisoft account never collide: the second one becomes "name#2", with its own pid and its own files.

    PLAYER_IDENTITIES   addr -> identity, from login until the connection ends (connections.py)
    CONNECTED_PIDS      addr -> pid, set at login
    CONNECTED_USERS     addr -> display name, set at login

The pid is derived from the name (stable_user_pid), so it is the same on every run without storage; `players` in
server_config.json can give a Ubisoft name a chosen display name and a fixed pid. Pids start at 1000: Quazal
reserves the ids below for the system, and the game's Overlord middleware refuses to send anything for a pid under
1000. The "Tracking" telemetry pseudo-account keeps acb-rdv's 105; it never reaches the Overlord code.

Every per-player file (profile blob, career, balance, purchases) is named after the identity's ACCOUNT, not its
display name (onlinesave_user), so a chosen name never orphans a career.
"""

import os
import zlib

import server_config as config
from ac3server import storage

from ac3server.players.directory import remember_player


USER_PID_MIN = 1000
TRACKING_USER_PID = 105

PLAYER_IDENTITIES = {}  # addr -> {"account", "name", "pid"}
CONNECTED_PIDS = {}     # addr -> principal id handed out in LoginEx
CONNECTED_USERS = {}    # addr -> display name, from LoginEx


def stable_user_pid(username: str) -> int:
    return USER_PID_MIN + (zlib.crc32(username.lower().encode("utf-8")) % 100_000_000)


_FILE_WARNINGS_GIVEN = set()


def _warn_about_files_left_behind(addr, account, name):
    """Say so if a chosen display name has stranded somebody's old files.

    Per-player files are named after the account. A file named after the
    display name instead (for example after a display name was added in
    `players`) would simply go unread.
    """
    if not account or account.lower() == (name or "").lower():
        return
    if account in _FILE_WARNINGS_GIVEN:
        return
    _FILE_WARNINGS_GIVEN.add(account)

    def clean(s):
        return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in s)

    stranded = []
    for prefix, suffix in (("onlinesave_", ".bin"), ("currency_", ".json"),
                           ("stats_", ".json"), ("purchases_", ".json")):
        by_name = os.path.join(storage.DATA_DIR, f"{prefix}{clean(name)}{suffix}")
        by_account = os.path.join(storage.DATA_DIR, f"{prefix}{clean(account)}{suffix}")
        if os.path.isfile(by_name) and not os.path.isfile(by_account):
            stranded.append((os.path.basename(by_name), os.path.basename(by_account)))
    if stranded:
        print(f"[{addr}]   NOTE: {account!r} plays as {name!r}, and these files "
              f"are still named after the display name - rename them or that "
              f"player starts fresh:")
        for old, new in stranded:
            print(f"           {old}  ->  {new}")


def resolve_player_identity(addr, account):
    """Decide the name and principal id for a connection.

    `account` is what the client reported; it is never modified or forged,
    only mapped to a server-side identity.
    """
    account = account or "player"
    entry = config.PLAYERS.get(account.lower(), {})
    name = entry.get("name") or account
    pid = entry.get("pid")

    # The same name again from the same IP address on another port: the same game reconnecting (after a sleep, or
    # limited mode) before its old connection timed out. It takes the identity over, as one player is one connection;
    # keeping both would show the player twice and file this session's career under "name#2".
    for other, i in list(PLAYER_IDENTITIES.items()):
        if other != addr and other[0] == addr[0] and i["account"].lower() == account.lower():
            print(f"[{addr}]   {account!r} logged in again from the same address: the identity moves here from "
                  f"{other}")
            PLAYER_IDENTITIES.pop(other, None)
            CONNECTED_PIDS.pop(other, None)
            CONNECTED_USERS.pop(other, None)

    # already-connected accounts elsewhere: keep distinct identities per connection
    taken = {i["name"].lower() for a, i in PLAYER_IDENTITIES.items()
             if a != addr and i["account"].lower() == account.lower()}
    if taken:
        suffix = 2
        while f"{name}#{suffix}".lower() in taken:
            suffix += 1
        name = f"{name}#{suffix}"
        pid = None  # a derived id, so the duplicate does not share the first's
        print(f"[{addr}]   account {account!r} is already connected from another "
              f"address - this connection is {name!r} so ids and saves stay separate")

    identity = {"account": account, "name": name,
                "pid": pid if pid is not None else stable_user_pid(name)}
    PLAYER_IDENTITIES[addr] = identity
    _warn_about_files_left_behind(addr, account, name)
    remember_player(identity)
    return identity


def online_pids():
    """Principal ids with a live secure connection right now."""
    return {i["pid"] for i in PLAYER_IDENTITIES.values()}


def onlinesave_user(addr):
    """The key every per-player file is named after.

    It is the **account**, not the display name, so that giving somebody a
    chosen name in `players` does not orphan their profile blob, credits
    and career. The `#2` suffix a second connection from one account gets
    is kept, because that mechanism exists precisely to hold those files
    apart.

    Falls back to the display name when no identity has been resolved yet.
    """
    identity = PLAYER_IDENTITIES.get(addr)
    user = None
    if identity:
        user = identity.get("account") or identity.get("name")
        name = identity.get("name") or ""
        if user and "#" in name:
            user = f"{user}#{name.rsplit('#', 1)[1]}"
    user = user or CONNECTED_USERS.get(addr) or "player"
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in user)

# Every name we send into the game's contacts code goes through one constructor (FUN_011517c0) that ends with
# _strncpy_s(dst, 48, name, 48): a name of 48 bytes or more does not get truncated, it aborts the game.
CONTACT_NAME_MAX = 47


def contact_safe_name(name):
    name = str(name)
    if len(name.encode("utf-8")) <= CONTACT_NAME_MAX:
        return name
    trimmed = name.encode("utf-8")[:CONTACT_NAME_MAX].decode("utf-8", "ignore")
    print(f"      name {name!r} is too long for the client's 48-byte buffer "
          f"- sending {trimmed!r} instead (a longer one aborts the game)")
    return trimmed


def addr_of_pid(pid):
    """The connection address of a pid that is online, or None."""
    for candidate, identity in PLAYER_IDENTITIES.items():
        if identity.get("pid") == pid:
            return candidate
    return None
