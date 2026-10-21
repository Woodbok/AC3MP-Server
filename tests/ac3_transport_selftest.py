"""Self-test for the PRUDP send path, with no game and no network.

Replies and notifications share `_send_rmc_payload`; a notification has no
incoming request to reply to, so `send_rmc_request` builds its context from the
stored connection. This checks that responses come out correctly, that an
unprompted notification goes out, and covers reconnects, resends, the packet
checksum and the RC4 keystream.

The socket is replaced with a recorder, so nothing is sent anywhere.

    python tests/ac3_transport_selftest.py
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import server_modules  # noqa: E402

ps = server_modules.load(quiet=False)

ADDR = ("192.168.0.50", 4242)
CLIENT_VPORT = 15


class RecordingSocket:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def bind(self, *_):
        pass

    def settimeout(self, *_):
        pass


def make_packet(ptype, payload=b"", seq=1, flags=0, part=0):
    pkt = ps.PrudpPacket()
    pkt.src_vport = ps.VPort(stream_type=3, port=CLIENT_VPORT)
    pkt.dst_vport = ps.VPort(stream_type=3, port=1)
    pkt.ptype = ptype
    pkt.flags = flags
    pkt.session_id = 0x5A
    pkt.signature = 0
    pkt.seq_id = seq
    pkt.part_number = part
    pkt.payload = payload
    pkt.connection_signature = 0xABCD
    return pkt


def run():
    failures = []

    def check(name, condition, detail=""):
        print(f"  [{'ok  ' if condition else 'FAIL'}] {name}"
              + (f" - {detail}" if detail else ""))
        if not condition:
            failures.append(name)

    server = ps.PortServer(0, ps.SECURE_DISPATCH, "selftest")
    server.sock = RecordingSocket()

    print("\nCONNECT records a reusable connection context")
    server.handle_packet(make_packet(1), ADDR)
    key = (ADDR, CLIENT_VPORT)
    ctx = server.conn_ctx.get(key)
    check("context stored for the connection", ctx is not None)
    if ctx:
        check("session id taken from the client's packet", ctx["session_id"] == 0x5A,
              f"got {ctx['session_id']:#x}")
        check("our source vport is the client's destination",
              ctx["src_vport"].port == 1)
        check("our destination vport is the client's source",
              ctx["dst_vport"].port == CLIENT_VPORT)

    print("\nA normal request still gets a normal response")
    server.sock.sent.clear()
    # GameSession.SearchSessions: no sessions exist, so the reply is the
    # empty list, four zero bytes.
    request = ps.build_rmc_request(0x2A, 7, 7, b"\x00" * 44)
    server.handle_packet(make_packet(2, request, seq=2,
                                     flags=ps.FLAG_RELIABLE | ps.FLAG_NEED_ACK), ADDR)
    data_out = [d for d, _ in server.sock.sent if ps.PrudpPacket.parse(d).payload]
    check("a response was sent", len(data_out) == 1, f"{len(data_out)} data packet(s)")
    if data_out:
        parsed = ps.PrudpPacket.parse(data_out[0])
        ok, err, proto, call_id, method_id, params = \
            ps.parse_rmc_response(parsed.payload)
        check("response reports success", ok is True, f"error {err:#010x}")
        check("response is for GameSession", proto == 0x2A, hex(proto))
        check("response echoes the call id", call_id == 7, str(call_id))
        check("response is for the method that was called", method_id == 7,
              str(method_id))
        check("empty session list", params == b"\x00\x00\x00\x00", params.hex())
        check("addressed back to the client's vport",
              parsed.dst_vport.port == CLIENT_VPORT)
        check("session id preserved", parsed.session_id == 0x5A)

    print("\nAn unprompted notification can be pushed with no request to reply to")
    server.sock.sent.clear()
    notification = ps.build_rmc_request(0x0E, 0xC001, 1, bytes.fromhex("01020304"))
    pushed = server.send_rmc_request(ADDR, notification, "Notification")
    check("push reported success", pushed is True)
    check("exactly one packet went out", len(server.sock.sent) == 1,
          f"{len(server.sock.sent)} packet(s)")
    if server.sock.sent:
        parsed = ps.PrudpPacket.parse(server.sock.sent[0][0])
        size, is_request, proto, call_id, method_id, params = \
            ps.parse_rmc_request(parsed.payload)
        check("notification is flagged as a request", is_request is True)
        check("notification protocol survives", proto == 0x0E, hex(proto))
        check("notification call id survives", call_id == 0xC001, hex(call_id))
        check("notification method id has no response bit", method_id == 1,
              hex(method_id))
        check("notification params survive", params.hex() == "01020304", params.hex())
        check("notification reached the right vport",
              parsed.dst_vport.port == CLIENT_VPORT)
        check("notification uses a fresh sequence id", parsed.seq_id > 1,
              str(parsed.seq_id))

    print("\nA disconnect forgets the connection")
    server.handle_packet(make_packet(3, seq=9), ADDR)
    check("context cleared", not any(k[0] == ADDR for k in server.conn_ctx))
    check("a later push reports failure",
          server.send_rmc_request(ADDR, notification, "Notification") is False)

    print("\nA resent CONNECT keeps the connection; a new one resets it")
    srv3 = ps.PortServer(0, ps.SECURE_DISPATCH, "reconnect")
    srv3.sock = RecordingSocket()
    a3 = ("192.0.2.10", 3074)
    k3 = (a3, CLIENT_VPORT)
    srv3.handle_packet(make_packet(1), a3)
    srv3._send_rmc_payload(k3, srv3.conn_ctx[k3], b"\x00" * 40, "queued reply")
    check("a reply is waiting for its ACK", len(srv3.pending.get(k3, {})) == 1)
    srv3.sock.sent.clear()
    srv3.handle_packet(make_packet(1), a3)                 # the very same CONNECT again
    check("the same CONNECT again is ACKed", any(ps.PrudpPacket.parse(d).ptype == 1 for d, _ in srv3.sock.sent))
    check("and the waiting reply survives it", len(srv3.pending.get(k3, {})) == 1)
    fresh = make_packet(1)
    fresh.connection_signature = 0x1234                    # a relaunched game: a new connection signature
    srv3.handle_packet(fresh, a3)
    check("a different CONNECT starts a new connection", not srv3.pending.get(k3))

    print("\nThe packet checksum, summed in one call, matches a word-by-word reference")

    def checksum_reference(data):
        setting = ps.CHECKSUM_SETTINGS.get(data[0] >> 4, 0x00)
        tmp = 0
        for i in range(len(data) // 4):
            tmp = (tmp + int.from_bytes(data[i * 4:i * 4 + 4], "little")) & 0xFFFFFFFF
        leftover = len(data) & 3
        pos = len(data) - leftover
        tmp2 = tmp3 = 0
        processed = 0
        if leftover >= 2:
            processed = 2
            tmp2, tmp3 = data[pos], data[pos + 1]
            pos += 2
        tmp4 = setting if processed >= leftover else (setting + data[pos]) & 0xFF
        return (((tmp >> 24) & 0xFF) + ((tmp >> 16) & 0xFF) + ((tmp >> 8) & 0xFF) + (tmp & 0xFF)
                + tmp2 + tmp3 + tmp4) & 0xFF
    import random as _random
    crng = _random.Random(11)
    agree = all(ps.make_checksum(d) == checksum_reference(d)
                for n in list(range(1, 80)) + [979, 980, 981, 982, 983]
                for d in [bytes([0x31]) + bytes(crng.randrange(256) for _ in range(n - 1)) for _ in range(5)])
    check("every length 1-79 and around a full packet, five random packets each", agree)

    print("\nThe precomputed keystream is the same RC4")
    import random
    rng = random.Random(7)
    same = True
    for key in (ps.RC4_KEY_RDV, ps.PLACEHOLDER_SESSION_KEY, b"another key"):
        for n in list(range(0, 70)) + [962, 963, 964, 4095, 4096, 4097, 9000]:
            data = bytes(rng.randrange(256) for _ in range(n))
            if ps.rc4(key, data) != ps._rc4(key, data):
                same = False
                check(f"rc4 key {key[:4]!r}... length {n}", False)
    check("every length 0-69, around a full packet and past the precomputed stream, three keys", same)
    data = bytes(rng.randrange(256) for _ in range(500))
    check("and it still decrypts what it encrypts", ps.rc4(ps.RC4_KEY_RDV, ps.rc4(ps.RC4_KEY_RDV, data)) == data)
    check("the ticket key is cached per pid, not recomputed",
          ps.derive_key(1234567) is ps.derive_key(1234567) and ps.derive_key(1234567) != ps.derive_key(1001))

    print("\nResends back off, and give up on time, not on a count")
    srv2 = ps.PortServer(0, ps.SECURE_DISPATCH, "resend")
    srv2.sock = RecordingSocket()
    key2 = (("192.0.2.9", 3074), 15)
    srv2.handle_packet(make_packet(1), key2[0])
    srv2.sock.sent.clear()
    srv2._send_rmc_payload(key2, srv2.conn_ctx[key2], b"\x00" * 40, "probe")
    entry = next(iter(srv2.pending[key2].values()))
    sent0 = len(srv2.sock.sent)
    real_time = ps.time.time
    try:
        t = [entry["first"]]
        ps.time.time = lambda: t[0]
        for step in range(60):                  # 15 s in 0.25 s steps, the check run every step
            t[0] += 0.25
            srv2.check_retransmits()
        resends = len(srv2.sock.sent) - sent0
        check("15 s unanswered: resent with growing gaps (0.25, 0.5, 1, 2, 2 ... s), not every pass",
              6 <= resends <= 10, str(resends))
        check("and still waiting for its ACK at 15 s", key2 in srv2.pending and len(srv2.pending[key2]) == 1)
        t[0] = entry["first"] + ps.GIVE_UP_AFTER + 0.5
        entry["sent"] = 0
        srv2.check_retransmits()
        check("given up after GIVE_UP_AFTER seconds", not srv2.pending.get(key2))
    finally:
        ps.time.time = real_time

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
