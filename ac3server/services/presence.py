"""RichPresence (protocol 0x6D): what makes a friend show as online.

    m1 SetPresence  [u32 presenceStringId][u16 len][blob]   -> nothing      the game tells us its own presence
    m2 GetPresence  [list<u32> pids]                        -> [list] { [u32 pid][u8 online][u32 presenceStringId]
                                                                        [u16 len][blob] }

The friends-row builder (FUN_007D025E) branches on the online byte this reply gives each contact; with no record a
friend is Offline, whatever the friends list said, and their invite and join options are never even computed. We
store what each game sets and hand it back for the players who are online; a player not online has no record.

The blob is a length-prefixed raw buffer, NOT a Quazal string (no NUL): ASCII hex of
[u32 count] { [u32 id][u32 type][u32 value] }. Layout from the game's reader (FUN_011948B0); Ubisoft's m2 replies
parse exactly against it.
"""

import struct
import time

from ac3server.gamesession.state import ATTR_GAME_MODE, GAME_SESSIONS
from ac3server.players.friendships import name_of_pid
from ac3server.players.identity import CONNECTED_PIDS, online_pids
from ac3server.protocol.datatypes import read_u32
from ac3server.protocol.rmc import build_rmc_response


PRESENCE = {}  # pid -> (presence_string_id, raw blob bytes)
# A game that drops to LIMITED MODE and comes back online re-logs in (LoginEx, RegisterEx, its data boot) but its
# online menus may never return - no SetPresence, no friends / session polls, invitations ACKed and ignored. A presence
# kept from before would show it online and joinable, so a new secure connection starts with no presence.
PRESENCE_AT = {}     # pid -> when its game last set its presence
REGISTERED_AT = {}   # pid -> when its game's current secure connection registered


def presence_reset(pid, now=None):
    """A new secure connection (RegisterEx): the old presence belongs to the old game state."""
    if pid is None:
        return
    PRESENCE.pop(pid, None)
    PRESENCE_AT.pop(pid, None)
    REGISTERED_AT[pid] = time.time() if now is None else now


def presence_forget(pid):
    """The player's last connection ended: nothing of theirs is handed out any more."""
    PRESENCE.pop(pid, None)
    PRESENCE_AT.pop(pid, None)
    REGISTERED_AT.pop(pid, None)



def handle_richpresence_set(call_id, params, addr):   # m1 SetPresence
    pid = CONNECTED_PIDS.get(addr)
    try:
        presence_id, pos = read_u32(params, 0)
        length = struct.unpack_from('<H', params, pos)[0]
        pos += 2
        blob = params[pos:pos + length]
        pos += length
        if pid is not None:
            PRESENCE[pid] = (presence_id, blob)
            PRESENCE_AT[pid] = time.time()
        print(f"[{addr}]   RichPresence.SetPresence: pid {pid} id {presence_id:#x}, "
              f"{length}-byte blob{'' if pos == len(params) else ' (trailing bytes!)'}")
    except Exception as e:
        print(f"[{addr}]   RichPresence.SetPresence: could not parse ({e})")
    return "RichPresence.1", build_rmc_response(0x6D, call_id, 1, b'')


# An invitation is ignored by the invitee's game unless the inviter's presence has property 4, the game mode - and a
# player who has not played a match yet has none. So a presence handed out without property 4 gets one: the mode of
# the key-type-1 session the player is in, else PRESENCE_FILL_MODE (11, a mode the game itself reports). None turns
# it off.
PRESENCE_FILL_MODE = 0x0B
_PRESENCE_FILLED = set()


def _presence_with_mode(pid, blob):
    if PRESENCE_FILL_MODE is None:
        return blob
    try:
        text = blob.decode("ascii")
        raw = bytes.fromhex(text)
        count = struct.unpack_from("<I", raw, 0)[0]
        if len(raw) != 4 + 12 * count:
            return blob
        props = [struct.unpack_from("<III", raw, 4 + 12 * i) for i in range(count)]
    except (ValueError, UnicodeDecodeError, struct.error):
        return blob
    if any(p[0] == 4 for p in props):
        return blob
    mode = PRESENCE_FILL_MODE
    for session in list(GAME_SESSIONS.values()):
        if session.get("type_id") == 1 and pid in session.get("participants", []):
            m = session.get("attributes", {}).get(ATTR_GAME_MODE)
            if m not in (None, 0xFFFFFFFF):
                mode = m
    out = struct.pack("<I", count + 1) + b"".join(struct.pack("<III", *p) for p in props) + struct.pack("<III", 4, 1, mode)
    if pid not in _PRESENCE_FILLED:
        _PRESENCE_FILLED.add(pid)
        print(f"[presence] {name_of_pid(pid)!r} has no game mode in their presence: mode {mode} given "
              f"(an invitation from them is shown only with one)")
    hexed = out.hex()
    return (hexed.upper() if text.isupper() else hexed).encode("ascii")


def handle_richpresence_get(call_id, params, addr):   # m2 GetPresence
    wanted = []
    try:
        count, pos = read_u32(params, 0)
        for _ in range(count):
            pid, pos = read_u32(params, pos)
            wanted.append(pid)
    except Exception as e:
        print(f"[{addr}]   RichPresence.GetPresence: could not parse ({e}) - empty list")
        return "RichPresence.2", build_rmc_response(0x6D, call_id, 2,
                                                    struct.pack('<I', 0))
    online = online_pids()
    records = []
    skipped = []
    for pid in wanted:
        if pid not in online:
            continue  # no record at all means offline, which is correct
        presence = PRESENCE.get(pid)
        if presence is None:
            # connected but no presence set yet: offline until it is, rather than a made-up blob
            skipped.append(pid)
            continue
        records.append((pid, presence[0], _presence_with_mode(pid, presence[1])))
    resp = bytearray(struct.pack('<I', len(records)))
    for pid, presence_id, blob in records:
        resp += struct.pack('<I', pid)
        resp.append(1)                       # online
        resp += struct.pack('<I', presence_id)
        resp += struct.pack('<H', len(blob))
        resp += blob                          # raw, no NUL
    print(f"[{addr}]   RichPresence.GetPresence: {len(wanted)} pid(s) asked -> "
          f"{len(records)} online"
          + (f", {len(skipped)} connected but no presence set yet {skipped}"
             if skipped else ""))
    return "RichPresence.2", build_rmc_response(0x6D, call_id, 2, bytes(resp))
