"""Notifications: the requests the SERVER sends - protocol 0x0E, method 1, ProcessNotificationEvent.

The payload is a NotificationEvent (embedded DDL; Ubisoft's notifications parse to it exactly):

    [u32 pidSource][u32 type][u32 param1][u32 param2][string strParam][u32 param3]

The client splits `type` into major = type / 1000 and minor = type % 1000 (FUN_0117cd30) and routes on the major:

    1     friendship requested / answered           players/friendships.py
    3     participation in a session                below; off by default
    5     a friend's status (online / offline)       players/friendships.py
    7     minor 5: an invitation                     gamesession/invitations.py
          minors 1-3: group membership               gamesession/groups.py
          minor 12: a group's broadcast message      gamesession/groups.py

The client only ACKs a notification; it never answers one. Our call ids for them count from 0xC000 per client, as
Ubisoft's server did, apart from the client's own small call ids.
"""

import struct

from ac3server.protocol.datatypes import write_string
from ac3server.protocol.rmc import build_rmc_request


# The secure server (set by app.main): the only connection a notification is ever pushed down.
SECURE_SERVER = [None]

NOTIFICATION_PROTO = 0x0E
NOTIFICATION_CALL_ID_BASE = 0xC000
_NOTIFY_CALL_IDS = {}  # addr -> next call id to use


def next_notification_call_id(addr):
    call_id = _NOTIFY_CALL_IDS.get(addr, NOTIFICATION_CALL_ID_BASE)
    _NOTIFY_CALL_IDS[addr] = call_id + 1
    return call_id


def build_notification_event(pid_source, ntype, param1=0, param2=0,
                             str_param="", param3=0) -> bytes:
    body = bytearray()
    body += struct.pack('<IIII', pid_source & 0xFFFFFFFF, ntype & 0xFFFFFFFF,
                        param1 & 0xFFFFFFFF, param2 & 0xFFFFFFFF)
    write_string(body, str_param)
    body += struct.pack('<I', param3 & 0xFFFFFFFF)
    return bytes(body)


def send_notification(addr, pid_source, ntype, param1=0, param2=0,
                      str_param="", param3=0, label="Notification"):
    """Push one notification to the client at `addr`. -> True if it had a connection to go down."""
    server = SECURE_SERVER[0]
    if server is None:
        print(f"[{addr}]   cannot send {label}: the secure server is not running")
        return False
    call_id = next_notification_call_id(addr)
    payload = build_rmc_request(
        NOTIFICATION_PROTO, call_id, 1,
        build_notification_event(pid_source, ntype, param1, param2,
                                 str_param, param3))
    print(f"[{addr}]   -> {label}: type {ntype}, source {pid_source}, "
          f"params {param1}/{param2}/{param3}, call id {call_id:#x}")
    return server.send_rmc_request(addr, payload, label)


# Major 3, "somebody's participation in a session changed". Case 3 of the client's switch: minor 1 builds an event
# with tag 0x14, minors 2 and 8 one with tag 0x15, and minor 3 only logs and builds nothing. Which tag means joined
# and which left is not recovered (that needs the consumers of tags 0x14/0x15), so these pushes stay off unless
# `notify_participants` is set; with it off the join and leave paths log what they would have sent.
PARTICIPATION_NOTIFICATION_MAJOR = 3
PARTICIPATION_JOINED_MINOR = 1
PARTICIPATION_LEFT_MINOR = 2
