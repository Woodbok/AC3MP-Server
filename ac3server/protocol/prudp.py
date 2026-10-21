"""PRUDP packets: Quazal's reliable-UDP format, version 0 as AC3 speaks it.

A packet is [src vport][dst vport][type | flags << 3][session id][u32 signature][u16 seq id], then a u32 connection
signature (SYN and CONNECT only), a part number (DATA only), an optional u16 size, the payload and a one-byte
checksum. Types: SYN, CONNECT, DATA, DISCONNECT, PING. Flags: ACK, RELIABLE, NEED_ACK, HAS_SIZE.

A payload on stream type 3 is RC4'd with the constant key and carries a leading byte that says whether the rest is
zlib-compressed; we always send uncompressed. Larger RMC messages are split into parts of MAX_RMC_PAYLOAD_SIZE.
What the server does with packets - handshakes, ACKs, retransmits, ordering - is ac3server/transport.py.
"""

import struct
import zlib

from ac3server.protocol.crypto import RC4_KEY_RDV, rc4
from ac3server.redaction import loggable_payload


# The checksum seed per stream type: 0xC4 is AC3's for stream type 3, the only one it uses. Any other stream type
# falls back to acb-rdv's generic default (0x00).
CHECKSUM_SETTINGS = {3: 0xC4}


def parse_connect_auth_payload(payload: bytes, session_key: bytes):
    """CONNECT-with-payload carries [ticket_len][ticket_bytes][auth_len]
    [encrypted_authenticator]. The authenticator is separately RC4-encrypted
    with the session key from the KerberosTicket we handed out during
    LoginEx - the client is just using the key we gave it. Decrypts to
    {pid, cid, challenge}, per acb-rdv's MakeConnectPayload."""
    pos = 0
    ticket_len = struct.unpack('<I', payload[pos:pos + 4])[0]
    pos += 4 + ticket_len  # skip the ticket bytes themselves, unused here
    auth_len = struct.unpack('<I', payload[pos:pos + 4])[0]
    pos += 4
    auth_len -= 16  # trailing 16 bytes are an HMAC tag, not part of the encrypted region
    encrypted = payload[pos:pos + auth_len]
    decrypted = rc4(session_key, encrypted)
    pid, cid, challenge = struct.unpack('<III', decrypted[0:12])
    return pid, cid, challenge


def build_connect_reply_payload(challenge: int) -> bytes:
    return struct.pack('<II', 4, (challenge + 1) & 0xFFFFFFFF)


class VPort:
    def __init__(self, byte_val=None, stream_type=0, port=0):
        if byte_val is not None:
            self.type = byte_val >> 4
            self.port = byte_val & 0xF
        else:
            self.type = stream_type
            self.port = port

    def to_byte(self):
        return (self.port & 0xF) | ((self.type & 0xF) << 4)

    def __repr__(self):
        return f"VPort(type={self.type}, port={self.port})"


PACKET_TYPES = {0: "SYN", 1: "CONNECT", 2: "DATA", 3: "DISCONNECT", 4: "PING", 5: "NATPING"}
FLAG_ACK, FLAG_RELIABLE, FLAG_NEED_ACK, FLAG_HAS_SIZE = 1, 2, 4, 8
MAX_RMC_PAYLOAD_SIZE = 963  # per acb-rdv's RMC.MaxRmcPayloadSize; larger responses are fragmented


def make_checksum(data: bytes) -> int:
    stream_type = data[0] >> 4
    setting = CHECKSUM_SETTINGS.get(stream_type, 0x00)
    n_words = len(data) // 4
    tmp = sum(struct.unpack_from(f'<{n_words}I', data)) & 0xFFFFFFFF      # the 32-bit sum of the whole words
    leftover = len(data) & 3
    pos = len(data) - leftover
    tmp2 = tmp3 = tmp4 = 0
    processed = 0
    if leftover >= 2:
        processed = 2
        tmp2, tmp3 = data[pos], data[pos + 1]
        pos += 2
    tmp4 = setting if processed >= leftover else (setting + data[pos]) & 0xFF
    result = ((tmp >> 24) & 0xFF) + ((tmp >> 16) & 0xFF) + ((tmp >> 8) & 0xFF) + (tmp & 0xFF) + tmp2 + tmp3 + tmp4
    return result & 0xFF


class PrudpPacket:
    def __init__(self):
        self.src_vport = VPort(stream_type=0, port=0)
        self.dst_vport = VPort(stream_type=0, port=0)
        self.ptype = 0
        self.flags = 0
        self.session_id = 0
        self.signature = 0
        self.seq_id = 0
        self.connection_signature = 0
        self.part_number = 0
        self.payload = b""

    @classmethod
    def parse(cls, data: bytes):
        p = cls()
        pos = 0
        p.src_vport = VPort(data[pos]); pos += 1
        p.dst_vport = VPort(data[pos]); pos += 1
        type_flags = data[pos]; pos += 1
        p.ptype = type_flags & 0x7
        p.flags = type_flags >> 3
        p.session_id = data[pos]; pos += 1
        p.signature = struct.unpack('<I', data[pos:pos + 4])[0]; pos += 4
        p.seq_id = struct.unpack('<H', data[pos:pos + 2])[0]; pos += 2
        if p.ptype in (0, 1):  # SYN, CONNECT
            p.connection_signature = struct.unpack('<I', data[pos:pos + 4])[0]; pos += 4
        if p.ptype == 2:  # DATA
            p.part_number = data[pos]; pos += 1
        if p.flags & FLAG_HAS_SIZE:
            size = struct.unpack('<H', data[pos:pos + 2])[0]; pos += 2
        else:
            size = len(data) - pos - 1  # minus checksum byte
        raw_payload = data[pos:pos + size]
        pos += size
        p.checksum = data[pos] if pos < len(data) else None

        p.payload = raw_payload
        # RC4-decrypt + decompress, matching the "OldRVSec" (stream type 3) rule.
        if raw_payload and p.ptype != 0 and p.src_vport.type == 3:
            decrypted = rc4(RC4_KEY_RDV, raw_payload)
            try:
                if decrypted[0] != 0:
                    p.payload = zlib.decompress(decrypted[1:])
                else:
                    p.payload = decrypted[1:]
            except Exception:
                p.payload = decrypted  # keep raw if decompression fails, for debugging
        return p

    def build(self) -> bytes:
        buf = bytearray()
        buf.append(self.src_vport.to_byte())
        buf.append(self.dst_vport.to_byte())
        buf.append((self.ptype & 0x7) | (self.flags << 3))
        buf.append(self.session_id)
        buf += struct.pack('<I', self.signature)
        buf += struct.pack('<H', self.seq_id)
        if self.ptype in (0, 1):
            buf += struct.pack('<I', self.connection_signature)
        if self.ptype == 2:
            buf.append(self.part_number)

        payload = self.payload or b""
        if payload and self.ptype != 0 and self.src_vport.type == 3:
            # mirror parse()'s symmetric compress+encrypt for outgoing payloads
            marker_and_body = b"\x00" + payload  # 0 = uncompressed, simplest correct case
            payload = rc4(RC4_KEY_RDV, marker_and_body)

        if self.flags & FLAG_HAS_SIZE:
            buf += struct.pack('<H', len(payload))
        buf += payload
        buf.append(make_checksum(bytes(buf)))
        return bytes(buf)

    def describe(self) -> str:
        return (f"{PACKET_TYPES.get(self.ptype, self.ptype)} "
                f"src={self.src_vport} dst={self.dst_vport} flags={self.flags:#04b} "
                f"session={self.session_id:#04x} sig={self.signature:#010x} "
                f"seq={self.seq_id:#06x} connsig={self.connection_signature:#010x} "
                f"payload={loggable_payload(self.payload)}")
