"""Delivering the game settings, and the content calls around it.

    GameInfo 0x71 m7         GetFileList  [u32 min][u32 max][string name]   -> [list] { [string name][u32 size] }
    PersistentStore 0x18 m4  GetItem      [u32 group][string name]         -> [u32 size][the container][bool 1]
    GameInfo 0x71 m6         [u32 hash of the settings just downloaded]    -> Ubisoft's own reply
    News 0x1F m2             GetChannelsByTypes                            -> empty list, as Ubisoft's servers sent

Whatever name the game asks for, the answer is our one container (gamesettings.GAMESETTINGS_CXB). The size
GetFileList announces must equal what GetItem then delivers: the game sizes its receive buffer from it, and a
mismatch leaves the download step waiting until its 20 s timeout.
"""

import struct

from ac3server.gamesettings import GAMESETTINGS_CXB
from ac3server.protocol.datatypes import read_string, write_string
from ac3server.protocol.rmc import build_rmc_response


def parse_gameinfo_getfilelist_request(params: bytes):
    min_n = struct.unpack('<I', params[0:4])[0]
    max_n = struct.unpack('<I', params[4:8])[0]
    filename, _pos = read_string(params, 8)
    return {"minElements": min_n, "maxElements": max_n, "fileName": filename}


def handle_gameinfo_getfilelist(call_id, params, addr):
    filename = ""
    try:
        fields = parse_gameinfo_getfilelist_request(params)
        print(f"[{addr}]   GameInfo.GetFileList fields: {fields}")
        filename = fields["fileName"]
    except Exception as e:
        print(f"[{addr}]   GameInfo.GetFileList parse failed: {e}")

    resp = bytearray()
    resp += struct.pack('<I', 1)  # file count
    write_string(resp, filename)
    resp += struct.pack('<I', len(GAMESETTINGS_CXB))
    return "GameInfo.GetFileList", build_rmc_response(0x71, call_id, 7, bytes(resp))


def parse_persistentstore_getitem_request(params: bytes):
    group = struct.unpack('<I', params[0:4])[0]
    tag, _pos = read_string(params, 4)
    return {"group": group, "tag": tag}


def handle_persistentstore_getitem(call_id, params, addr):
    try:
        fields = parse_persistentstore_getitem_request(params)
        print(f"[{addr}]   PersistentStore.GetItem fields: {fields}")
    except Exception as e:
        print(f"[{addr}]   PersistentStore.GetItem parse failed: {e}")
    content = GAMESETTINGS_CXB
    resp = bytearray()
    resp += struct.pack('<I', len(content))
    resp += content
    resp += struct.pack('<B', 1)  # Result = True
    return "PersistentStore.GetItem", build_rmc_response(0x18, call_id, 4, bytes(resp))


# u32 count = 6, then six {u32, u32} pairs: Ubisoft's reply, byte for byte (ACR's server answers the same shape)
GAMEINFO_METHOD6_REAL = bytes.fromhex(
    "06000000010000000a000000020000000600000006000000010000000f000000"
    "0100000008000000000000000b00000000000000")


def handle_gameinfo_method6(call_id, params, addr):
    return "GameInfo.6", build_rmc_response(0x71, call_id, 6, GAMEINFO_METHOD6_REAL)


def handle_news_get_channels_by_types(call_id, params, addr):
    return "News.GetChannelsByTypes", build_rmc_response(0x1F, call_id, 2, b'\x00\x00\x00\x00')
