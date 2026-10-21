"""NAT type detection for AC3: our own answer to the game's "punch detect" servers (server_config nat_detect = "own").

The game finds its NAT type at every start with Ubisoft's Storm library (stormnatcheck, DetectNatTypeStateMachine in
AC3MP.exe) against the two servers onlineconfig's punch_DetectUrls names (Ubisoft's lb-ne1z-prod-mpe-detect01/02 :11000).
Read from the client and checked against Ubisoft's live server.

The wire (big-endian, UDP):
  request  13 bytes  [u16 length = 13][u16 command][u8 sequence][u8 0][7 bytes the client never fills]
                     (send: 0x11E26E0)
  reply    19 bytes  [u16 length = 19][u16 command, echoed][u8 sequence, echoed][u8 2][00 00 90 00 00 00 00]
                     [u32 the address the request came from][u16 its port]   (parse: 0x11E1FD0; Ubisoft's bytes)
  command 1  answer from the socket it arrived on       -> the client's "reply" event
  command 2  answer from ANOTHER PORT of the same server -> "reply from the next port"
  command 3  answer from the OTHER SERVER                -> "reply from the next server"
The client checks only the echoed command of a 2 / 3 reply, never where it came from.

What the client does with them (0x11E49A0, the states; 0x11E4120, the verdict):
  1. command 1 to server 1. If the address reported is its own local address there is no NAT: Open, done.
  2. command 3 to server 1 (up to 3 tries). If server 2's unsolicited answer gets in, the NAT lets in anybody
     (full cone) and it asks command 2 next; otherwise it sends command 1 to server 2 and goes on.
  3. command 1 to server 1's address at its PORT + 1. The same public port as in step 1 -> the NAT keeps one mapping
     for every destination (cone): Open after a successful step 2, else Moderate. Another port -> symmetric: Strict.
  Storm's result 3 Open / 2 Moderate / 1 Strict / 0 Unknown becomes the game's NATType 1 Open / 2 Moderate / 3 Strict
  (0x402000) - the value the game puts in its sessions and filters matchmaking with (UseSessionNatFilter).

So a server answers on three sockets: server 1 = (address, port) and (address, port + 1); server 2 = (address2, port)
when a second address is configured, else (address, port + 2). With one address, "server 2" differs from server 1 by
port only, so a NAT that admits any port of an address it has sent to (address-restricted cone) reads as Open where
two addresses would read Moderate - the one case one address cannot tell apart.

The client's next step shows what it saw, so the server follows each client's run and reaches the same verdict
(`verdict(ip)`): that is how the group's host score learns a player's NAT type before any session exists.
"""

import socket
import struct
import threading
import time


HEADER = struct.Struct(">HHBB")             # length, command, sequence, flag
REQUEST_MIN, REPLY_SIZE = 6, 19
REPLY_FLAG = 2
REPLY_MIDDLE = bytes.fromhex("00009000000000")
CMD_REPLY, CMD_NEXT_PORT, CMD_NEXT_SERVER = 1, 2, 3

GAME_OPEN, GAME_MODERATE, GAME_STRICT, GAME_UNKNOWN = 1, 2, 3, 0
GAME_NAMES = {GAME_OPEN: "open", GAME_MODERATE: "moderate", GAME_STRICT: "strict", GAME_UNKNOWN: "unknown"}

RUN_TTL = 30.0            # a client's run: a new one starts with a command 1 to server 1 after this long
NO_NAT_QUIET = 4.0        # a client that asks nothing after its first answer saw its own address: no NAT
VERDICT_TTL = 6 * 3600.0  # how long a verdict describes an address
RATE_WINDOW, RATE_MAX = 10.0, 30   # packets per address per window; more are dropped (no reflection tool)


def build_reply(request, source):
    """The reply to `request` (bytes) that came from `source` (ip, port), or None if it is not a request."""
    if len(request) < REQUEST_MIN:
        return None
    _length, command, sequence, _flag = HEADER.unpack_from(request)
    if command not in (CMD_REPLY, CMD_NEXT_PORT, CMD_NEXT_SERVER):
        return None
    try:
        address = socket.inet_aton(source[0])
    except OSError:
        return None
    return HEADER.pack(REPLY_SIZE, command, sequence, REPLY_FLAG) + REPLY_MIDDLE + address + struct.pack(">H", source[1])


class Detector:
    """The three sockets, the replies, and each client's run followed to the game's own verdict."""

    def __init__(self, host="0.0.0.0", port=11000, host2="", log=print):
        self.port, self.log = port, log
        self.main = self._bind(host, port)
        self.alt = self._bind(host, port + 1)
        self.other = self._bind(host2, port) if host2 else self._bind(host, port + 2)
        self.lock = threading.Lock()
        self.runs = {}         # client ip -> {first public port: {"start", "first", "last", "next_server", "steps"}}
        self.verdicts = {}     # client ip -> (game NAT type, time)
        self.rate = {}         # client ip -> [window start, count]

    @staticmethod
    def _bind(host, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((host, port))
        return s

    def ports(self):
        return [s.getsockname() for s in (self.main, self.alt, self.other)]

    def close(self):
        for s in (self.main, self.alt, self.other):
            s.close()

    # --- the wire ---------------------------------------------------------------------------------------------
    def handle(self, sock, data, source):
        """One request on `sock`: follow the client's run, then answer from the right socket."""
        if not self._allowed(source[0]):
            return
        reply = build_reply(data, source)
        if reply is None:
            return
        command = HEADER.unpack_from(data)[1]
        self._follow(sock, command, source)
        if sock is self.main and command == CMD_NEXT_PORT:
            out = self.alt
        elif sock is self.main and command == CMD_NEXT_SERVER:
            out = self.other
        else:
            out = sock
        try:
            out.sendto(reply, source)
        except OSError as exc:
            self.log(f"[natdetect] could not answer {source}: {exc}")

    def _allowed(self, ip):
        now = time.time()
        with self.lock:
            window = self.rate.get(ip)
            if not window or now - window[0] > RATE_WINDOW:
                self.rate[ip] = [now, 1]
                return True
            window[1] += 1
            return window[1] <= RATE_MAX

    # --- following a client's run (DetectNatTypeStateMachine's steps, seen from here) -------------------------
    def _run_for(self, ip, port, now):
        """The run a packet from (ip, port) belongs to. Runs are told apart by the public port their first packet
        came from (players behind one router run at once); a packet from another port of the same address belongs to
        that address's only run (a symmetric NAT moves the port with the destination), else to none."""
        runs = self.runs.get(ip, {})
        for first in [f for f, r in runs.items() if now - r["start"] > RUN_TTL]:
            runs.pop(first)
        if port in runs:
            return runs[port]
        return next(iter(runs.values())) if len(runs) == 1 else None

    def _follow(self, sock, command, source):
        ip, port, now = source[0], source[1], time.time()
        with self.lock:
            run = self._run_for(ip, port, now)
            if sock is self.main and command == CMD_REPLY and (run is None or run["first"] != port):
                run = {"start": now, "first": port, "last": now, "next_server": None, "steps": []}
                self.runs.setdefault(ip, {})[port] = run
            if run is None:
                return
            run["last"] = now
            if sock is self.main:
                run["steps"].append(str(command))
                if command == CMD_NEXT_PORT:
                    run["next_server"] = True          # it asks for the next port only after the next server got in
            elif sock is self.other and command == CMD_REPLY:
                run["steps"].append("1@2")
                run["next_server"] = False             # the next server never got in: it asks server 2 directly
            elif sock is self.alt and command == CMD_REPLY:
                run["steps"].append("1@+1")
                if port != run["first"]:
                    verdict = GAME_STRICT              # another public port for another destination: symmetric
                elif run["next_server"]:
                    verdict = GAME_OPEN
                else:
                    verdict = GAME_MODERATE
                self._settle(ip, verdict, run)

    def _settle(self, ip, verdict, run):
        self.verdicts[ip] = (verdict, time.time())
        self.runs.get(ip, {}).pop(run["first"], None)
        self.log(f"[natdetect] {ip}: {GAME_NAMES[verdict]} (the game's NATType {verdict}; steps "
                 f"{' '.join(run['steps'])})")

    def verdict(self, ip):
        """The game's NATType for a client address as its last run here showed it (GAME_UNKNOWN: no run seen)."""
        now = time.time()
        with self.lock:
            for run in list(self.runs.get(ip, {}).values()):
                if run["steps"] == ["1"] and now - run["last"] > NO_NAT_QUIET:
                    run["steps"].append("(nothing more: it saw its own address - no NAT)")
                    self._settle(ip, GAME_OPEN, run)
            found = self.verdicts.get(ip)
            if found and now - found[1] <= VERDICT_TTL:
                return found[0]
            return GAME_UNKNOWN

    # --- serving ------------------------------------------------------------------------------------------------
    def serve_forever(self):
        import select
        socks = [self.main, self.alt, self.other]
        while True:
            try:
                ready, _, _ = select.select(socks, [], [], 1.0)
            except (OSError, ValueError):
                return                                  # closed (a closed socket's descriptor reads -1)
            for sock in ready:
                try:
                    data, source = sock.recvfrom(256)
                except (ConnectionResetError, OSError):
                    continue                            # Windows reports a vanished peer as a recv error
                try:
                    self.handle(sock, data, source)
                except Exception as exc:                # one bad packet must not end the service
                    self.log(f"[natdetect] error on a packet from {source}: {exc}")

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True, name="natdetect").start()
        (h1, p1), (_, p2), (h3, p3) = self.ports()
        self.log(f"[natdetect] answering the game's NAT detection on UDP {p1} and {p2} (server 1) and "
                 f"{h3 if h3 != '0.0.0.0' else ''}:{p3} (server 2)")
        return self


__all__ = ["Detector", "build_reply", "GAME_OPEN", "GAME_MODERATE", "GAME_STRICT", "GAME_UNKNOWN", "GAME_NAMES"]
