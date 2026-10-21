"""Invitations: how a private match is joined. Entirely GameSession (protocol 0x2A) - no Uplay service is involved.

    m12 SendInvitation          [key][list<pid> recipients][string message]   -> nothing
    m14 GetInvitationsReceived  [u32 typeId][range]   -> [list] { [key][u32 senderPid][string][u64 datetime] }
    m17 AcceptInvitation        [key][u32 senderPid][string][u64 datetime]    -> nothing
    m18 DeclineInvitation       (the same)                                    -> nothing

The game never asks for its invitations (it does not call m14), so each one is PUSHED to its
recipient as notification 7005 - major 7 minor 5, the client's OnlineEventDataInvitation, with the fields acb-rdv
uses: source = sender, param2 = session id, param3 = key type, string = message. Accepting is then the ordinary join
path: GetSession, JoinSession. Both of the game's join tasks - invited in game or through Uplay - call GetSession on
us; Uplay's part is only a command-line hand-off on the player's own machine. Layouts are the client's own
serializers (FUN_01167810, FUN_01167650).

Invitations live in memory only, as a handshake between two connected games.
"""

import struct

from ac3server import notifications

from ac3server.gamesession.state import read_key
from ac3server.players.identity import CONNECTED_PIDS, addr_of_pid
from ac3server.protocol.datatypes import q_datetime_now, read_string, read_u32, write_string
from ac3server.protocol.rmc import build_rmc_response


INVITATIONS = {}  # recipient pid -> list of {"key", "sender_pid", "message", "when"}
# The newest kept per recipient: an invitation nobody answers would otherwise stay for the life of the process.
INVITATIONS_KEPT = 20


INVITE_NOTIFICATION_TYPE = 7005


def notify_invitation(recipient_pid, sender_pid, key, message):
    target = addr_of_pid(recipient_pid)
    if target is None:
        print(f"      {recipient_pid} is not connected - invitation stored only")
        return False
    return notifications.send_notification(
        target, pid_source=sender_pid, ntype=INVITE_NOTIFICATION_TYPE,
        param1=0, param2=key[1], param3=key[0], str_param=message,
        label=f"GameSession invitation from {sender_pid}")


def invitations_for(pid):
    return INVITATIONS.setdefault(pid, [])


def store_invitation(recipient_pid, key, sender_pid, message, when):
    invites = invitations_for(recipient_pid)
    invites.append({"key": key, "sender_pid": sender_pid, "message": message, "when": when})
    del invites[:-INVITATIONS_KEPT]


def handle_gamesession_send_invitation(call_id, params, addr):    # m12
    try:
        key, pos = read_key(params, 0)
        count, pos = read_u32(params, pos)
        recipients = []
        for _ in range(count):
            pid, pos = read_u32(params, pos)
            recipients.append(pid)
        message, pos = read_string(params, pos)
        sender = CONNECTED_PIDS.get(addr, 0)
        when = q_datetime_now()
        for pid in recipients:
            store_invitation(pid, key, sender, message, when)
        print(f"[{addr}]   GameSession.SendInvitation to session {key[1]:#x}: "
              f"{len(recipients)} recipient(s) {recipients}, message {message!r}")
        # pushed: the recipient's game never asks
        for pid in recipients:
            notify_invitation(pid, sender, key, message)
    except Exception as e:
        print(f"[{addr}]   GameSession.SendInvitation: could not parse request ({e})")
    return "GameSession.SendInvitation", build_rmc_response(0x2A, call_id, 12, b"")


def handle_gamesession_get_invitations_received(call_id, params, addr):  # m14
    """The caller's invitations, as the DDL's GameSessionInvitationReceived records."""
    pid = CONNECTED_PIDS.get(addr)
    invites = list(invitations_for(pid)) if pid is not None else []
    resp = bytearray(struct.pack('<I', len(invites)))
    for invite in invites:
        resp += struct.pack('<II', invite["key"][0], invite["key"][1])
        resp += struct.pack('<I', invite["sender_pid"])
        write_string(resp, invite["message"])
        resp += struct.pack('<Q', invite["when"])
    print(f"[{addr}]   GameSession.GetInvitationsReceived -> {len(invites)} invitation(s)")
    return "GameSession.GetInvitationsReceived", build_rmc_response(
        0x2A, call_id, 14, bytes(resp))


def _consume_invitation(addr, params, what):
    pid = CONNECTED_PIDS.get(addr)
    try:
        key, pos = read_key(params, 0)
    except Exception as e:
        print(f"[{addr}]   GameSession.{what}: could not parse request ({e})")
        return
    invites = invitations_for(pid) if pid is not None else []
    remaining = [i for i in invites if i["key"][1] != key[1]]
    dropped = len(invites) - len(remaining)
    if pid is not None:
        INVITATIONS[pid] = remaining
    print(f"[{addr}]   GameSession.{what} for session {key[1]:#x}: "
          f"{dropped} matching invitation(s) cleared")


def handle_gamesession_accept_invitation(call_id, params, addr):  # m17
    _consume_invitation(addr, params, "AcceptInvitation")
    # The client follows this with GetSession (m6) and then JoinSession
    # (m22), so nothing else has to happen here.
    return "GameSession.AcceptInvitation", build_rmc_response(0x2A, call_id, 17, b"")


def handle_gamesession_decline_invitation(call_id, params, addr):  # m18
    _consume_invitation(addr, params, "DeclineInvitation")
    return "GameSession.DeclineInvitation", build_rmc_response(0x2A, call_id, 18, b"")
