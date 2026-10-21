"""Authentication (protocol 0x0A), on the authentication server (21006): the game's login.

    m1 Login           only the "Tracking" telemetry pseudo-account uses it
    m2 LoginEx         the player's login: a principal id (pid), a ticket, and the address of the secure server to
                       go on to
    m3 RequestTicket   a ticket for the secure server, sealed with a key derived from the pid

LoginEx carries UbiAuthenticationLoginCustomData: the Ubisoft name, an online key and a password slot. A stock client
puts the player's real Ubisoft password there; the server never reads it, ac3server/redaction.py keeps it out of
every log, and patcher/ac3mp_patch.py stops the client sending it at all. The player is who the name says: there are
no accounts on this server (players/identity.py).
"""

import struct

from ac3server.gamesession.sessions import forget_stale_sessions
from ac3server.players.friendships import announce_friend_status
from ac3server.players.identity import CONNECTED_PIDS, CONNECTED_USERS, TRACKING_USER_PID, resolve_player_identity
from ac3server.protocol.crypto import PLACEHOLDER_SESSION_KEY, PLACEHOLDER_TICKET_BLOB, build_kerberos_ticket
from ac3server.protocol.datatypes import read_string, write_string
from ac3server.protocol.rmc import build_rmc_response
from ac3server.redaction import login_secrets, read_login_custom_data
from ac3server.runtime import LOCAL_IP, SECURE_CONNECTION_PORT


def secure_server_url(server_pid):
    """Where the client goes next: the secure server, in the parameter set AC3's own code builds."""
    return (f"prudps:/address={LOCAL_IP};port={SECURE_CONNECTION_PORT};"
            f"CID=1;PID={server_pid};sid=1;stream=3;type=2")


def loginex_credentials(params: bytes):
    """LoginEx's (username, onlineKey, password), raw. Never log these."""
    _user, pos = read_string(params, 0)
    return read_login_custom_data(params, pos)[1:]


def parse_loginex_request(params: bytes):
    user, pos = read_string(params, 0)
    class_name, username, online_key, password = read_login_custom_data(params, pos)
    return {"user": user, "className": class_name, "username": username,
            **login_secrets(username, online_key, password)}


def handle_login(call_id, params, addr):
    """Plain Login(username), used only by the 'Tracking' telemetry
    pseudo-account. The ticket buffer is a placeholder, as in acb-rdv: the
    client never decrypts or checks it."""
    resp = bytearray()
    resp += struct.pack('<I', 0x10001)
    resp += struct.pack('<I', TRACKING_USER_PID)
    resp += struct.pack('<I', len(PLACEHOLDER_TICKET_BLOB))
    resp += PLACEHOLDER_TICKET_BLOB
    write_string(resp, secure_server_url(1))
    resp += struct.pack('<I', 0)
    write_string(resp, "")
    write_string(resp, "")
    return "Login", build_rmc_response(0x0A, call_id, 1, bytes(resp))


def handle_loginex(call_id, params, addr):
    username = None
    try:
        fields = parse_loginex_request(params)
        print(f"[{addr}]   LoginEx fields: {fields}")
        username, _online_key, _password = loginex_credentials(params)
    except Exception as e:
        print(f"[{addr}]   LoginEx field parse failed: {e}")
    if username:
        CONNECTED_USERS[addr] = username
    identity = resolve_player_identity(addr, CONNECTED_USERS.get(addr))
    CONNECTED_USERS[addr] = identity["name"]
    pid = identity["pid"]
    CONNECTED_PIDS[addr] = pid
    announce_friend_status(pid, True)
    # A client that has just logged in is, by definition, not in any session.
    # Without this, a session it never got to abandon - because the game or
    # the server was restarted under it - stays advertised for ever, and its
    # friends are told it is hosting a game that no longer exists.
    forget_stale_sessions(pid, addr)
    server_pid = 1
    print(f"[{addr}]   LoginEx: principal id {pid} for {identity['name']!r}"
          + (f" (Ubisoft account {identity['account']!r})" if identity["name"] != identity["account"] else ""))
    ticket = build_kerberos_ticket(pid, server_pid, PLACEHOLDER_SESSION_KEY, PLACEHOLDER_TICKET_BLOB)
    resp = bytearray()
    resp += struct.pack('<I', 0x10001)
    resp += struct.pack('<I', pid)
    resp += struct.pack('<I', len(ticket))
    resp += ticket
    write_string(resp, secure_server_url(server_pid))
    resp += struct.pack('<I', 0)
    write_string(resp, "")
    write_string(resp, "")
    return "LoginEx", build_rmc_response(0x0A, call_id, 2, bytes(resp))


def parse_requestticket_request(params: bytes):
    source_pid = struct.unpack('<I', params[0:4])[0]
    target_pid = struct.unpack('<I', params[4:8])[0]
    return {"sourcePID": source_pid, "targetPID": target_pid}


def handle_requestticket(call_id, params, addr):
    # The ticket key is derived from the user's pid, and the client derives
    # its copy from the pid it received in LoginEx, which it echoes back here
    # as sourcePID - so that is the one to use (acb-rdv does the same).
    source_pid = None
    try:
        fields = parse_requestticket_request(params)
        source_pid = fields["sourcePID"]
        print(f"[{addr}]   RequestTicket fields: {fields}")
    except Exception as e:
        print(f"[{addr}]   RequestTicket field parse failed: {e}")
    pid = source_pid or CONNECTED_PIDS.get(addr, TRACKING_USER_PID)
    server_pid = 1
    ticket_buffer = build_kerberos_ticket(pid, server_pid, PLACEHOLDER_SESSION_KEY, PLACEHOLDER_TICKET_BLOB)
    resp = bytearray()
    resp += struct.pack('<I', 0x10001)
    resp += struct.pack('<I', len(ticket_buffer))
    resp += ticket_buffer
    return "RequestTicket", build_rmc_response(0x0A, call_id, 3, bytes(resp))
