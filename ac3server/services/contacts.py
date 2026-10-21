"""ContactsExtensions (protocol 0x72): the lists behind the game's friends screen.

    m3  RetrieveGameSessionFromContact  [list<string> names]  -> [list] { [string name][u32 pid][u8 inMatch] }
    m4  (resolve pids)                  [list<u32> pids]      -> the same records
    m5, m6                              answered empty, as Ubisoft's server answered both

The game's consumer of both replies (FUN_01187B10) makes one contact per record and never matches them back to what
it asked, so what the lists hold is this server's decision:

  * m3 is asked with the player's Ubisoft Connect friends' names. Under the "requests" policy those who play on this
    server become friends here too; the reply is the player's friends list (players/friendships.py).
  * m4 resolves pids the game already knows to names.

The record's flag is bit 0 = "in a match": the game then looks the session up with GameSession m24 and offers Join.
It is not "online" (that is RichPresence). Groups and lobbies do not count (gamesession/state.match_session_of).
The record layout follows the game's own reader (FUN_01193B80), which also parses Ubisoft's m4 replies exactly.
"""

import struct

import server_config as config

from ac3server.gamesession.state import match_session_of
from ac3server.players.directory import PLAYER_DIRECTORY, find_player, refresh_player_directory
from ac3server.players.friendships import add_friendship, friends_of, friendship_made
from ac3server.players.identity import CONNECTED_PIDS, contact_safe_name
from ac3server.protocol.datatypes import read_string, read_u32, write_string
from ac3server.protocol.rmc import build_rmc_response


def _in_session_flag(pid):
    """The contact flag: bit 0 = in a match (the client then looks the
    session up with GameSession m24 and offers Join). Not "online" - that
    is RichPresence's."""
    session_id, _session = match_session_of(pid)
    return 1 if session_id is not None else 0


def _contact_records(entries):
    resp = bytearray(struct.pack('<I', len(entries)))
    for entry, flag in entries:
        write_string(resp, contact_safe_name(entry["name"]))
        resp += struct.pack('<I', entry["pid"])
        resp.append(flag)
    return bytes(resp)


def handle_contacts_game_sessions(call_id, params, addr):     # 0x72 m3
    names = []
    try:
        count, pos = read_u32(params, 0)
        for _ in range(count):
            name, pos = read_string(params, pos)
            names.append(name)
    except Exception as e:
        print(f"[{addr}]   ContactsExtensions.RetrieveGameSessionFromContact: "
              f"could not parse the {len(params)}-byte request ({e}) - empty reply")
        return "ContactsExtensions.RetrieveGameSessionFromContact", \
            build_rmc_response(0x72, call_id, 3, struct.pack('<I', 0))

    refresh_player_directory()
    requester = CONNECTED_PIDS.get(addr)

    # 1. under "requests": every Ubisoft Connect friend asked about who is a player here becomes a friend here
    made = []
    if requester is not None and config.FRIENDS_POLICY not in ("everyone", "explicit"):
        for n in names:
            entry = find_player(name=n)
            if entry is not None and entry["pid"] != requester and add_friendship(requester, entry["pid"]):
                made.append(entry["name"])
                friendship_made(requester, entry["pid"])
        if made:
            print(f"[{addr}]   Friends: {len(made)} new friendship(s) from the Ubisoft Connect names: {made}")

    # 2. the friends list itself
    if requester is None:
        matches = []
        print(f"[{addr}]   ContactsExtensions.RetrieveGameSessionFromContact: caller not identified yet - empty")
    else:
        friends = sorted(friends_of(requester), key=lambda f: f["name"].lower())
        matches = [(entry, _in_session_flag(entry["pid"])) for entry in friends]
        print(f"[{addr}]   ContactsExtensions.RetrieveGameSessionFromContact: "
              f"{len(names)} name(s) asked, "
              f"{len(matches)} friend(s) answered "
              f"{[(e['name'], 'in a match' if f else 'not in a match') for e, f in matches]}")
    return "ContactsExtensions.RetrieveGameSessionFromContact", \
        build_rmc_response(0x72, call_id, 3, _contact_records(matches))


def handle_contacts_resolve_pids(call_id, params, addr):      # 0x72 m4
    pids = []
    try:
        count, pos = read_u32(params, 0)
        for _ in range(count):
            pid, pos = read_u32(params, pos)
            pids.append(pid)
        if pos != len(params):
            print(f"[{addr}]   ContactsExtensions.ResolvePids: {len(params) - pos} "
                  f"trailing byte(s) not consumed")
    except Exception as e:
        print(f"[{addr}]   ContactsExtensions.ResolvePids: could not parse ({e})")
    refresh_player_directory()
    records = {}  # pid -> (entry, flag), in request order
    for pid in pids:
        entry = PLAYER_DIRECTORY.get(pid)
        if entry is None:
            # unknown ids are left out, as Ubisoft's server dropped ids it did not know
            continue
        records[pid] = (entry, _in_session_flag(pid))
    entries = list(records.values())
    print(f"[{addr}]   ContactsExtensions.ResolvePids: {len(pids)} asked, "
          f"{len(entries)} answered -> "
          f"{[(e['name'], 'in a match' if f else 'not in a match') for e, f in entries]}")
    return "ContactsExtensions.ResolvePids", build_rmc_response(
        0x72, call_id, 4, _contact_records(entries))


def handle_contacts_method5(call_id, params, addr):
    return "0x72.5", build_rmc_response(0x72, call_id, 5, b'')


def handle_contacts_method6(call_id, params, addr):
    return "0x72.6", build_rmc_response(0x72, call_id, 6, b'')
