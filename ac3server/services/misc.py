"""The small services: calls of the game's online start-up and menus that need only a fixed or empty answer.

    TrackingExt 0x6F        m1  telemetry ping                -> [u8 1]           (byte-exact from ACR, same family)
    Eligibility 0x23        m1  the error-code table           -> a static table   (byte-exact from ACR)
    (self profile) 0x1D     m3  the caller's own profile       -> pid and name; no email or country of anybody's
    (locale) 0x27           m2  [string locale]                -> nothing          (Ubisoft's AC3 and ACR servers agree)
    Messaging 0x17          m4  GetMessagesHeaders             -> empty list       (Ubisoft's reply)
    LiveEvents 0x78         m1                                 -> [u32 0][f32 60.0], Ubisoft's reply

Without a Messaging answer the start-up waits on "Updating Animus parameters" for ever.
"""

import struct

from ac3server.players.identity import CONNECTED_PIDS, CONNECTED_USERS, USER_PID_MIN
from ac3server.protocol.datatypes import write_string
from ac3server.protocol.rmc import build_rmc_response


def handle_trackingext_ping(call_id, params, addr):
    return "TrackingExt.Ping", build_rmc_response(0x6F, call_id, 1, b'\x01')


# generic message texts by code ("Allow to play online", "unknown", "DLC_1"...), nothing about a player
_PROTO_0X23_TABLE_PARAMS = bytes.fromhex(
    "180000000100000001000000150041"
    "6c6c6f7720746f20706c6179206f6e"
    "6c696e6500e8030000e80300000800"
    "756e6b6e6f776e00e9030000e90300"
    "000800756e6b6e6f776e00ea030000"
    "ea0300000800756e6b6e6f776e00eb"
    "030000eb0300000800756e6b6e6f77"
    "6e00ec030000ec0300000800756e6b"
    "6e6f776e00ed030000ed0300000800"
    "756e6b6e6f776e00ee030000ee0300"
    "000800756e6b6e6f776e00ef030000"
    "ef0300000800756e6b6e6f776e00f0"
    "030000f00300000800756e6b6e6f77"
    "6e00f1030000f10300000800756e6b"
    "6e6f776e00f2030000f20300000800"
    "756e6b6e6f776e00f3030000f30300"
    "000800756e6b6e6f776e00f4030000"
    "f40300000800756e6b6e6f776e00f5"
    "030000f50300000800756e6b6e6f77"
    "6e00f6030000f60300000800756e6b"
    "6e6f776e00f7030000f70300000800"
    "756e6b6e6f776e00f8030000f80300"
    "000800756e6b6e6f776e00f9030000"
    "f90300000800756e6b6e6f776e00fa"
    "030000fa0300000800756e6b6e6f77"
    "6e00fb030000fb0300000600444c43"
    "5f3100fc030000fc0300000600444c"
    "435f3200fd030000fd030000060044"
    "4c435f33004c0400004c0400000800"
    "756e6b6e6f776e00"
)


def handle_eligibility_check(call_id, params, addr):
    return "EligibilityCheck", build_rmc_response(0x23, call_id, 1, _PROTO_0X23_TABLE_PARAMS)


def handle_self_profile_lookup(call_id, params, addr):
    """Ubisoft's reply shape - [pid string][name][string][6 bytes][email][14 bytes][country][flags] - filled with
    what we know; email and country stay blank."""
    username = CONNECTED_USERS.get(addr, "player")
    resp = bytearray()
    write_string(resp, str(CONNECTED_PIDS.get(addr, USER_PID_MIN)))  # pid, as handed out in LoginEx
    write_string(resp, username)
    write_string(resp, "")
    resp += b'\x00' * 6
    write_string(resp, "")              # email, left blank
    resp += b'\x00' * 14
    write_string(resp, "")              # country code, left blank
    resp += bytes.fromhex("000001000001000001000000000001")  # trailing flags, as Ubisoft sent them
    return "SelfProfileLookup", build_rmc_response(0x1D, call_id, 3, bytes(resp))


def handle_locale_announce(call_id, params, addr):
    return "LocaleAnnounce", build_rmc_response(0x27, call_id, 2, b'')


MESSAGING_PROTO = 0x17


def handle_messaging_get_headers(call_id, params, addr):
    return "Messaging.GetMessagesHeaders", build_rmc_response(
        MESSAGING_PROTO, call_id, 4, struct.pack('<I', 0))


# [u32 count][u32 value] (dispatcher 0x01175220); the value is the float 60.0, plausibly a refresh interval
LIVEEVENTS_REFRESH_REPLY = bytes.fromhex("0000000000007042")


def handle_liveevents_method1(call_id, params, addr):
    return "LiveEvents.1", build_rmc_response(
        0x78, call_id, 1, LIVEEVENTS_REFRESH_REPLY)
