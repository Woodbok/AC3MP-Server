"""Groups: players who search and play together. A group is a GameSession with key type 2.

Two channels keep a group's members in step, and both are pushes:

  * membership - notification major 7, minors 1/2/3 (leader changed, member left, member joined), sent by
    `notify_group_members` whenever the server changes who is in the group. The leader's game learns its members
    ONLY this way, and dissolves a group it believes has fewer than two (state.py).
  * the leader's broadcasts - GameSession m20 SendTextMessage [key][string]: the leader's game says what it is
    doing (type 3: its session record and URLs -> the members join it; type 4: its search parameters -> the members
    search alike; type 2: "return to the lobby"). We relay each one, verbatim, as notification 7012 with
    param3 = the key type, to EVERY member - the sender too, because the leader's own game only acts on "return
    to the lobby" when it receives it back.

Known to work: a group holds together through search, lobby, a match played to completion and "leave with my
group". Untested: a member leaving on their own, a kick, the leader disconnecting.
"""

import struct
import time

import server_config as config
from ac3server import notifications

from ac3server.gamesession.state import (GAME_SESSIONS, GROUP_LEADER_CHANGED_MINOR, GROUP_MEMBER_LEFT_MINOR,
    GROUP_NOTIFICATION_MAJOR, GROUP_SESSION_TYPE_ID, PARTICIPANT_INFO, describe_attrs, read_key)
from ac3server.players.identity import CONNECTED_PIDS, addr_of_pid
from ac3server.protocol.datatypes import read_string
from ac3server.protocol.rmc import build_rmc_response


# After the leader's "return to the lobby" broadcast (type 2, QUIT_WITH_GROUP) the members leave the group session
# on purpose. Told of it, the leader's game sees fewer than two members and dissolves the group at once. So for
# this long after a type-2 broadcast, member leaves are logged, not pushed.
GROUP_MESSAGE_RETURN_TO_LOBBY = 2
RETURN_TO_LOBBY_GRACE = 30.0


def notify_group_members(session_id, session, subject_pid, minor, what,
                          exclude=(), param1=None):
    """Push a major-7 group event about `subject_pid` to the other members.

    Sent to every current participant except the subject itself and
    anything in `exclude`. Nothing is sent to the subject: for a join, the
    joiner's own task already built its member list, and addMember with
    the receiver's own pid raises a game event (tag 0x14) whose effect on
    a client that already knows it joined has not been observed.
    `param1` is the subject's pid unless given (minor 1 carries the
    session id there: `notify_group_leader`).
    """
    targets = []
    for pid in session.get("participants", []):
        if pid == subject_pid or pid in exclude:
            continue
        info = PARTICIPANT_INFO.get(pid)
        if info and info.get("addr"):
            targets.append((pid, info["addr"]))
    ntype = GROUP_NOTIFICATION_MAJOR * 1000 + minor
    if not targets:
        print(f"      (group {session_id:#x}: nobody else to tell that "
              f"{subject_pid} {what})")
        return 0
    since = time.time() - session.get("returning_to_lobby", 0.0)
    if minor == GROUP_MEMBER_LEFT_MINOR and since < RETURN_TO_LOBBY_GRACE:
        print(f"      (group {session_id:#x} is returning to the lobby, "
              f"{since:.1f}s in: {subject_pid} leaving is expected, not telling "
              f"{len(targets)} member(s) - the leader would dissolve the group)")
        return 0
    if not config.NOTIFY_GROUP_EVENTS:
        print(f"      (notify_group_events is off - would have told "
              f"{len(targets)} member(s) of group {session_id:#x} that "
              f"{subject_pid} {what}, type {ntype})")
        return 0
    sent = 0
    for pid, target_addr in targets:
        if notifications.send_notification(
                target_addr, pid_source=subject_pid, ntype=ntype,
                param1=subject_pid if param1 is None else param1, param2=session_id,
                param3=GROUP_SESSION_TYPE_ID,
                label=f"Group event ({what}, type {ntype}) to {pid}"):
            sent += 1
    return sent


def group_member_left(session_id, session, pid, new_host_pid=None):
    """After `pid` has been removed from a group: tell the others, and if
    the leadership moved, tell everyone but the new leader who it is.

    `pid` may be None when nobody was removed on this path (an abandon
    that follows a leave, where the leaver is already gone) but the host
    still changed - then only the leader-changed event goes out.
    """
    if session.get("type_id") != GROUP_SESSION_TYPE_ID:
        return
    if pid is not None:
        notify_group_members(session_id, session, pid,
                              GROUP_MEMBER_LEFT_MINOR, "left")
    if new_host_pid is not None:
        notify_group_leader(session_id, session, new_host_pid,
                            exclude=() if pid is None else (pid,))


def notify_group_leader(session_id, session, new_host_pid, exclude=()):
    """Minor 1, "the group's host migrated": pidSource = the new host, param1 = the group's session id.

    The consumer (FUN_00DD8EDB, minor 1) passes the event to FUN_00DBC807 -> GroupService's own migration handler
    FUN_0116B5D0, which stores param1 as the group SESSION ID (GroupService+0x168) and marks pidSource as its host
    (setHost FUN_0116DF00: +0x2A4 and each member's host flag); in a match it also moves the P2P session's master
    there (P2PSession::MigrateHost 0x01152050). It is the members' half of the new host's TaskRDV2_MigrateHost
    (GameSession.MigrateSession), so it goes to everyone but the new host - whose consumer ignores its own pid anyway.
    param1 must be the session id, not the new leader's pid: a member takes it as the group's session id."""
    return notify_group_members(session_id, session, new_host_pid, GROUP_LEADER_CHANGED_MINOR,
                                "is now the leader", exclude=exclude, param1=session_id)


def _decode_group_message(message):
    """A readable form of a group broadcast, for the log only.

    The message is hex TEXT: decoded, it is [u32 messageType] and a payload. Type 3 carries the leader's session
    attribute block, 4 a nested big-endian parameter blob, 6 three u32s.
    """
    try:
        digits = "".join(c for c in message if c in "0123456789abcdefABCDEF")
        raw = bytes.fromhex(digits[:len(digits) // 2 * 2])
        if len(raw) < 8:
            return None
        msg_type = struct.unpack_from('<I', raw, 0)[0]
        if msg_type != 3:
            return f"type {msg_type}, {len(raw)} bytes"
        count = struct.unpack_from('<I', raw, 4)[0]
        pos = 8
        attrs = {}
        for _ in range(count):
            if pos + 9 > len(raw):
                break
            attr_id = struct.unpack_from('<I', raw, pos)[0]
            value = struct.unpack_from('<I', raw, pos + 5)[0]
            attrs[attr_id] = value
            pos += 9
        return (f"type 3, the leader's match settings: "
                f"[{describe_attrs(attrs)}]")
    except Exception:
        return None


# The relay: major 7 minor 12 reaches the group consumer's text branch (FUN_00DD8EDB), which requires param3 == 2
# and hex-decodes strParam using the string's own length - so it must go out verbatim.
GROUP_MESSAGE_NOTIFICATION_TYPE = 7012


def _relay_group_message(key, sender_pid, recipient_pid, message):
    target = addr_of_pid(recipient_pid)
    if target is None:
        return False
    return notifications.send_notification(
        target, pid_source=sender_pid or 0,
        ntype=GROUP_MESSAGE_NOTIFICATION_TYPE,
        param1=0, param2=0, str_param=message, param3=key[0],
        label=f"group message to {recipient_pid}")


def handle_gamesession_send_text_message(call_id, params, addr):  # m20 SendTextMessage
    try:
        key, pos = read_key(params, 0)
        message, pos = read_string(params, pos)
        session = GAME_SESSIONS.get(key[1])
        sender = CONNECTED_PIDS.get(addr)
        # EVERY participant, the sender included. The consumer acts on each message type by the receiver's role,
        # and type 2 ("return to the lobby") has an owner branch too (createLobbySession and the leave flow,
        # FUN_00dd2dea): without the echo the leader never goes back to a lobby. For every other type the echo is
        # inert.
        others = list((session or {}).get("participants", []))
        decoded = _decode_group_message(message)
        print(f"[{addr}]   GameSession.SendTextMessage to group {key[1]:#x}: "
              f"{decoded or repr(message[:60])}")
        # type 2, "return to the lobby": the members' leaves that follow are not reported (RETURN_TO_LOBBY_GRACE)
        try:
            mtype = struct.unpack_from('<I', bytes.fromhex(message.strip("\0")), 0)[0]
        except Exception:
            mtype = None
        if session is not None and mtype == GROUP_MESSAGE_RETURN_TO_LOBBY:
            session["returning_to_lobby"] = time.time()
            print(f"      group {key[1]:#x} is returning to the lobby - member "
                  f"leaves are expected and will not be reported to the leader")
        delivered = 0
        for pid in others:
            if _relay_group_message(key, sender, pid, message):
                delivered += 1
        if others:
            names = [PARTICIPANT_INFO.get(p, {}).get("name", p) for p in others]
            print(f"      relayed to {delivered} of {len(others)} member(s) "
                  f"{names} (sender included - its consumer needs its own type 2)")
    except Exception as e:
        print(f"[{addr}]   GameSession.SendTextMessage: could not parse request ({e})")
    return "GameSession.SendTextMessage", build_rmc_response(0x2A, call_id, 20, b"")
