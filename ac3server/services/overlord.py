"""Overlord: Ubisoft's online middleware inside the game (StartOverlordRequest; its failure is ERROR_OVERLORD, 4).

    0x138A News m1          -> empty list
    0x138B CoreSettings m1  -> the settings map below - its CONTENT is what lets the game's start-up finish
    0x138D Friends m1       -> nothing (Ubisoft's server replied with no parameters)
    0x1390 Dares m1         -> [DateTime now][empty list]

Each layout is the protocol client's own response reader (slot 22 of each client's vtable) and a real reply on the
wire (Ubisoft's server, or the ACR community server). The settings map's flags tell the game's post-login job to
skip its calls to Ubisoft social services that no longer exist; without them it waits 15 s on the dead Uplay web
login and tears Overlord down.
Nothing here touches the player's own Ubisoft Connect login, which happens at launch.
"""

import struct

from ac3server.protocol.datatypes import q_datetime_now, write_string
from ac3server.protocol.rmc import build_rmc_response


OVERLORD_NEWS_PROTO = 0x138A
OVERLORD_CORE_PROTO = 0x138B
OVERLORD_FRIENDS_PROTO = 0x138D
OVERLORD_DARES_PROTO = 0x1390


# The post-login job (FUN_010F4440) reads each step's flag from this map (FUN_010E9050); the game pre-seeds
# SKIP_AWARDS_SYNC but clears the map before our reply is spliced in, so it only survives if we send it back.
#
# Entry encoding, read off the client's own parser (`FUN_010EBC50`):
#   [u32 count] then per entry [String key][u8 tag][value]
#   String = u16 length including the NUL, then the bytes (write_string)
#   tag 3 = bool, one byte (FUN_0107DB30); tag 4 = String; 1/2/6 = 8 bytes;
#   5 = pair; 0 = nothing.
OVERLORD_TAG_BOOL = 3
OVERLORD_TAG_STRING = 4

OVERLORD_CORE_SETTINGS = [
    ("SKIP_UPLAY_LOGIN", True),         # dead Uplay web services - the blocker
    ("SKIP_FRIENDS_SYNC", True),        # Uplay friends fetch / 0x138D sync
    ("SKIP_LOCAL_CODE_SYNC", True),     # second 0x27/2; also gated by pid<1000
    ("SKIP_AWARDS_SYNC", True),         # mirrors the game's own pre-seed
    ("OL_NOTIFICATION_OFFLINE", False), # no offline-notification fetch
]


def build_overlord_settings(entries) -> bytes:
    """Serialise Overlord core settings the way `FUN_010EBC50` reads them."""
    buf = bytearray(struct.pack('<I', len(entries)))
    for key, value in entries:
        write_string(buf, key)
        if isinstance(value, bool):
            buf.append(OVERLORD_TAG_BOOL)
            buf.append(1 if value else 0)
        elif isinstance(value, str):
            buf.append(OVERLORD_TAG_STRING)
            write_string(buf, value)
        else:
            raise TypeError(f"unsupported Overlord setting type for {key!r}: {type(value).__name__}")
    return bytes(buf)


def handle_overlord_core_settings(call_id, params, addr):
    return "Overlord.CoreSettings.1", build_rmc_response(
        OVERLORD_CORE_PROTO, call_id, 1, build_overlord_settings(OVERLORD_CORE_SETTINGS))


def handle_overlord_news(call_id, params, addr):
    return "Overlord.News.1", build_rmc_response(
        OVERLORD_NEWS_PROTO, call_id, 1, struct.pack('<I', 0))


def handle_overlord_friends(call_id, params, addr):
    return "Overlord.Friends.1", build_rmc_response(
        OVERLORD_FRIENDS_PROTO, call_id, 1, b"")


def handle_overlord_dares(call_id, params, addr):
    resp = struct.pack('<Q', q_datetime_now()) + struct.pack('<I', 0)
    return "Overlord.Dares.1", build_rmc_response(
        OVERLORD_DARES_PROTO, call_id, 1, resp)
