"""Load test: many simulated players against a throwaway server. Not part of run_all - it takes a minute or two.

    python tests/load_test.py [--players 300] [--workers 6] [--ramp 0] [--idle 40] [--friends 5]

Starts both server processes on free ports with a temporary data folder (as smoke_test.py does) and simulates players
that speak real PRUDP, the way the game does after the OnlineConfig request:

  * the authentication connection: SYN, CONNECT, LoginEx; then DISCONNECT
  * the secure connection: SYN, CONNECT, RegisterEx, GetFileList, GetItem (the 76 KB game settings, in ~80 parts,
    every one ACKed), OnlineSave.Get, Friends.GetDetailedList, Shop.GetCurrency
  * then idle: a PING every 20 s, as the game sends, ACKing anything the server pushes

--ramp spreads the logins over that many seconds (0 = all at once: the restart storm). Reported: each phase's server
CPU time (both processes, via psutil), login times, failures, and the server log's warnings.
"""
import argparse
import json
import multiprocessing
import os
import random
import selectors
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

PING_EVERY = 20.0
RETRY_AFTER = 1.0
LOGIN_TIMEOUT = 120.0


def free_port(kind):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- the simulated client ----------------------------------------------------------------------------------------

class Client:
    """One player: two PRUDP connections in turn, then idling. Driven by Worker's select loop."""

    def __init__(self, name, password, auth_addr, secure_addr):
        from ac3server.protocol import datatypes, prudp, rmc
        self.d, self.p, self.r = datatypes, prudp, rmc
        self.name, self.password = name, password
        self.auth_addr, self.secure_addr = auth_addr, secure_addr
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sock.bind(("127.0.0.1", 0))
        self.state = "start"
        self.target = None
        self.session = random.randrange(1, 255)
        self.seq = 1
        self.call = 0
        self.parts = bytearray()
        self.in_next = 1              # the server's next reliable DATA seq id on this connection
        self.in_ahead = {}            # seq -> packet that arrived before its turn
        self.pending = None           # (raw bytes, sent at, kind) - the one outstanding packet we resend
        self.steps = []
        self.started = None
        self.logged_in = None
        self.failed = None
        self.next_ping = 0.0
        self.pings = 0
        self.pushes = 0

    # packets
    def _packet(self, ptype, flags, payload=b"", seq=None, part=0, conn_sig=0):
        pkt = self.p.PrudpPacket()
        pkt.src_vport = self.p.VPort(stream_type=3, port=15)
        pkt.dst_vport = self.p.VPort(stream_type=3, port=1)
        pkt.ptype, pkt.flags, pkt.session_id = ptype, flags, self.session
        pkt.seq_id = self.seq if seq is None else seq
        pkt.part_number, pkt.connection_signature, pkt.payload = part, conn_sig, payload
        return pkt.build()

    def _send(self, raw, kind, reliable=True):
        self.sock.sendto(raw, self.target)
        if reliable:
            self.pending = (raw, time.time(), kind)

    def _rmc(self, proto, method, params):
        self.call += 1
        self.seq += 1
        payload = self.r.build_rmc_request(proto, self.call, method, params)
        flags = self.p.FLAG_RELIABLE | self.p.FLAG_NEED_ACK
        self._send(self._packet(2, flags, payload), (proto, method))

    def _login_data(self):
        out = bytearray()
        self.d.write_string(out, "UbiAuthenticationLoginCustomData")
        out += b"\x00" * 8
        for s in (self.name, self.name, self.password):
            self.d.write_string(out, s)
        return bytes(out)

    # the script
    def begin(self, now):
        self.started = now
        self._connect(self.auth_addr, "auth")

    def _connect(self, addr, which):
        self.target, self.which = addr, which
        self.in_next, self.in_ahead, self.parts = 1, {}, bytearray()
        self.session = random.randrange(1, 255)
        self.seq = 1
        self.state = "syn"
        self._send(self._packet(0, self.p.FLAG_NEED_ACK), "SYN")

    def _next_call(self):
        """The secure connection's start-up calls, in the game's order; then idle."""
        step = len(self.steps)
        calls = [
            (0x0B, 4, lambda: struct.pack("<I", 1) + self._str("prudp:/address=127.0.0.1;port=3074;sid=15")
                + self._login_data()),
            (0x71, 7, lambda: struct.pack("<II", 0, 50) + self._str("gamesettings_c20559.cxb")),
            (0x18, 4, lambda: struct.pack("<I", 0) + self._str("gamesettings_c20559.cxb")),
            (0x77, 2, lambda: b""),
            (0x14, 12, lambda: bytes([1, 0])),
            (0x1B5B, 4, lambda: b""),
        ]
        if step < len(calls):
            proto, method, params = calls[step]
            self._rmc(proto, method, params())
            self.state = "call"
        else:
            self.state = "idle"
            self.logged_in = time.time() - self.started
            self.next_ping = time.time() + random.uniform(0, PING_EVERY)

    def _str(self, s):
        out = bytearray()
        self.d.write_string(out, s)
        return bytes(out)

    def on_packet(self, data, source=None):
        pkt = self.p.PrudpPacket.parse(data)
        if source is not None and source != self.target:
            # a late packet of the connection we have left (the auth server resending its answer): ACK it there
            if pkt.ptype == 2 and not (pkt.flags & self.p.FLAG_ACK):
                ack = self.p.PrudpPacket()
                ack.src_vport, ack.dst_vport = pkt.dst_vport, pkt.src_vport
                ack.ptype, ack.flags, ack.session_id, ack.seq_id = 2, self.p.FLAG_ACK, pkt.session_id, pkt.seq_id
                self.sock.sendto(ack.build(), source)
            return
        ack_flag = pkt.flags & self.p.FLAG_ACK
        if pkt.ptype == 0 and self.state == "syn":
            self.server_sig = pkt.connection_signature
            self.state = "connect"
            self._send(self._packet(1, self.p.FLAG_RELIABLE | self.p.FLAG_NEED_ACK, conn_sig=0xABCD), "CONNECT")
            return
        if pkt.ptype == 1 and self.state == "connect":
            self.pending = None
            if self.which == "auth":
                self.seq += 1
                self.call += 1
                payload = self.r.build_rmc_request(0x0A, self.call, 2, self._str(self.name) + self._login_data())
                self._send(self._packet(2, self.p.FLAG_RELIABLE | self.p.FLAG_NEED_ACK, payload), (0x0A, 2))
                self.state = "call"
            else:
                self._next_call()
            return
        if pkt.ptype == 2 and ack_flag:
            if self.pending and pkt.seq_id == self.seq:
                self.pending = None                  # the server has our request: its answer follows, no resend
            return
        if pkt.ptype == 4:
            return                                   # the server's ACK of our PING
        if pkt.ptype == 2:
            # the server's reliable DATA: ACK it (again, if it is a resend), then take it in sequence order
            ack = self.p.PrudpPacket()
            ack.src_vport, ack.dst_vport = pkt.dst_vport, pkt.src_vport
            ack.ptype, ack.flags, ack.session_id, ack.seq_id = 2, self.p.FLAG_ACK, pkt.session_id, pkt.seq_id
            self.sock.sendto(ack.build(), self.target)
            if ((pkt.seq_id - self.in_next) & 0xFFFF) >= 0x8000:
                return                               # already taken: a resend
            self.in_ahead[pkt.seq_id] = pkt
            while self.in_next in self.in_ahead:
                ready = self.in_ahead.pop(self.in_next)
                self.in_next = (self.in_next + 1) & 0xFFFF       # before: taking it may start the next connection
                self._take(ready)

    def _take(self, pkt):
        """One reliable DATA packet, in order: buffer a part, or handle a complete message."""
        # the last few packets taken, for the failure message (which, sequence, part, size, protocol byte, call id)
        self.seen = (getattr(self, "seen", []) + [(self.which, pkt.seq_id, pkt.part_number, len(pkt.payload),
                                                    pkt.payload[4:5].hex(), pkt.payload[5:9].hex(), self.call)])[-6:]
        self.parts += pkt.payload
        if pkt.part_number != 0:
            return
        full, self.parts = bytes(self.parts), bytearray()
        if len(full) > 4 and full[4] & 0x80:
            self.pushes += 1
            return                               # a request of the server's (a notification): ACKed, no answer
        ok, err, proto, call, method, params = self.r.parse_rmc_response(full)
        if call != self.call:
            return                               # not the answer to our outstanding call
        if self.state != "call":
            return
        self.pending = None
        if not ok:
            self.failed = f"{proto:#x} error {err:#010x}"
            self.state = "done"
            return
        if self.which == "auth":
            self.seq += 1
            self.sock.sendto(self._packet(3, 0), self.target)        # DISCONNECT the auth connection
            self._connect(self.secure_addr, "secure")
            return
        self.steps.append((proto, method, len(full)))
        self._next_call()

    def tick(self, now):
        if self.state in ("done", "start"):
            return
        if self.state != "idle" and now - self.started > LOGIN_TIMEOUT:
            self.failed = (f"login timed out in {self.which} {self.state} after {len(self.steps)} call(s); "
                           f"pending {self.pending is not None}; last taken {getattr(self, 'seen', [])}")
            self.state = "done"
            return
        if self.pending and now - self.pending[1] > RETRY_AFTER:
            raw, _t, kind = self.pending
            self._send(raw, kind)
        if self.state == "idle" and now >= self.next_ping:
            self.seq += 1
            self.sock.sendto(self._packet(4, self.p.FLAG_NEED_ACK), self.target)
            self.pings += 1
            self.next_ping = now + PING_EVERY

    def close(self):
        if self.state == "idle":
            self.seq += 1
            try:
                self.sock.sendto(self._packet(3, 0), self.target)
            except OSError:
                pass
        self.sock.close()


def worker(index, players, ports, start_at, ramp, stop, logged_in, results):
    """One process: its share of the players, in a select loop."""
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        import ac3server.protocol.prudp  # noqa: F401  - imported quietly here, before the clients use it
    auth, secure = ("127.0.0.1", ports["auth"]), ("127.0.0.1", ports["secure"])
    sel = selectors.DefaultSelector()
    clients = []
    for name in players:
        c = Client(name, name, auth, secure)
        c.begin_at = start_at + (random.uniform(0, ramp) if ramp else 0)
        clients.append(c)
        sel.register(c.sock, selectors.EVENT_READ, c)
    counted = set()
    while not stop.is_set():
        now = time.time()
        for c in clients:
            if c.state == "start" and now >= c.begin_at:
                c.begin(now)
            c.tick(now)
            if c.state in ("idle", "done") and c.name not in counted:
                counted.add(c.name)
                with logged_in.get_lock():
                    logged_in.value += 1
        for key, _mask in sel.select(timeout=0.02):
            c = key.data
            while True:
                try:
                    data, addr = c.sock.recvfrom(4096)
                except (BlockingIOError, ConnectionResetError):
                    break
                try:
                    c.on_packet(data, addr)
                except Exception as exc:
                    c.failed, c.state = f"client error: {exc}", "done"
                    break
    out = [{"name": c.name, "login": c.logged_in, "failed": c.failed, "pings": c.pings,
            "settings": next((n for p, m, n in c.steps if (p, m) == (0x18, 4)), 0), "pushes": c.pushes}
           for c in clients]
    for c in clients:
        c.close()
    results.put((index, out))


# --- the run ----------------------------------------------------------------------------------------------------

def cpu(proc):
    import psutil
    t = psutil.Process(proc.pid).cpu_times()
    return t.user + t.system


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--players", type=int, default=300)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--ramp", type=float, default=0.0)
    ap.add_argument("--idle", type=float, default=40.0)
    ap.add_argument("--friends", type=int, default=5,
                    help="friendships per player (friends policy 'requests'); -1 = friends: everyone")
    args = ap.parse_args()
    try:
        import psutil  # noqa: F401  - only checked for here; cpu() uses it
    except ImportError:
        raise SystemExit("pip install psutil (the load test measures the server processes' CPU time with it)")

    tmp = tempfile.mkdtemp(prefix="ac3_load_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    ports = {"http": free_port(socket.SOCK_STREAM), "status": free_port(socket.SOCK_STREAM),
             "auth": free_port(socket.SOCK_DGRAM), "secure": free_port(socket.SOCK_DGRAM)}
    config = os.path.join(tmp, "server_config.json")
    with open(config, "w", encoding="utf-8") as fh:
        json.dump({"address": "127.0.0.1", "status_port": ports["status"], "log_packets": False,
                   "friends": "everyone" if args.friends < 0 else "requests"}, fh)
    env = dict(os.environ, AC3_CONFIG=config, AC3_LOG_DIR=os.path.join(tmp, "logs"), AC3_DATA_DIR=data,
               AC3_HTTP_PORT=str(ports["http"]), AC3_AUTH_PORT=str(ports["auth"]),
               AC3_SECURE_PORT=str(ports["secure"]), AC3_SERVER_VERSION="load", PYTHONUNBUFFERED="1")

    names = [f"load{i:04d}" for i in range(args.players)]
    if args.friends > 0:
        # every player friends with --friends others, by pid (stable_user_pid of the name)
        import zlib
        pid_of = {n: 1000 + (zlib.crc32(n.lower().encode()) % 100_000_000) for n in names}
        table = {}
        for i, n in enumerate(names):
            for k in range(1, args.friends // 2 + 1):
                m = names[(i + k) % len(names)]
                table.setdefault(str(pid_of[n]), set()).add(pid_of[m])
                table.setdefault(str(pid_of[m]), set()).add(pid_of[n])
        with open(os.path.join(data, "friends.json"), "w", encoding="utf-8") as fh:
            json.dump({k: sorted(v) for k, v in table.items()}, fh)

    procs = {}

    def start(args, name):
        log = open(os.path.join(tmp, f"{name}.out"), "w", encoding="utf-8")
        procs[name] = subprocess.Popen([sys.executable] + args, cwd=ROOT, env=env, stdout=log,
                                       stderr=subprocess.STDOUT)

    start([os.path.join(ROOT, "onlineconfig", "onlineconfig_server.py")], "http")
    start(["-m", "ac3server"], "prudp")
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{ports['http']}/status", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.5)
    else:
        raise SystemExit(f"the server did not come up - see {tmp}")
    print(f"server up (temporary folder {tmp}); {args.players} players in {args.workers} workers, "
          f"ramp {args.ramp:g} s, idle {args.idle:g} s, "
          f"{'friends: everyone' if args.friends < 0 else f'{args.friends} friends each'}")

    try:
        cpu0 = {n: cpu(p) for n, p in procs.items()}
        t0 = time.time()
        start_at = t0 + 2.0
        stop = multiprocessing.Event()
        logged_in = multiprocessing.Value("i", 0)
        results = multiprocessing.Queue()
        shares = [names[i::args.workers] for i in range(args.workers)]
        workers = [multiprocessing.Process(target=worker, args=(i, shares[i], ports, start_at, args.ramp, stop,
                                                                logged_in, results)) for i in range(args.workers)]
        for w in workers:
            w.start()
        give_up = start_at + args.ramp + 180.0
        while logged_in.value < args.players and time.time() < give_up:
            time.sleep(0.2)
        storm_wall = time.time() - start_at
        cpu_storm = {n: cpu(p) - cpu0[n] for n, p in procs.items()}
        time.sleep(args.idle)
        cpu_idle = {n: cpu(p) - cpu0[n] - cpu_storm[n] for n, p in procs.items()}
        stop.set()
        rows = []
        for _ in workers:
            _i, out = results.get(timeout=60)
            rows += out
        for w in workers:
            w.join(10)
        time.sleep(3)                                # the disconnects reach the server and are handled
        status = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{ports['http']}/status", timeout=5).read())
    finally:
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            p.wait(10)

    logins = sorted(r["login"] for r in rows if r["login"] is not None)
    failed = [r for r in rows if r["failed"]]
    log_text = ""
    for f in os.listdir(os.path.join(tmp, "logs")):
        log_text += open(os.path.join(tmp, "logs", f), encoding="utf-8", errors="replace").read()

    def pct(p):
        return logins[min(len(logins) - 1, int(len(logins) * p))] if logins else float("nan")

    print("")
    print(f"logged in: {len(logins)} of {len(rows)}; failed: {len(failed)}")
    for r in failed[:5]:
        print(f"   {r['name']}: {r['failed']}")
    print(f"login time: median {pct(0.5):.2f} s, 90% {pct(0.9):.2f} s, slowest {pct(0.999):.2f} s")
    print(f"server CPU, logins (first start to last login, {storm_wall:.1f} s): game server {cpu_storm['prudp']:.1f} s, "
          f"HTTP {cpu_storm['http']:.1f} s")
    print(f"server CPU, idle ({args.idle:g} s, {sum(r['pings'] for r in rows)} pings): game server "
          f"{cpu_idle['prudp']:.2f} s = {100 * cpu_idle['prudp'] / args.idle:.1f}% of a core, HTTP "
          f"{cpu_idle['http']:.2f} s = {100 * cpu_idle['http'] / args.idle:.1f}% of a core")
    sizes = sorted({r["settings"] for r in rows if r["login"] is not None})
    print(f"game settings download per player: {sizes} bytes; notifications pushed to the players: "
          f"{sum(r['pushes'] for r in rows)}")
    print(f"/status after every player disconnected: {status['prudp'].get('players_online')} online")
    for marker in ("GIVING UP", "handler error", "GAP:", "duplicate seq", "***"):
        print(f"server log '{marker}': {log_text.count(marker)}")
    return 0 if not failed and len(logins) == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
