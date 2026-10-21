"""SecureConnection (protocol 0x0B), on the secure server (21007): the first call of the game's second connection.

    m1 Register     the station URLs the client can be reached at; answered with a connection id and its address
    m4 RegisterEx   the same, with the login custom data again (only logged, redacted)

The reply carries two things that matter for play across the internet: a per-connection id (the RVCID every station URL
of that client carries afterwards), and the client's own address as this server sees it, which is its public NAT mapping,
since the client's PRUDP connection comes from the same UDP port the game listens on. The client publishes that URL and
peers connect to it.
"""

import struct

from ac3server.players.identity import CONNECTED_PIDS
from ac3server.protocol.datatypes import read_string_list, write_string
from ac3server.protocol.rmc import build_rmc_response
from ac3server.redaction import login_secrets, read_login_custom_data
from ac3server.services.presence import presence_reset


def parse_register_request(params: bytes):
    urls, _pos = read_string_list(params, 0)
    return {"stationUrls": urls}


def handle_register(call_id, params, addr):
    try:
        fields = parse_register_request(params)
        print(f"[{addr}]   Register fields: {fields}")
    except Exception as e:
        print(f"[{addr}]   Register parse failed: {e}")
    resp = bytearray()
    resp += struct.pack('<I', 0x10001)
    # Same connection id as RegisterEx hands out for this client: a client
    # carries one RVCID across everything it does. The URL keeps its own
    # shape - `Register` (m1) is the auth-port registration and, unlike
    # RegisterEx, no reply from Ubisoft's server is known to match against.
    resp += struct.pack('<I', connection_id_for(addr))
    client_url = f"prudps:/address={addr[0]};port={addr[1]};sid=14;type=3"
    write_string(resp, client_url)
    return "Register", build_rmc_response(0x0B, call_id, 1, bytes(resp))


def registerex_credentials(params: bytes):
    """RegisterEx's (username, onlineKey, password), raw. Never log these."""
    _urls, pos = read_string_list(params, 0)
    return read_login_custom_data(params, pos)[1:]


def parse_registerex_request(params: bytes):
    urls, pos = read_string_list(params, 0)
    class_name, username, online_key, password = read_login_custom_data(params, pos)
    return {"stationUrls": urls, "className": class_name, "username": username,
            **login_secrets(username, online_key, password)}


# --- connection ids, and telling a client its own public address ----------
# `RegisterEx`'s reply is `[u32 0x00010001][u32 connectionId][string url]`,
# as in this reply from Ubisoft's server:
#
#   01 00 01 00  2d 07 00 00  33 00 "prudp:/address=192.0.2.124;port=3074;sid=15;type=3"
#
# - **connectionId is the RVCID.** 0x72d is 1837, and every station URL that
#   client goes on to register carries `RVCID=1837`. It is per-connection:
#   two players sharing one id cannot be told apart as peers.
# - **The scheme is `prudp:`**, not `prudps:`. The client echoes this URL
#   back as its own public station URL and peers then connect to it, so the
#   scheme is not cosmetic.
#
# The address is simply what our socket sees. That is the whole trick for
# internet play: the client's PRUDP connection reaches us **from the same
# UDP port the game listens on** (3074), so the endpoint we observe already
# is its public mapping - 3074 where the NAT preserved the port, something
# else where it did not.
_NEXT_CONNECTION_ID = [1750]
CONNECTION_IDS = {}          # addr -> the RVCID we handed that connection


def connection_id_for(addr):
    """A stable per-connection id, in the range Ubisoft's server used."""
    if addr not in CONNECTION_IDS:
        CONNECTION_IDS[addr] = _NEXT_CONNECTION_ID[0]
        _NEXT_CONNECTION_ID[0] += 1
    return CONNECTION_IDS[addr]


def public_station_url(addr, pid=None, rvcid=None, probe=False):
    """The station URL for an endpoint as this server observes it.

    Two shapes, both as Ubisoft's server sent them:

      RegisterEx reply   prudp:/address=A;port=P;sid=15;type=3
      InitiateProbe      prudp:/address=A;port=P;RVCID=R;sid=15
    """
    if rvcid is None:
        rvcid = connection_id_for(addr)
    if probe:
        return f"prudp:/address={addr[0]};port={addr[1]};RVCID={rvcid};sid=15"
    url = f"prudp:/address={addr[0]};port={addr[1]};sid=15;type=3"
    if pid is not None:
        url += f";PID={pid};RVCID={rvcid}"
    return url


def handle_registerex(call_id, params, addr):
    try:
        fields = parse_registerex_request(params)
        print(f"[{addr}]   RegisterEx fields: {fields}")
    except Exception as e:
        print(f"[{addr}]   RegisterEx parse failed: {e}")
    presence_reset(CONNECTED_PIDS.get(addr))    # no presence carried over from the previous connection
    resp = bytearray()
    resp += struct.pack('<I', 0x10001)
    resp += struct.pack('<I', connection_id_for(addr))
    client_url = public_station_url(addr)
    write_string(resp, client_url)
    print(f"[{addr}]   RegisterEx -> connection id {connection_id_for(addr)}, "
          f"its own address as we see it: {client_url}")
    return "RegisterEx", build_rmc_response(0x0B, call_id, 4, bytes(resp))
