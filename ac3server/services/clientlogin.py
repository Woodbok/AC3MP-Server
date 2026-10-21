"""ClientLogin (protocol 0x1BBC): the login of Ubisoft's "aconlineservices" layer, a step of the game's online start-up.

    m1 Authenticate   [u32][string name][u32 8][u8 0]  -> [bool 1][u32 0][u32 0][u32 0][string ""][string ""]
    m3                [u32][string name][u32 0][u8 0]  -> [bool 1][u32 0]
    m4 GetServerTime  nothing                          -> the current Quazal DateTime
    m5                nothing                          -> [u32 0]
    m7                nothing                          -> five u32s: 500000, 500000, 0, 0, 418149

Every reply is the one Ubisoft's server sent, except the server time, which is now. If Authenticate goes unanswered
or fails, the game shows its "you are not running the most recent version" popup (error code 6, ERROR_VERSION): the
popup reports this step failing, not an old game.
"""

import struct

from ac3server.protocol.datatypes import q_datetime_now, read_string, write_string
from ac3server.protocol.rmc import build_rmc_response


CLIENTLOGIN_PROTO = 0x1BBC


def handle_clientlogin_method7(call_id, params, addr):
    if params:
        print(f"[{addr}]   ClientLogin.7: {len(params)} unexpected param byte(s) - "
              f"sending the fixed reply anyway")
    return "ClientLogin.7", build_rmc_response(
        0x1BBC, call_id, 7, bytes.fromhex("20a1070020a10700000000000000000065610600"))


def handle_clientlogin_authenticate(call_id, params, addr):
    pos = 0
    leading = struct.unpack_from('<I', params, 0)[0] if len(params) >= 4 else None
    name = None
    try:
        name, pos = read_string(params, 4)
    except Exception:
        pass
    print(f"[{addr}]   ClientLogin.Authenticate: leading={leading} name={name!r} "
          f"({len(params)} bytes) -> sending Ubisoft's reply")
    resp = bytearray()
    resp.append(1)                      # bool: authenticated
    resp += struct.pack('<III', 0, 0, 0)
    write_string(resp, "")              # both strings empty, as Ubisoft's server sent
    write_string(resp, "")
    return "ClientLogin.Authenticate", build_rmc_response(CLIENTLOGIN_PROTO, call_id, 1, bytes(resp))


def handle_clientlogin_method3(call_id, params, addr):
    return "ClientLogin.3", build_rmc_response(
        CLIENTLOGIN_PROTO, call_id, 3, b"\x01" + struct.pack('<I', 0))


def handle_clientlogin_servertime(call_id, params, addr):
    return "ClientLogin.GetServerTime", build_rmc_response(
        CLIENTLOGIN_PROTO, call_id, 4, struct.pack('<Q', q_datetime_now()))


def handle_clientlogin_method5(call_id, params, addr):
    return "ClientLogin.5", build_rmc_response(
        CLIENTLOGIN_PROTO, call_id, 5, struct.pack('<I', 0))
