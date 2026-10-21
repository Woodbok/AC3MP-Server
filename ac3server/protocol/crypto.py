"""The cryptography of a Quazal login: RC4, the iterated-MD5 key derivation and the Kerberos-style ticket.

None of it protects anything. Every PRUDP payload is RC4'd with a public constant key (RC4_KEY_RDV), and the ticket
LoginEx hands out is sealed with a key derived from the Quazal-level password, which in AC3 is always the constant
"UbiDummyPwd" (the game logs into Rendez-Vous as DummyUbiUser@quazal.com). The server produces these bytes because the client will not continue without well-formed
ones. Who a player is, and whether they may log in, is decided by the account check in players/identity.

All of it is ported from acb-rdv (https://github.com/michal-kapala/acb-rdv), the open-source Assassin's Creed
Brotherhood server, whose layouts AC3's traffic matches byte for byte.
"""

import functools
import hashlib
import hmac as hmac_lib
import struct


RC4_KEY_RDV = b"CD&ML"  # Quazal's documented default key, per acb-rdv.


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + key[i % len(key)] + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(len(data))
    i = j = 0
    for n, b in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[n] = b ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


# RC4 starts afresh for every packet, so with a constant key every packet is XORed with the same keystream: worked out
# once per constant key and reused, it is ~100x cheaper than running RC4 per packet (tests/ac3_transport_selftest.py
# checks the two agree). Other keys - a player's ticket key - go through _rc4 itself.
_KEYSTREAMS = {}             # constant key -> its keystream, grown as longer data comes along


def rc4(key: bytes, data: bytes) -> bytes:
    if key not in _CONSTANT_KEYS:
        return _rc4(key, data)
    n = len(data)
    stream = _KEYSTREAMS.get(key)
    if stream is None or len(stream) < n:
        stream = _KEYSTREAMS[key] = _rc4(key, bytes(max(n, 4096)))
    return (int.from_bytes(data, "little") ^ int.from_bytes(stream[:n], "little")).to_bytes(n, "little")


@functools.lru_cache(maxsize=4096)
def derive_key(pid: int, input_bytes: bytes = b"UbiDummyPwd") -> bytes:
    """Iterated MD5 key derivation, per acb-rdv's Helper.DeriveKey. The input is the Quazal-level password, which
    in AC3 is always "UbiDummyPwd", never the player's own. 65000 MD5 rounds (~25 ms) for a value that never changes
    for a pid, and a login asks twice (LoginEx, RequestTicket): cached."""
    count = 65000 + (pid % 1024)
    buf = input_bytes
    for _ in range(count):
        buf = hashlib.md5(buf).digest()
    return buf


def make_hmac(key: bytes, data: bytes) -> bytes:
    return hmac_lib.new(key, data, hashlib.md5).digest()


def build_kerberos_ticket(user_pid: int, server_pid: int, session_key: bytes, ticket_blob: bytes) -> bytes:
    m = bytearray()
    m += session_key  # 16 bytes
    m += struct.pack('<I', server_pid)
    m += struct.pack('<I', len(ticket_blob))
    m += ticket_blob
    key = derive_key(user_pid)
    encrypted = rc4(key, bytes(m))
    tag = make_hmac(key, encrypted)
    return encrypted + tag


# Placeholder session key / inner ticket blob (values from acb-rdv's own
# example) - arbitrary: the client only needs them well-formed.
PLACEHOLDER_SESSION_KEY = bytes([0x9C, 0xB0, 0x1D, 0x7A, 0x2C, 0x5A, 0x6C, 0x5B,
                                  0xED, 0x12, 0x68, 0x45, 0x69, 0xAE, 0x09, 0x0D])
PLACEHOLDER_TICKET_BLOB = bytes([0x76, 0x21, 0x4B, 0xA6, 0x21, 0x96, 0xD3, 0xF3,
                                  0x9A, 0x8C, 0x7A, 0x27, 0x0D, 0xD9, 0xB3, 0xFA,
                                  0x21, 0x0E, 0xED, 0xAF, 0x42, 0x63, 0x92, 0x95,
                                  0xC1, 0x16, 0x54, 0x08, 0xEE, 0x6E, 0x69, 0x17,
                                  0x35, 0x78, 0x2E, 0x6E])

_CONSTANT_KEYS = frozenset((RC4_KEY_RDV, PLACEHOLDER_SESSION_KEY))
