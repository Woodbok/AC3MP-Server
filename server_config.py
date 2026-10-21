"""The server's settings, shared by both processes (ac3server and onlineconfig/onlineconfig_server.py).

Read from, in order of precedence:

  1. environment variables - AC3_SERVER_ADDRESS, AC3_PLAYERS, AC3_LOG_PACKETS, AC3_LOG_DIR (and AC3_CONFIG: where the
     file is; AC3_DATA_DIR, AC3_HTTP_PORT and the port variables are read where they are used)
  2. server_config.json beside this file (or at the path in AC3_CONFIG)
  3. the defaults below, which suit a hosted server

Every setting, what it does and why, is in server_config.example.json; the start-up banner (describe) states each
one and where it came from, and names any key in the file that is not a setting.
"""
import ipaddress
import json
import os
import socket
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.environ.get("AC3_CONFIG") or os.path.join(_HERE, "server_config.json")

DEFAULT_ADDRESS = "127.0.0.1"
# Asked, in order, for this machine's public address when no interface holds one (a cloud host behind NAT, a home
# server behind a router). Each answers the caller's address as plain text. Only with `address` unset or "public".
PUBLIC_ADDRESS_SERVICES = ("https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip")


def _load_file():
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[config] {_CONFIG_PATH} could not be read ({exc}) - using defaults")
        return {}


_FILE = _load_file()


def _detect_lan_address():
    """The address this machine uses to reach the outside world.

    Opens a UDP socket to a public address and reads back the local end. No
    packet is actually sent - UDP connect() only sets the destination - so
    this works offline and contacts nothing.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 53))
        return sock.getsockname()[0]
    except OSError:
        return DEFAULT_ADDRESS
    finally:
        sock.close()


def _is_public(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_global


def _lookup_public_address():
    """This machine's address as the internet sees it, from the first of PUBLIC_ADDRESS_SERVICES that answers with
    a public IPv4 address; None when none does (offline, blocked)."""
    for url in PUBLIC_ADDRESS_SERVICES:
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                text = r.read(64).decode("ascii", "replace").strip()
        except Exception:
            continue
        if _is_public(text):
            return text
    return None


def _resolve_address():
    """-> (address, how it was found).

    Set (`address` in server_config.json, or AC3_SERVER_ADDRESS): used as it is - "auto" means this machine's LAN
    address, "public" its address as the internet sees it. Unset: a public address on one of this machine's own
    interfaces (a VPS), else the public address seen from outside (a cloud host behind NAT, a home server whose
    router forwards the ports), else the LAN address."""
    value = os.environ.get("AC3_SERVER_ADDRESS") or _FILE.get("address") or ""
    if value == "auto":
        return _detect_lan_address(), "auto: this machine's LAN address"
    if value == "public":
        found = _lookup_public_address()
        if found:
            return found, "public: as the internet sees this machine"
        return _detect_lan_address(), "public: NOT FOUND (offline?) - this machine's LAN address instead"
    if value:
        return value, "set"
    lan = _detect_lan_address()
    if _is_public(lan):
        return lan, "found: this machine's public interface"
    found = _lookup_public_address()
    if found:
        return found, "found: as the internet sees this machine - forward the ports to it"
    return lan, "found: this machine's LAN address (no public address could be found)"


SERVER_ADDRESS, ADDRESS_SOURCE = _resolve_address()


def local_addresses():
    """Every IPv4 address currently on this machine."""
    found = {DEFAULT_ADDRESS}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    return found


def is_local_address(address):
    """Whether `address` is on this machine: a socket can bind to it. Unlike looking up the host name, this also holds
    inside a container on the host's network (where the host name resolves to loopback only) and on Windows."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((address, 0))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def address_problem():
    """Why the advertised address cannot work, or None if it looks fine.

    The address is resolved once, at import, from whichever interface routes
    outside at that moment (a VPN, for one). If that interface goes away, the
    game is sent somewhere unreachable and drops to limited mode with nothing
    in the PRUDP log, because the client never arrives - so say it here.
    """
    if SERVER_ADDRESS == DEFAULT_ADDRESS:
        return None  # loopback is a deliberate same-machine choice
    if "as the internet sees" in ADDRESS_SOURCE:
        return None  # behind NAT by design: the router's address, never one of ours
    if SERVER_ADDRESS not in local_addresses() and not is_local_address(SERVER_ADDRESS):
        return (f"{SERVER_ADDRESS} is not on any interface of this machine "
                f"right now ({', '.join(sorted(local_addresses()))}). Clients "
                f"will be told to connect somewhere unreachable and will fall "
                f"back to limited mode. Restart after fixing 'address' in "
                f"server_config.json.")
    return None


def _load_players():
    raw = os.environ.get("AC3_PLAYERS")
    if raw:
        try:
            return json.loads(raw)
        except Exception as exc:
            print(f"[config] AC3_PLAYERS is not valid JSON ({exc}) - ignoring")
            return {}
    return _FILE.get("players") or {}


# Optional roster: maps the Ubisoft account name the client sends to a chosen
# display name and/or a fixed principal id. Purely a server-side alias - the
# same idea as acb-rdv's `users` table, which looks up the incoming name in a
# local database and takes the pid from there.
PLAYERS = {str(k).lower(): v for k, v in _load_players().items()}


# Starting shop balance for a player with no saved currency yet, as
# [field0, field1]. The client reads these through Shop m4 (GetCurrency) and
# adds to them through m3 (AddCurrency) after a match; match rewards go into
# the second field. Default 0, 0.
STARTING_CURRENCY = tuple((_FILE.get("currency") or [0, 0])[:2])


# Scales every price in the shop catalogue. Prices are server-side: the
# catalogue is a list of `Quazal::_DDL_ShopPricing` records, each carrying its
# own price, so what we send is what the store charges. 1.0 keeps the
# catalogue's prices; 0 makes everything free.
PRICE_MULTIPLIER = float(_FILE.get("price_multiplier", 1.0))


# Switching ability set in a match for free. Every competitive mode's GBrick_MoneySink ships
# IsMoneySinkFree="false" (the switch costs credits, more each time). On (default): the game settings we serve set
# it "true" in every mode. The reset-cooldowns purchase stays disabled as the game ships it.
FREE_ABILITY_SETS = bool(_FILE.get("free_ability_sets", True))


# Whether to push a GameSession notification to the other participants when
# somebody joins or leaves a session.
#
# Off by default: the `NotificationEvent` layout is known, but which
# GameSession participation type means "joined" is not, and an event the
# client does not expect can crash it. With it off, the join and leave paths
# log what they would have sent.
NOTIFY_PARTICIPANTS = bool(_FILE.get("notify_participants", False))


# Whether a GROUP (a session with key type id 2) tells its members about
# each other: major-7 minor 3 when somebody joins, minor 2 when somebody
# leaves, minor 1 when the leadership moves.
#
# On by default: the client's group consumer (FUN_00DD8EDB) names these
# minors itself, and without the join event the leader's client never learns
# it has a member, so it dissolves the group the moment it creates a match
# session (its "fewer than two known members" rule). With it off the log
# prints what would have been sent.
NOTIFY_GROUP_EVENTS = bool(_FILE.get("notify_group_events", True))


# Whether SearchSessions filters its results by the client's query.
#
# Off by default: the matching rules are not yet verified against the game.
# With it off every session hosted by somebody else is returned and the log
# shows which ones a filter would have dropped.
STRICT_SESSION_MATCHING = bool(_FILE.get("strict_session_matching", False))


# How a player's career statistics (proto 0x6C) are carried forward.
#
# The client reports one match's numbers with SendPlayerStats (method 2) and
# reads the career back with ReadPlayerStats (method 3) - that is where XP
# lives (group 1 info 1, op 0). On by default, each stat is folded using the
# `op` byte the client sends with it: 0 adds, 2 replaces, 3 keeps the larger.
#
# Off: every stat is replaced by the newest report, so the career shows only
# the last match. Nothing is lost either way: every raw report is kept under
# `prudp/data/stats_history/`.
STATS_ACCUMULATE = bool(_FILE.get("stats_accumulate", True))

# The Abstergo Ladder (Ladder 0x7A, ac3server/services/ladder.py): the host's game registers each match and reports
# its scores at the end; the server works out each player's ladder points and rating. The client does not keep
# private matches off the ladder, so whether they count is ours to say: `ladder_private` "count" (default) or
# "skip" (a private match then pays no points and moves no rating). No caps and no minimum player count (a player
# alone has nobody to beat and earns nothing anyway). `ladder` false: endGame is answered with an empty list (no
# income, nothing stored), while registerNewGame keeps handing out game ids.
LADDER = bool(_FILE.get("ladder", True))
LADDER_PRIVATE = "skip" if str(_FILE.get("ladder_private", "count")).strip().lower() == "skip" else "count"


# Extra maps to list in the playlists we serve: {playlist id: [map ids]}.
#
# For maps the stock config never lists - the DLC maps, into playlists 9 and
# 10 (Public / Private DLC). A map id is the uid of an `MpWorld` in the
# client's Game Bootstrap Settings; the map's archive must be in the client's
# `multi/`. Empty by default. This edits the one file the client must accept
# before matchmaking works, so remove it first if matchmaking stops working.
# See cxb/extra_maps.py.
EXTRA_MAPS = {str(k): list(v) for k, v in (_FILE.get("extra_maps") or {}).items()
              if not str(k).startswith("_")}


# The order of each playlist's `<map id>` list: "stock" (as written),
# "reverse" or "shuffle" (a fresh random order each server start).
#
# The pool comes from playlistconfig but the pick is made by the host's
# client (GBrick_MapVote). Applied after extra_maps, so added maps are
# reordered too. See cxb/extra_maps.py.
PLAYLIST_ORDER = str(_FILE.get("playlist_order", "stock")).lower()


# Where the multiplayer menu's "Extra Content" button goes.
#
# That button raises GET_EXTRA_CONTENT, which pushes OpenWalletUrlSequence,
# whose init step waits for Overlord and then opens whatever URL the Wallet
# service (0x1A0A method 1) returns. Empty by default, which still completes
# the step so the sequence does not stall; set any URL and the button opens it.
EXTRA_CONTENT_URL = str(_FILE.get("extra_content_url", "") or "")


# Who appears in a player's in-game friends list.
#
# The list is served by this server, so it decides who is on it (as acb-rdv
# does with its local users table); it is also who can be invited to a
# private match.
#
#   "everyone" (default) - every other player this server has ever seen is a
#       friend: anyone can be invited to a private match. Each login tells
#       every online player, so on a very big server prefer "requests".
#   "requests" - friendships kept by the server in prudp/data/friends.json:
#       the player's Ubisoft Connect friends who play here (the game asks
#       about them in ContactsExtensions m3), and the game's Friends m3/m4
#       AddFriend calls, recorded at once, both ways (the client has no
#       RendezVous method to accept one).
#   "explicit" - only the pairs named in `friendships` below.
FRIENDS_POLICY = str(_FILE.get("friends", "everyone")).lower()

# Friend notifications, pushed on the client's own notification channel
# (proto 0x0E, the one the game-invite push already uses): when a friendship
# is made the other player gets "friend request from X" (type 1001, as the
# ACB reference server sends it), and when a player logs in or drops, every
# online friend gets a status change (type 5001). The client's handler
# builds its FriendshipRequested / FriendStatus events from exactly these.
FRIEND_NOTIFICATIONS = bool(_FILE.get("friend_notifications", True))

# A client that vanishes without a DISCONNECT (crash, cable, VPN) would
# otherwise stay online in every friends list and come back as "name#2". The
# client pings every ~20 s; after this many seconds of silence the connection
# is treated as closed. 0 disables the sweep.
CONNECTION_TIMEOUT = float(_FILE.get("connection_timeout", 120))

# The status listener (ac3server/status.py): the game server answers GET /status on this TCP port, on 127.0.0.1 only,
# for the HTTP process's /status. Nothing to open in a firewall. 21080 by default.
STATUS_PORT = int(_FILE.get("status_port", 21080))

# Groups. AC3's group of 4 is data this server serves, not the game's limit
# (its own default is 8): `group_size` 8 lets a group of 8 form and follow its
# leader into a private match - GroupSizeMax in onlineconfig, the private
# playlists' (8, 10) groupSize, the team modes' JoinMaxGroupSize. 4 (stock) by
# default, as the group menu is untested with more. `private_min_players` 1
# lets a private match start with one player; 2 (stock) by default.
GROUP_SIZE = int(_FILE.get("group_size", 4))
PRIVATE_MIN_PLAYERS = int(_FILE.get("private_min_players", 2))

# Only consulted when friends == "explicit". Maps a principal id (as a
# string) to a list of the players it should see, each given as a pid,
# display name or account name.
EXPLICIT_FRIENDSHIPS = _FILE.get("friendships") or {}


def _env_bool(name, default):
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


# Logging. The game server mirrors its console into prudp/logs/<start time>.log.
# log_packets: every packet's RECV / SEND / RESEND line and hex dump (what reverse engineering needs; hundreds of MB an
# evening) - off by default, the RMC call lines are kept. log_max_mb: a log file rolls over to a new one at this size.
# log_keep: the newest this many log files are kept, older ones deleted (0 = all).
LOG_PACKETS = _env_bool("AC3_LOG_PACKETS", bool(_FILE.get("log_packets", False)))
LOG_MAX_MB = float(_FILE.get("log_max_mb", 100))
LOG_KEEP = int(_FILE.get("log_keep", 30))
LOG_DIR = os.environ.get("AC3_LOG_DIR") or os.path.join(_HERE, "prudp", "logs")

# Every profile upload and stats report is also kept as a snapshot (prudp/data/history,
# prudp/data/stats_history) so a misread field can be recovered. history_keep: the newest
# this many snapshots per player and kind are kept (0 = all).
HISTORY_KEEP = int(_FILE.get("history_keep", 200))

# The profile's shrink guard (ac3server/services/onlinesave.py): an OnlineSave upload is refused - the stored save
# kept, the upload set aside as history/<account>_<time>_refused.bin, the game answered as if saved - when it is under
# this fraction of the stored save's size, holds under this fraction of its persona loadout entries, or is not a
# well-formed save. A client woken from sleep can upload a near-empty save over a full one. 0 = off.
ONLINESAVE_SHRINK_GUARD = float(_FILE.get("onlinesave_shrink_guard", 0.5) or 0)

# The game's NAT type detection (ac3server/natdetect.py). At every start the game
# asks the two servers onlineconfig's punch_DetectUrls names and gets Open / Moderate / Strict, which it puts in its
# sessions and filters matchmaking by. "ubisoft" (default): Ubisoft's own detect servers, which still answered in
# October 2026. "own": this server answers on UDP nat_detect_port, +1 and +2 (or nat_detect_port on
# nat_detect_address2, a second public address of this machine: the most faithful setup), and onlineconfig points the
# game there. Open the ports in the firewall.
NAT_DETECT = str(_FILE.get("nat_detect", "ubisoft")).lower()
NAT_DETECT_PORT = int(_FILE.get("nat_detect_port", 11000))
NAT_DETECT_ADDRESS2 = str(_FILE.get("nat_detect_address2", "") or "")


# Every key a settings file may hold. Anything else is reported at start-up (a key starting with "_" is a comment).
KNOWN_KEYS = frozenset(["address", "connection_timeout", "currency", "extra_content_url", "extra_maps",
                        "free_ability_sets", "friend_notifications", "friends", "friendships", "group_size",
                        "history_keep", "ladder", "ladder_private", "log_keep", "log_max_mb", "log_packets",
                        "nat_detect", "nat_detect_address2", "nat_detect_port", "notify_group_events",
                        "notify_participants", "onlinesave_shrink_guard", "players", "playlist_order",
                        "price_multiplier", "private_min_players", "stats_accumulate", "status_port",
                        "strict_session_matching"])


def unknown_keys():
    return sorted(k for k in _FILE if not str(k).startswith("_") and k not in KNOWN_KEYS)


def describe():
    """Startup banner: every setting and where it came from, so a missing or
    mistyped config file is visible in the log."""
    if os.path.isfile(_CONFIG_PATH):
        source = "[config] using %s" % _CONFIG_PATH
    else:
        source = ("[config] no server_config.json found - using defaults. "
                  "Copy server_config.example.json to server_config.json to change them.")
    lines = [source]
    if unknown_keys():
        lines.append("[config] *** not a setting, ignored (a typo?): %s" % ", ".join(unknown_keys()))
    lines.append("[config] advertising address %s (%s)%s" % (
        SERVER_ADDRESS, ADDRESS_SOURCE,
        "" if SERVER_ADDRESS != DEFAULT_ADDRESS
        else " - loopback, only reachable from this machine"))
    problem = address_problem()
    if problem:
        lines.append("[config] *** UNREACHABLE ADDRESS *** " + problem)
    lines.append("[config] starting currency %s%s" % (
        list(STARTING_CURRENCY),
        "" if tuple(STARTING_CURRENCY) != (0, 0)
        else " (a player with no saved balance starts with nothing)"))
    if PRICE_MULTIPLIER != 1.0:
        lines.append("[config] shop prices scaled by %g" % PRICE_MULTIPLIER)
    lines.append("[config] ability set switch %s" % (
        "FREE (default) - IsMoneySinkFree in every mode" if FREE_ABILITY_SETS else "paid, as the game ships it"))
    lines.append("[config] participant notifications %s" % (
        "ON - the host is told when somebody joins or leaves"
        if NOTIFY_PARTICIPANTS else
        "off (default) - join/leave is logged, nothing is pushed"))
    lines.append("[config] group events %s" % (
        "ON (default) - a group's members are told who joins, leaves and leads"
        if NOTIFY_GROUP_EVENTS else
        "OFF - the group leader will never learn it has members and will "
        "dissolve the group on its first search"))
    lines.append("[config] session query matching %s" % (
        "STRICT - SearchSessions filters on the client's query"
        if STRICT_SESSION_MATCHING else
        "off (default) - every other host's session is returned"))
    lines.append("[config] career statistics %s" % (
        "ACCUMULATE (default) - folded with the client's own op byte "
        "(0 add, 2 replace, 3 keep the larger)"
        if STATS_ACCUMULATE else
        "LAST REPORT - every stat is replaced by the newest match"))
    lines.append("[config] Abstergo Ladder: %s" % (
        "private matches %s" % ("COUNT (default)" if LADDER_PRIVATE == "count" else "SKIPPED (ladder_private skip)")
        if LADDER else "OFF (ladder false) - endGame answered with the empty list, nothing stored"))
    lines.append("[config] friends policy %s" % {
        "requests": "requests - Ubisoft Connect friends who play here and in-game requests, kept in "
                    "prudp/data/friends.json",
        "explicit": "explicit - only the pairs listed in 'friendships'",
    }.get(FRIENDS_POLICY, "everyone (default) - every player this server has seen is a friend"))
    lines.append("[config] friend notifications %s" % (
        "ON (default) - friend requests and friends' online / offline changes are pushed"
        if FRIEND_NOTIFICATIONS else "off"))
    lines.append("[config] connection timeout %s" % (
        f"{CONNECTION_TIMEOUT:g} s of silence = disconnected (the client pings every ~20 s)"
        if CONNECTION_TIMEOUT > 0 else "off - a vanished client stays online until it reconnects"))
    if PLAYERS:
        lines.append("[config] %d player alias(es): %s"
                     % (len(PLAYERS), ", ".join(sorted(PLAYERS))))
    lines.append("[config] logging: %s; files roll over at %g MB, %s" % (
        "every packet (log_packets)" if LOG_PACKETS else "RMC calls only (log_packets off)",
        LOG_MAX_MB, f"the newest {LOG_KEEP} kept" if LOG_KEEP > 0 else "all kept"))
    lines.append("[config] profile / stats snapshots: %s" % (
        f"the newest {HISTORY_KEEP} per player kept" if HISTORY_KEEP > 0 else "all kept"))
    lines.append("[config] profile shrink guard: %s" % (
        f"ON - an upload under {ONLINESAVE_SHRINK_GUARD:g} of the stored save's size or persona loadouts is refused"
        if ONLINESAVE_SHRINK_GUARD > 0 else "off - every upload replaces the stored save"))
    lines.append("[config] NAT type detection: %s" % (
        f"OURS - UDP {NAT_DETECT_PORT}, {NAT_DETECT_PORT + 1} and "
        + (f"{NAT_DETECT_ADDRESS2}:{NAT_DETECT_PORT}" if NAT_DETECT_ADDRESS2 else f"{NAT_DETECT_PORT + 2}")
        if NAT_DETECT == "own" else "Ubisoft's detect servers (default)"))
    return "\n".join(lines)
