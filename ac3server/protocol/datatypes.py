"""Quazal's wire data types, the few every service needs: strings, string lists, u32s and the packed DateTime.

Everything on the wire is little-endian. A string is [u16 length including the NUL][bytes][NUL]; a list is
[u32 count][items]. Service-specific structures are read and written in their own service modules, beside the
evidence for their layout.
"""

import datetime
import struct


def read_string(data: bytes, pos: int):
    length = struct.unpack('<H', data[pos:pos + 2])[0]
    pos += 2
    s = data[pos:pos + length - 1].decode('ascii', 'replace')
    pos += length  # includes the null terminator byte
    return s, pos


def write_string(buf: bytearray, s: str):
    encoded = s.encode('ascii') + b'\x00'
    buf += struct.pack('<H', len(encoded))
    buf += encoded


def read_string_list(data: bytes, pos: int):
    count = struct.unpack('<I', data[pos:pos + 4])[0]
    pos += 4
    items = []
    for _ in range(count):
        s, pos = read_string(data, pos)
        items.append(s)
    return items, pos


def q_datetime_now() -> int:
    """Quazal packed DateTime (acb-rdv's QDateTime.ToU64), the layout Ubisoft's own server time replies use."""
    n = datetime.datetime.now()
    return ((n.year << 26) | (n.month << 22) | (n.day << 17)
            | (n.hour << 12) | (n.minute << 6) | n.second)


def read_u32(params, pos):
    return struct.unpack_from('<I', params, pos)[0], pos + 4
