"""The NAT detection service (ac3server/natdetect.py) against a model of the game's own client. Offline, a few seconds.

    python tests/natdetect_selftest.py

1. The wire: a request over real loopback sockets is answered in Ubisoft's 19-byte form (checked against a reply from
   Ubisoft's live detect server), command 1 from the socket asked, 2 from the port + 1 socket, 3 from
   server 2.
2. The verdicts: a model of DetectNatTypeStateMachine (AC3MP.exe 0x11E49A0 / 0x11E1FD0 / 0x11E4120, the game's
   conversion 0x402000) talks to the detector through a model NAT - none, full cone, port-restricted cone, symmetric -
   and the game's NATType it reaches must equal the detector's own verdict for that address. Then two players behind
   one router at once.
"""
import os
import socket
import struct
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ac3server import natdetect as nd  # noqa: E402

FAILED = []
UBISOFT_REPLY = bytes.fromhex("00130001070200009000000000cb00712ff815")   # cmd 1, seq 7 -> 203.0.113.47:63509 (Ubisoft's reply, the address replaced)


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def request(cmd, seq):
    return struct.pack(">HHBB", 13, cmd, seq, 0) + os.urandom(7)       # the client leaves 7 bytes unwritten


def free_base():
    for base in range(40100, 60000, 7):
        socks = []
        try:
            for p in range(base, base + 3):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                socks.append(s)
                s.bind(("127.0.0.1", p))
            return base
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
    raise SystemExit("no free ports")


def wire():
    print("\nthe wire, over loopback")
    check("the reply rebuilds Ubisoft's bytes",
          nd.build_reply(struct.pack(">HHBB", 13, 1, 7, 0) + b"\0" * 7, ("203.0.113.47", 63509)) == UBISOFT_REPLY)
    check("a short or unknown request is ignored",
          nd.build_reply(b"\x00\x0d", ("1.2.3.4", 1)) is None and nd.build_reply(request(9, 1), ("1.2.3.4", 1)) is None)
    base = free_base()
    det = nd.Detector("127.0.0.1", base, log=lambda *_: None).start()
    try:
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.bind(("127.0.0.1", 0))
        c.settimeout(2)
        me = c.getsockname()
        for cmd, expect_from in ((1, base), (2, base + 1), (3, base + 2)):
            c.sendto(request(cmd, cmd), ("127.0.0.1", base))
            data, src = c.recvfrom(64)
            length, rcmd, seq, flag = struct.unpack(">HHBB", data[:6])
            addr = (socket.inet_ntoa(data[13:17]), struct.unpack(">H", data[17:19])[0])
            check(f"command {cmd}: 19 bytes from port {expect_from - base:+d}, echoed, the sender's address",
                  len(data) == 19 and length == 19 and rcmd == cmd and seq == cmd and flag == 2
                  and src[1] == expect_from and addr == me, f"{data.hex()} from {src}")
        c.sendto(request(1, 4), ("127.0.0.1", base + 1))
        data, src = c.recvfrom(64)
        check("command 1 on the port + 1 socket is answered from there", src[1] == base + 1)
        c.close()
    finally:
        det.close()


class Wire:
    """The detector's sockets replaced by recorders; the model NAT delivers what each sends."""
    def __init__(self, name, host, port):
        self.name, self.addr, self.sent = name, (host, port), []

    def sendto(self, data, dest):
        self.sent.append((data, dest))

    def getsockname(self):
        return self.addr


class Nat:
    """kind: none | full | restricted (port-restricted cone) | symmetric."""
    def __init__(self, kind, public_ip, local):
        self.kind, self.ip, self.local = kind, public_ip, local
        self.maps, self.sent_to, self.next_port = {}, set(), 50000 + (hash(local) % 1000)

    def outbound(self, dest):
        if self.kind == "none":
            return self.local
        key = dest if self.kind == "symmetric" else "any"
        if key not in self.maps:
            self.next_port += 1
            self.maps[key] = self.next_port
        self.sent_to.add(dest)
        return (self.ip, self.maps[key])

    def admits(self, src):
        return self.kind in ("none", "full") or src in self.sent_to


class Client:
    """DetectNatTypeStateMachine, as read from the exe (states 0x11E49A0; parser 0x11E1FD0; verdict 0x11E4120)."""
    def __init__(self, det, nat, server1, server2):
        self.det, self.nat, self.s1, self.s2 = det, nat, server1, server2
        self.seq = 0

    def ask(self, cmd, dest):
        """Send a command, return (reply bytes, source) the NAT lets in, or None (a packet timeout)."""
        self.seq = (self.seq + 1) & 0xFF
        sock = {self.det.main.addr: self.det.main, self.det.alt.addr: self.det.alt,
                self.det.other.addr: self.det.other}[dest]
        for s in (self.det.main, self.det.alt, self.det.other):
            s.sent.clear()
        self.det.handle(sock, request(cmd, self.seq), self.nat.outbound(dest))
        for s in (self.det.main, self.det.alt, self.det.other):
            for data, _ in s.sent:
                if self.nat.admits(s.addr):
                    return data, s.addr
        return None

    @staticmethod
    def mapped(data):
        return socket.inet_ntoa(data[13:17]), struct.unpack(">H", data[17:19])[0]

    def run(self):
        """-> the game's NATType (0x402000 applied to Storm's verdict), driving its own steps."""
        steps = self.steps()
        reply = None
        try:
            while True:
                cmd, dest = steps.send(reply)
                reply = self.ask(cmd, dest)
        except StopIteration as done:
            return done.value

    def steps(self):
        """A generator: yields (command, destination), is sent the reply or None, returns the game's NATType."""
        r = yield 1, self.s1                                         # start: command 1 to server 1
        if r is None:
            return nd.GAME_UNKNOWN
        first = self.mapped(r[0])
        if first == self.nat.local:                                  # its own address: no NAT
            return nd.GAME_OPEN
        next_server = False
        for _try in range(3):                                        # command 3, three tries
            r = yield 3, self.s1
            if r is not None and struct.unpack(">H", r[0][2:4])[0] == 3:
                next_server = True
                break
        if next_server:
            if (yield 2, self.s1) is None:                           # command 2: the next port
                return nd.GAME_UNKNOWN
        elif (yield 1, self.s2) is None:                             # command 1 to server 2
            return nd.GAME_UNKNOWN
        r = yield 1, (self.s1[0], self.s1[1] + 1)                    # command 1 to server 1's port + 1
        if r is not None and self.mapped(r[0])[1] == first[1]:
            return nd.GAME_OPEN if next_server else nd.GAME_MODERATE  # cone
        return nd.GAME_STRICT                                        # another port per destination: symmetric


def verdicts():
    print("\nthe verdicts: the game's own steps through a model NAT")
    base = free_base()
    det = nd.Detector("127.0.0.1", base, log=lambda *_: None)
    det.close()
    det.main, det.alt, det.other = Wire("main", "203.0.113.5", 11000), Wire("alt", "203.0.113.5", 11001), \
        Wire("other", "203.0.113.5", 11002)
    expect = {"none": nd.GAME_OPEN, "full": nd.GAME_OPEN, "restricted": nd.GAME_MODERATE, "symmetric": nd.GAME_STRICT}
    for i, (kind, game) in enumerate(expect.items()):
        public = f"198.51.100.{10 + i}"
        local = (public, 3074) if kind == "none" else (f"192.168.1.{20 + i}", 3074)
        nat = Nat(kind, public, local)
        got = Client(det, nat, det.main.addr, det.other.addr).run()
        nd.NO_NAT_QUIET, saved = 0.0, nd.NO_NAT_QUIET
        time.sleep(0.01)
        seen = det.verdict(public)
        nd.NO_NAT_QUIET = saved
        check(f"{kind:10s}: the game decides {nd.GAME_NAMES[got]}, and so does the server",
              got == game and seen == game, f"game {got}, server {seen}, expected {game}")

    print("\ntwo players behind one router at once")
    nat = Nat("restricted", "198.51.100.77", ("192.168.1.2", 3074))
    a = Client(det, nat, det.main.addr, det.other.addr)
    nat_b = Nat("restricted", "198.51.100.77", ("192.168.1.3", 3074))
    nat_b.next_port = nat.next_port + 500
    b = Client(det, nat_b, det.main.addr, det.other.addr)
    ga, gb, results, replies = a.steps(), b.steps(), {}, {"a": None, "b": None}
    live = {"a": (a, ga), "b": (b, gb)}
    while live:                                                      # one step each, in turn
        for name in list(live):
            client, gen = live[name]
            try:
                cmd, dest = gen.send(replies[name])
                replies[name] = client.ask(cmd, dest)
            except StopIteration as done:
                results[name] = done.value
                live.pop(name)
    ra, rb = results["a"], results["b"]
    check("interleaved step by step, both reach moderate and the server agrees", ra == rb == nd.GAME_MODERATE
          and det.verdict("198.51.100.77") == nd.GAME_MODERATE, f"{ra} {rb} {det.verdict('198.51.100.77')}")
    check("an address never seen is unknown", det.verdict("192.0.2.200") == nd.GAME_UNKNOWN)

    print("\nflooding")
    flood = Nat("full", "198.51.100.99", ("192.168.9.9", 3074))
    answered = 0
    for _ in range(100):
        det.main.sent.clear()
        det.handle(det.main, request(1, 1), flood.outbound(det.main.addr))
        answered += len(det.main.sent)
    check(f"one address gets at most {nd.RATE_MAX} answers per {nd.RATE_WINDOW:g} s", answered == nd.RATE_MAX,
          str(answered))


def main():
    wire()
    verdicts()
    print("\nall checks passed" if not FAILED else f"\n{len(FAILED)} check(s) failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
