"""Friends (protocol 0x14): the game's friends list, served from this server's own friendships.

    m3  AddFriendWithDetails        [u32 pid][u32 details][string message]      -> RelationshipData
    m4  AddFriendByNameWithDetails  [string name][u32 details][string message]  -> RelationshipData
    m12 GetDetailedList             [u8 relationship][bool reversed]            -> [list] { FriendData }

These three are the only Friends methods compiled into the game (from its stub table), so there is no accept,
decline or block to answer. Records, from the game's own DDL and acb-rdv, which agree field for field:

    FriendData        [u32 pid][string name][u8 relationship][u32 details][string status]
    RelationshipData  [u32 pid][string name][u8 relationship][u32 details][u8 status]

The status is a STRING in one and a BYTE in the other; mixing them up desynchronises the reply. The game counts a
friend online unless the status string is exactly "Offline" (TaskRDV_ListFriends, FUN_01186a40). It asks for the
list once per relationship (1 friend, 2 pending, 3 blocked); only "friend" gets a roster.

GetDetailedList also gates the game's Overlord start-up: without an answer the game waits on "Updating Animus
parameters" for ever. Whether a friend's row reads online is decided elsewhere, by RichPresence
(services/presence.py).
"""

import struct

import server_config as config

from ac3server.players.directory import find_player
from ac3server.players.friendships import add_friendship, friends_of, friendship_made
from ac3server.players.identity import CONNECTED_PIDS, contact_safe_name, online_pids
from ac3server.protocol.datatypes import read_string, read_u32, write_string
from ac3server.protocol.rmc import build_rmc_error_response, build_rmc_response


FRIENDS_PROTO = 0x14

FRIEND_RELATIONSHIP_FRIEND = 1
FRIEND_RELATIONSHIP_PENDING = 2
FRIEND_RELATIONSHIP_BLOCKED = 3

# exactly "Offline" means offline; anything else, even "", reads as online
FRIEND_STATUS_OFFLINE = "Offline"
FRIEND_STATUS_ONLINE = "Online"


def _build_friend_record(entry, online):
    out = bytearray()
    out += struct.pack('<I', entry["pid"])
    write_string(out, contact_safe_name(entry["name"]))
    out.append(FRIEND_RELATIONSHIP_FRIEND)
    out += struct.pack('<I', 0)                      # m_uiDetails
    write_string(out, FRIEND_STATUS_ONLINE if online else FRIEND_STATUS_OFFLINE)
    return bytes(out)


def _build_relationship_record(pid, name, relationship, status_byte):
    out = bytearray()
    out += struct.pack('<I', pid)
    write_string(out, contact_safe_name(name))
    out.append(relationship)
    out += struct.pack('<I', 0)          # m_uiDetails
    out.append(status_byte)              # m_byStatus - a byte, not a string
    return bytes(out)


def _add_friend_reply(call_id, method, entry, addr, how):
    if entry is None:
        print(f"[{addr}]   Friends.AddFriend ({how}): not on this server -> "
              f"error {QERROR_FRIENDS_USER_NOT_FOUND:#010x}")
        return build_rmc_error_response(FRIENDS_PROTO, call_id,
                                        QERROR_FRIENDS_USER_NOT_FOUND)
    online = entry["pid"] in online_pids()
    requester = CONNECTED_PIDS.get(addr)
    if config.FRIENDS_POLICY not in ("everyone", "explicit") and requester is not None \
            and entry["pid"] != requester:
        new = add_friendship(requester, entry["pid"])
        print(f"[{addr}]   Friends.AddFriend ({how}): {entry['name']!r} "
              f"({'online' if online else 'offline'}) - "
              f"{'friendship recorded, both ways' if new else 'already friends'}")
        if new:
            friendship_made(requester, entry["pid"])
    else:
        print(f"[{addr}]   Friends.AddFriend ({how}): {entry['name']!r} "
              f"({'online' if online else 'offline'}) - already a friend under the "
              f"'{config.FRIENDS_POLICY}' policy, confirming it")
    return build_rmc_response(FRIENDS_PROTO, call_id, method,
                              _build_relationship_record(
                                  entry["pid"], entry["name"],
                                  FRIEND_RELATIONSHIP_FRIEND, 1 if online else 0))


# acb-rdv's Friends_UserNotFound (Quazal's Friends error space is 0x806Cxxxx)
QERROR_FRIENDS_USER_NOT_FOUND = 0x806C0100


def handle_friends_add_with_details(call_id, params, addr):    # m3
    pid = None
    try:
        pid, pos = read_u32(params, 0)
    except Exception as e:
        print(f"[{addr}]   Friends.AddFriendWithDetails: could not parse ({e})")
    return "Friends.AddFriendWithDetails", _add_friend_reply(
        call_id, 3, find_player(pid=pid), addr, f"pid {pid}")


def handle_friends_add_by_name_with_details(call_id, params, addr):  # m4
    name = None
    try:
        name, pos = read_string(params, 0)
    except Exception as e:
        print(f"[{addr}]   Friends.AddFriendByNameWithDetails: could not parse ({e})")
    return "Friends.AddFriendByNameWithDetails", _add_friend_reply(
        call_id, 4, find_player(name=name), addr, f"name {name!r}")


def handle_friends_get_detailed_list(call_id, params, addr):    # m12
    relationship = params[0] if len(params) >= 1 else None
    reversed_ = bool(params[1]) if len(params) >= 2 else None
    pid = CONNECTED_PIDS.get(addr)

    # Only the "friends" query gets a roster: a request is a friendship at once, so nothing is ever pending or blocked
    if relationship != FRIEND_RELATIONSHIP_FRIEND or pid is None:
        print(f"[{addr}]   Friends.GetDetailedList relationship={relationship} "
              f"reversed={reversed_} -> empty list"
              + ("" if pid is not None else " (caller not identified yet)"))
        return "Friends.GetDetailedList", build_rmc_response(
            FRIENDS_PROTO, call_id, 12, struct.pack('<I', 0))

    online = online_pids()
    friends = sorted(friends_of(pid), key=lambda f: f["name"].lower())
    resp = bytearray(struct.pack('<I', len(friends)))
    for entry in friends:
        resp += _build_friend_record(entry, entry["pid"] in online)
    print(f"[{addr}]   Friends.GetDetailedList relationship={relationship} "
          f"reversed={reversed_} -> {len(friends)} friend(s): "
          f"{[(f['name'], 'online' if f['pid'] in online else 'offline') for f in friends]}")
    return "Friends.GetDetailedList", build_rmc_response(
        FRIENDS_PROTO, call_id, 12, bytes(resp))
