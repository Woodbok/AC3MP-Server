"""RMC (remote method call) envelopes: what one complete PRUDP message carries.

    request    [u32 size][u8 proto | 0x80][u32 callId][u32 methodId][params]
    response   [u32 size][u8 proto][u8 1][u32 callId][u32 methodId | 0x8000][params]
    error      [u32 size][u8 proto][u8 0][u32 Quazal error code][u32 callId]

A protocol id above 0x7F is written as the escape byte 0x7F followed by a u16 id (AC3's Ubisoft-specific services:
ClientLogin 0x1BBC, Shop 0x1B5B, Wallet 0x1A0A, FriendsLeague 0x1C20, the Overlord protocols 0x138x). The server
also sends requests of its own - notifications (ac3server/notifications.py) - which the client only ACKs.
"""

import struct


# Protocol-id escape: this value in the protocol byte means "the real id is
# the u16 that follows". Same constant acb-rdv's RMCP uses.
EXTENDED_PROTO_ESCAPE = 0x7F


def parse_rmc_request(data: bytes):
    """Parse an RMC envelope, including the 16-bit "extended protocol" form.

    A protocol byte of 0x7F (0xFF with the request bit) is an escape: the real
    protocol id is the u16 that follows, as in acb-rdv's RMCP constructor.
    AC3 uses it for the
    `aconlineservices` protocols - ClientLogin 0x1BBC, Shop 0x1B5B, Wallet
    0x1A0A, FriendsLeague 0x1C20 - which the 8-bit form cannot express.
    """
    size = struct.unpack('<I', data[0:4])[0]
    proto_byte = data[4]
    is_request = bool(proto_byte & 0x80)
    proto = proto_byte & 0x7F
    pos = 5
    if proto == EXTENDED_PROTO_ESCAPE:
        proto = struct.unpack('<H', data[pos:pos + 2])[0]
        pos += 2
    call_id = struct.unpack('<I', data[pos:pos + 4])[0]
    method_id = struct.unpack('<I', data[pos + 4:pos + 8])[0]
    params = data[pos + 8:]
    return size, is_request, proto, call_id, method_id, params


def parse_rmc_response(data: bytes):
    """Parse an RMC *response* envelope.

    Not the same shape as a request, and the difference is easy to miss: a
    response carries a **success byte** between the protocol id and the call
    id, and its method id has 0x8000 set. Running `parse_rmc_request` over a
    response therefore reads the call id one byte early and returns garbage.

    Returns (success, error_code, proto, call_id, method_id, params). On a
    failure response there is no method id or params - error_code holds the
    Quazal error and both are None.
    """
    proto_byte = data[4]
    proto = proto_byte & 0x7F
    pos = 5
    if proto == EXTENDED_PROTO_ESCAPE:
        proto = struct.unpack('<H', data[pos:pos + 2])[0]
        pos += 2
    success = bool(data[pos])
    pos += 1
    if not success:
        error_code = struct.unpack('<I', data[pos:pos + 4])[0]
        call_id = struct.unpack('<I', data[pos + 4:pos + 8])[0]
        return False, error_code, proto, call_id, None, b""
    call_id = struct.unpack('<I', data[pos:pos + 4])[0]
    method_id = struct.unpack('<I', data[pos + 4:pos + 8])[0] & ~0x8000
    return True, 0, proto, call_id, method_id, data[pos + 8:]


def build_rmc_error_response(proto: int, call_id: int, error_code: int) -> bytes:
    """RMC *error* envelope, exactly as acb-rdv's RMC.SendResponsePacket writes
    it when error != 0: [size][proto][0x00][error:u32][call_id:u32]. This is
    the protocol's own failure signalling, not a guessed payload layout."""
    envelope = bytearray()
    if proto > EXTENDED_PROTO_ESCAPE:
        # 16-bit protocol ids need the same escape here as in the success
        # path, otherwise `proto & 0x7F` silently truncates (0x138B -> 0x0B)
        # and the client is handed an error for a protocol it never called.
        envelope.append(EXTENDED_PROTO_ESCAPE)
        envelope += struct.pack('<H', proto)
    else:
        envelope.append(proto & 0x7F)
    envelope.append(0)  # success = false
    envelope += struct.pack('<I', error_code)
    envelope += struct.pack('<I', call_id)
    return struct.pack('<I', len(envelope)) + bytes(envelope)


def build_rmc_response(proto: int, call_id: int, method_id: int, resp_params: bytes) -> bytes:
    envelope = bytearray()
    if proto > EXTENDED_PROTO_ESCAPE:
        # 16-bit protocol id: escape byte then the id, mirroring the request
        # form (the size counts the escape byte and the u16).
        envelope.append(EXTENDED_PROTO_ESCAPE)
        envelope += struct.pack('<H', proto)
    else:
        envelope.append(proto & 0x7F)  # no high bit on a response
    envelope.append(1)  # success
    envelope += struct.pack('<I', call_id)
    envelope += struct.pack('<I', method_id | 0x8000)
    envelope += resp_params
    return struct.pack('<I', len(envelope)) + bytes(envelope)


def build_rmc_request(proto: int, call_id: int, method_id: int, params: bytes) -> bytes:
    """RMC *request* envelope, for calls the **server** initiates.

    The exact inverse of `parse_rmc_request` above: the request bit is the
    high bit of the protocol byte, the extended form escapes with 0x7F (so
    0xFF once the request bit is set), and the method id carries no 0x8000
    response flag.

    Server-initiated requests are how Quazal delivers notifications
    (`ProcessNotificationEvent`). The client does **not** answer them: it
    only PRUDP-ACKs the packet and never sends an RMC response, so nothing
    should wait on a reply. One is logged if it ever arrives.
    """
    envelope = bytearray()
    if proto > EXTENDED_PROTO_ESCAPE:
        envelope.append(EXTENDED_PROTO_ESCAPE | 0x80)
        envelope += struct.pack('<H', proto)
    else:
        envelope.append((proto & 0x7F) | 0x80)
    envelope += struct.pack('<I', call_id)
    envelope += struct.pack('<I', method_id)
    envelope += params
    return struct.pack('<I', len(envelope)) + bytes(envelope)
