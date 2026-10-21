"""PortServer: one UDP port speaking PRUDP - connections, ACKs, reliable delivery, fragments, and RMC dispatch.

The game opens two PRUDP connections, each to its own PortServer: first the authentication server (21006), then,
with the ticket that one hands out, the secure server (21007), which carries every game service after login. Both
run this same code with a different dispatch table (ac3server/dispatch.py).

A connection's life:

    SYN          the client asks for a connection signature; we hand out the next one
    CONNECT      opens the connection (resetting anything left from an earlier one on the same address and vport);
                 on the secure port it carries an authenticator encrypted with the ticket's session key, which we
                 decrypt and answer with challenge + 1
    DATA         an RMC message, or a part of one; reliable DATA is ACKed, de-duplicated, put in sequence order,
                 reassembled, and handed to the handler registered for its (protocol, method)
    PING         keep-alive (about every 20 s), ACKed
    DISCONNECT   or CONNECTION_TIMEOUT seconds of silence: the connection is forgotten

What we send is paced: at most RELIABLE_WINDOW reliable packets in flight per connection, each resent until ACKed.
The comments below say what breaks without each rule.

Game state is not this module's business. The server that owns it (the secure one) is given two hooks by
ac3server/app.py: `on_disconnect(addr)` when a connection ends, and `on_tick()` on every pass of the loop.
"""

import collections
import socket
import time
import traceback

import server_config as config

from ac3server.protocol.crypto import PLACEHOLDER_SESSION_KEY
from ac3server.protocol.prudp import (FLAG_ACK, FLAG_NEED_ACK, FLAG_RELIABLE, MAX_RMC_PAYLOAD_SIZE, PrudpPacket,
    build_connect_reply_payload, parse_connect_auth_payload)
from ac3server.protocol.rmc import parse_rmc_request, parse_rmc_response
from ac3server.redaction import loggable_payload
from ac3server.runtime import STATE_LOCK


# Pacing our own reliable DATA. The client drops most of a burst, and PRUDP reliability is the sender's job, so
# without a window a large reply (the 63-packet game settings download) times out. Ubisoft's server likewise sends
# each fragment after the client's ACK of the one before.
RELIABLE_WINDOW = 4          # reliable DATA packets in flight per connection
RETRANSMIT_TIMEOUT = 0.25    # seconds before an unACKed packet is first resent; doubled each time ...
RETRANSMIT_MAX_INTERVAL = 2.0    # ... up to this
GIVE_UP_AFTER = 30.0         # seconds after it was first sent, an unACKed packet is dropped (and logged)
# Time, not a count of resends: the resend check runs after every packet handled, so under load a count would run out
# within moments while the ACKs wait unread, and replies would be lost. For the same reason every packet already
# waiting is handled before the check (DRAIN_MAX at most per pass).
DRAIN_MAX = 256

# A reliable packet the client sends again (our ACK reached it late) is ACKed again but not handled again. Handling
# it twice re-runs the call and, worse, re-buffers the first part of a message already delivered, so the next
# request is glued onto it and misread, which can hang the game. Sequence ids are 16-bit; remembering the last 1024
# per connection is far inside a wrap.
SEEN_SEQ_WINDOW = 1024

# Reliable DATA is handled in sequence order: a fragment lost and resent after its successors would otherwise be
# reassembled out of order and the request garbled. The client's seq ids run without a gap from CONNECT's + 1, so a
# packet ahead of the next expected one is held until the gap fills. Safety valve: a
# gap still open after IN_ORDER_GAP_TIMEOUT seconds, or more than IN_ORDER_MAX_HELD packets held, is skipped and
# logged rather than stalling the connection.
IN_ORDER_GAP_TIMEOUT = 5.0
IN_ORDER_MAX_HELD = 128


class PortServer:
    """One PRUDP listener with its own connection state and RMC dispatch table."""

    def __init__(self, port: int, rmc_dispatch: dict, label: str, on_disconnect=None, on_tick=None):
        self.port = port
        self.rmc_dispatch = rmc_dispatch
        self.label = label
        self.on_disconnect = on_disconnect      # f(addr), after a connection's own state is gone
        self.on_tick = on_tick                  # f(), on every pass of the receive loop
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.next_conn_sig = 1
        # Per address, or per connection: key = (addr, the client's vport).
        self.clients = {}             # addr -> connection signature we assigned in SYN
        self.client_conn_sigs = {}    # key -> the client's connection signature (from CONNECT)
        self.out_seq = {}             # key -> next seq id of our own DATA
        self.incoming_fragments = {}  # key -> parts of a request still being reassembled
        self.seen_seq = {}            # key -> (set, deque) of reliable seq ids already handled (SEEN_SEQ_WINDOW)
        self.in_next = {}             # key -> the next reliable seq id to hand on
        self.in_ahead = {}            # key -> {seq: (packet, time held)} waiting for a gap (IN_ORDER_GAP_TIMEOUT)
        self.pending = {}             # key -> {seq: {"raw", "addr", "desc", "first", "sent", "tries"}} not yet ACKed
        self.send_queue = {}          # key -> deque of (PrudpPacket, addr, desc) not yet sent (RELIABLE_WINDOW)
        # How to address a client without a request of its own to reply to (a notification): its two vports and
        # the PRUDP session id, recorded from its CONNECT and refreshed by every DATA packet.
        self.conn_ctx = {}            # key -> {"src_vport", "dst_vport", "session_id", "addr"}
        # key -> (seq id, connection signature, session id) of the CONNECT that opened the current connection: the
        # same CONNECT again is the client resending it (our ACK was slow), not a new connection
        self.connect_seen = {}
        # addr -> time of its last packet. A client that vanishes without a DISCONNECT (a crash, a cable, a VPN)
        # is dropped after CONNECTION_TIMEOUT seconds of silence; otherwise it would stay online in every friends
        # list and come back as "name#2".
        self.last_seen = {}

    def run(self):
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.settimeout(0.05)  # wake up regularly to retransmit unACKed packets
        print(f"[{self.label}] Listening for PRUDP on UDP {self.port}...")
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                try:
                    with STATE_LOCK:
                        self.periodic_checks()
                except Exception:
                    print(f"[{self.label}] *** error in the periodic checks:\n{traceback.format_exc()}")
                continue
            except ConnectionResetError:
                # Windows reports ICMP port-unreachable for a peer that went
                # away as a recv error; nothing to do but keep serving.
                continue
            batch = [(data, addr)]
            # everything else already waiting, before the resend check: an ACK queued behind other packets must
            # be seen first (DRAIN_MAX)
            self.sock.setblocking(False)
            try:
                while len(batch) < DRAIN_MAX:
                    batch.append(self.sock.recvfrom(4096))
            except (BlockingIOError, ConnectionResetError):
                pass
            finally:
                self.sock.settimeout(0.05)
            for data, addr in batch:
                self._receive(data, addr)
            try:
                with STATE_LOCK:
                    self.periodic_checks()
            except Exception:
                print(f"[{self.label}] *** error in the periodic checks:\n{traceback.format_exc()}")

    def _receive(self, data, addr):
        try:
            pkt = PrudpPacket.parse(data)
        except Exception as e:
            print(f"[{self.label}][{addr}] failed to parse ({len(data)} bytes): {e}")
            return
        try:
            with STATE_LOCK:
                self.handle_packet(pkt, addr)
        except Exception:
            # One bad packet or handler bug must not end this listener's
            # thread (the process would keep running, deaf). Log and serve on.
            print(f"[{self.label}][{addr}] *** handler error, packet dropped:\n{traceback.format_exc()}")

    def periodic_checks(self):
        self.check_retransmits()
        self.check_stalled_gaps()
        self.check_stale_clients()
        if self.on_tick:
            self.on_tick()

    def check_stale_clients(self):
        """Treat a client silent for CONNECTION_TIMEOUT seconds as disconnected."""
        if config.CONNECTION_TIMEOUT <= 0:
            return
        now = time.time()
        for addr, seen in list(self.last_seen.items()):
            if now - seen <= config.CONNECTION_TIMEOUT:
                continue
            self.last_seen.pop(addr, None)
            live = addr in self.clients or any(k[0] == addr for k in self.conn_ctx)
            if not live:
                continue
            print(f"[{self.label}][{addr}] silent for {now - seen:.0f} s (the client pings every ~20 s): "
                  f"treated as disconnected")
            self.clients.pop(addr, None)
            self.drop_connection_state(addr)

    # --- reliable outgoing DATA (window + retransmit) --------------------
    def queue_reliable(self, key, resp, addr, desc):
        self.send_queue.setdefault(key, collections.deque()).append((resp, addr, desc))
        self.pump(key)

    def pump(self, key):
        pend = self.pending.setdefault(key, {})
        queue = self.send_queue.get(key)
        while queue and len(pend) < RELIABLE_WINDOW:
            resp, addr, desc = queue.popleft()
            out = resp.build()
            if config.LOG_PACKETS:
                print(f"[{self.label}][{addr}] SEND {desc} {resp.describe()}")
            self.sock.sendto(out, addr)
            now = time.time()
            pend[resp.seq_id] = {"raw": out, "addr": addr, "desc": desc,
                                 "first": now, "sent": now, "tries": 0}

    def on_ack(self, key, seq_id, addr):
        pend = self.pending.get(key)
        if pend and seq_id in pend:
            pend.pop(seq_id)
            self.pump(key)

    def check_retransmits(self):
        now = time.time()
        for key, pend in list(self.pending.items()):
            for seq_id, entry in list(pend.items()):
                if now - entry["sent"] < min(RETRANSMIT_TIMEOUT * 2 ** entry["tries"], RETRANSMIT_MAX_INTERVAL):
                    continue
                if now - entry["first"] >= GIVE_UP_AFTER:
                    print(f"[{self.label}][{entry['addr']}] GIVING UP on {entry['desc']} seq={seq_id:#06x} "
                          f"after {entry['tries']} resends over {now - entry['first']:.0f} s (no ACK)")
                    pend.pop(seq_id)
                    self.pump(key)
                    continue
                entry["tries"] += 1
                entry["sent"] = now
                if config.LOG_PACKETS:
                    print(f"[{self.label}][{entry['addr']}] RESEND {entry['desc']} seq={seq_id:#06x} "
                          f"(try {entry['tries']})")
                self.sock.sendto(entry["raw"], entry["addr"])

    def drop_connection_state(self, addr):
        """Forget everything about the connections from `addr`, then tell the owner of the game state."""
        for key in [k for k in list(self.pending) + list(self.send_queue) if k[0] == addr]:
            self.pending.pop(key, None)
            self.send_queue.pop(key, None)
        for key in [k for k in list(self.seen_seq) + list(self.incoming_fragments)
                    + list(self.in_next) + list(self.in_ahead) if k[0] == addr]:
            self.seen_seq.pop(key, None)
            self.incoming_fragments.pop(key, None)
            self.in_next.pop(key, None)
            self.in_ahead.pop(key, None)
        for key in [k for k in list(self.conn_ctx) + list(self.connect_seen) if k[0] == addr]:
            self.conn_ctx.pop(key, None)
            self.connect_seen.pop(key, None)
        if self.on_disconnect:
            self.on_disconnect(addr)

    def handle_packet(self, pkt, addr):
        if config.LOG_PACKETS:
            print(f"[{self.label}][{addr}] RECV {pkt.describe()}")
        self.last_seen[addr] = time.time()

        reply = None
        if pkt.ptype == 0:  # SYN
            conn_sig = self.clients.setdefault(addr, self.next_conn_sig)
            if conn_sig == self.next_conn_sig:
                self.next_conn_sig += 1
            reply = PrudpPacket()
            reply.src_vport = pkt.dst_vport
            reply.dst_vport = pkt.src_vport
            reply.ptype = 0
            reply.flags = FLAG_ACK
            reply.session_id = pkt.session_id
            reply.signature = pkt.signature
            reply.seq_id = pkt.seq_id
            reply.connection_signature = conn_sig
        elif pkt.ptype == 1:  # CONNECT
            conn_sig = self.clients.get(addr, self.next_conn_sig)
            key = (addr, pkt.src_vport.port)
            self.client_conn_sigs[key] = pkt.connection_signature
            ident = (pkt.seq_id, pkt.connection_signature, pkt.session_id)
            if self.connect_seen.get(key) == ident:
                # the SAME CONNECT again: the client resending it because our ACK was slow (a busy server). It is
                # ACKed again and nothing else - resetting would throw away the replies already queued on the
                # connection, never to be resent, and the player would wait for ever.
                print(f"[{self.label}][{addr}]   the same CONNECT again (a resend): ACKed, the connection kept")
            else:
                # A new connection on this (addr, vport): reset everything of the last one. Without this, a second
                # launch of the game against a running server gets replies numbered on from the old connection and
                # never gets past LoginEx.
                self.connect_seen[key] = ident
                self.out_seq[key] = 1
                self.incoming_fragments.pop(key, None)
                self.seen_seq.pop(key, None)
                self.in_next[key] = (pkt.seq_id + 1) & 0xFFFF
                self.in_ahead.pop(key, None)
                self.pending.pop(key, None)
                self.send_queue.pop(key, None)
                self._remember_conn(key, pkt, addr)
            reply = PrudpPacket()
            reply.src_vport = pkt.dst_vport
            reply.dst_vport = pkt.src_vport
            reply.ptype = 1
            reply.flags = FLAG_ACK
            reply.session_id = pkt.session_id
            reply.signature = pkt.connection_signature
            reply.seq_id = pkt.seq_id
            reply.connection_signature = conn_sig
            if pkt.payload:
                try:
                    pid, cid, challenge = parse_connect_auth_payload(pkt.payload, PLACEHOLDER_SESSION_KEY)
                    print(f"[{self.label}][{addr}]   CONNECT auth: pid={pid} cid={cid} challenge={challenge:#010x}")
                    reply.payload = build_connect_reply_payload(challenge)
                except Exception as e:
                    print(f"[{self.label}][{addr}]   CONNECT payload parse failed: {e}")
        elif pkt.ptype in (2, 4):  # DATA, PING
            if pkt.ptype == 2 and (pkt.flags & FLAG_ACK):
                # the client ACKing one of our reliable packets: the next queued one may go out
                self.on_ack((addr, pkt.src_vport.port), pkt.seq_id, addr)
            if pkt.ptype == 2:
                self._remember_conn((addr, pkt.src_vport.port), pkt, addr)
                if config.LOG_PACKETS:
                    print(f"[{self.label}][{addr}]   -> decoded payload ({len(pkt.payload)} bytes): "
                          f"{loggable_payload(pkt.payload)}")
            if pkt.flags & FLAG_NEED_ACK:
                reply = PrudpPacket()
                reply.src_vport = pkt.dst_vport
                reply.dst_vport = pkt.src_vport
                reply.ptype = pkt.ptype
                reply.flags = FLAG_ACK
                reply.session_id = pkt.session_id
                reply.signature = self.client_conn_sigs.get((addr, pkt.src_vport.port), 0)
                reply.seq_id = pkt.seq_id
        elif pkt.ptype == 3:  # DISCONNECT
            self.clients.pop(addr, None)
            self.drop_connection_state(addr)

        if reply is not None:
            out = reply.build()
            if config.LOG_PACKETS:
                print(f"[{self.label}][{addr}] SEND {reply.describe()}  raw={out.hex()}")
            self.sock.sendto(out, addr)

        # Then the RMC message itself, if this DATA carries one. A request may come in parts (part 1, 2, ..., then
        # a final part 0), buffered until the last. A bare ACK also has part 0 and an empty payload: it must never
        # be taken for a last part, or it would cut a buffered request short.
        if pkt.ptype == 2 and not (pkt.flags & FLAG_ACK):
            key = (addr, pkt.src_vport.port)
            if pkt.flags & FLAG_RELIABLE and self._seen_before(key, pkt.seq_id):
                # already ACKed again above; handling it again would re-run the
                # call or corrupt reassembly (SEEN_SEQ_WINDOW)
                print(f"[{self.label}][{addr}]   duplicate seq={pkt.seq_id:#06x} "
                      f"(a client retransmit): ACKed, not handled again")
                return
            if pkt.flags & FLAG_RELIABLE:
                for ready in self._in_order(key, pkt, addr):
                    self._deliver(key, ready, addr)
                return
            self._deliver(key, pkt, addr)

    def _in_order(self, key, pkt, addr):
        """Reliable DATA, put in sequence order (IN_ORDER_GAP_TIMEOUT).
        -> the packets that can be handled now, in order."""
        seq = pkt.seq_id
        nxt = self.in_next.get(key)
        if nxt is None:
            # no CONNECT seen for this connection (the server was restarted
            # under a running client): take up the sequence here
            nxt = seq
        dist = (seq - nxt) & 0xFFFF
        if dist >= 0x8000:
            # behind the next expected one and not caught as a duplicate: a
            # packet whose gap was already skipped (safety valve) - too late
            print(f"[{self.label}][{addr}]   late seq={seq:#06x} (expected {nxt:#06x}, "
                  f"its gap was skipped): ACKed, not handled")
            return []
        if dist:
            ahead = self.in_ahead.setdefault(key, {})
            ahead[seq] = (pkt, time.time())
            self.in_next[key] = nxt
            print(f"[{self.label}][{addr}]   out of order: seq={seq:#06x} held until "
                  f"{nxt:#06x} arrives ({len(ahead)} held)")
            return self._release(key, addr, force=False)
        return self._drain(key, addr, [pkt], (nxt + 1) & 0xFFFF)

    def _drain(self, key, addr, ready, nxt):
        """Append every held packet that now follows on; store the new next."""
        ahead = self.in_ahead.get(key, {})
        released = 0
        while nxt in ahead:
            ready.append(ahead.pop(nxt)[0])
            nxt = (nxt + 1) & 0xFFFF
            released += 1
        self.in_next[key] = nxt
        if released:
            print(f"[{self.label}][{addr}]   gap filled: {released} held packet(s) released in order")
        return ready

    def _release(self, key, addr, force):
        """Safety valve: skip a gap that has stayed open too long (or too many
        packets held) instead of stalling the connection. -> packets to handle."""
        ahead = self.in_ahead.get(key)
        if not ahead:
            return []
        oldest = min(t for _p, t in ahead.values())
        if not force and len(ahead) < IN_ORDER_MAX_HELD and time.time() - oldest < IN_ORDER_GAP_TIMEOUT:
            return []
        nxt = self.in_next[key]
        first = min(ahead, key=lambda s_: (s_ - nxt) & 0xFFFF)
        print(f"[{self.label}][{addr}]   GAP: seq {nxt:#06x}..{(first - 1) & 0xFFFF:#06x} never "
              f"arrived ({len(ahead)} held, oldest {time.time() - oldest:.1f}s) - skipping it; "
              f"a request split across the gap is lost")
        # a request cut by the gap can't be completed: drop its first half
        self.incoming_fragments.pop(key, None)
        return self._drain(key, addr, [ahead.pop(first)[0]], (first + 1) & 0xFFFF)

    def check_stalled_gaps(self):
        for key in list(self.in_ahead):
            ready = self._release(key, key[0], force=False)
            for p in ready:
                self._deliver(key, p, key[0])

    def _deliver(self, key, pkt, addr):
        """Handle one DATA packet, in order: buffer a fragment, or reassemble
        and dispatch a complete RMC request."""
        if pkt.part_number != 0:
            self.incoming_fragments.setdefault(key, bytearray())
            self.incoming_fragments[key] += pkt.payload
            return
        buffered = self.incoming_fragments.pop(key, None)
        full_payload = (bytes(buffered) + pkt.payload) if buffered else pkt.payload
        if not full_payload:
            return
        try:
            _size, is_request, proto, call_id, method_id, params = parse_rmc_request(full_payload)
        except Exception as e:
            print(f"[{self.label}][{addr}]   RMC envelope parse failed: {e}")
            return
        print(f"[{self.label}][{addr}]   RMC: proto={proto:#x} call_id={call_id} "
              f"method_id={method_id} is_request={is_request}")

        if not is_request:
            # A response from the client: only ever to a request of ours (a notification), which the client does
            # not normally answer. Logged, as direct evidence a notification was understood.
            try:
                ok, err, r_proto, r_call, r_method, r_params = parse_rmc_response(full_payload)
            except Exception as exc:
                print(f"[{self.label}][{addr}]   client response unparsable: {exc}")
                return
            if ok:
                print(f"[{self.label}][{addr}]   client RESPONSE to our call "
                      f"{r_call} (proto={r_proto:#x} method={r_method}): "
                      f"{r_params.hex() or '(no params)'}")
            else:
                print(f"[{self.label}][{addr}]   client ERROR response to our "
                      f"call {r_call} (proto={r_proto:#x}): {err:#010x}")
            return
        handler = self.rmc_dispatch.get((proto, method_id))
        if handler is None:
            print(f"[{self.label}][{addr}]   No handler for proto={proto:#x} method_id={method_id}")
            return
        label, response_payload = handler(call_id, params, addr)
        self.send_rmc_response(pkt, addr, response_payload, label)

    def _seen_before(self, key, seq_id):
        """True if this reliable DATA seq id was already handled on this
        connection; otherwise record it and return False."""
        seen, order = self.seen_seq.setdefault(
            key, (set(), collections.deque(maxlen=SEEN_SEQ_WINDOW)))
        if seq_id in seen:
            return True
        if len(order) == order.maxlen:
            seen.discard(order[0])
        order.append(seq_id)
        seen.add(seq_id)
        return False

    def _remember_conn(self, key, pkt, addr):
        """Record how to address this client unprompted (conn_ctx): a reply copies these off the request, a
        notification has no request to copy from."""
        self.conn_ctx[key] = {"src_vport": pkt.dst_vport, "dst_vport": pkt.src_vport,
                              "session_id": pkt.session_id, "addr": addr}

    def send_rmc_response(self, pkt, addr, response_payload, label):
        """An RMC response as reliable DATA, in parts of MAX_RMC_PAYLOAD_SIZE (acb-rdv's MakeAndSend), paced
        through the send window."""
        key = (addr, pkt.src_vport.port)
        self._send_rmc_payload(key, {"src_vport": pkt.dst_vport,
                                     "dst_vport": pkt.src_vport,
                                     "session_id": pkt.session_id,
                                     "addr": addr},
                               response_payload, f"{label} response")

    def _send_rmc_payload(self, key, ctx, payload, label):
        """Fragment and queue any RMC payload - a response or a
        server-initiated request - onto one connection."""
        addr = ctx["addr"]

        def make_chunk(chunk, part_number):
            seq = self.out_seq.get(key, 1)
            self.out_seq[key] = seq + 1
            resp = PrudpPacket()
            resp.src_vport = ctx["src_vport"]
            resp.dst_vport = ctx["dst_vport"]
            resp.ptype = 2  # DATA
            resp.flags = FLAG_RELIABLE | FLAG_NEED_ACK
            resp.session_id = ctx["session_id"]
            resp.signature = self.client_conn_sigs.get(key, 0)
            resp.seq_id = seq
            resp.part_number = part_number
            resp.payload = chunk
            return resp, f"{label} part={part_number} ({len(chunk)} bytes)"

        if len(payload) < MAX_RMC_PAYLOAD_SIZE:
            resp, desc = make_chunk(payload, 0)
            self.queue_reliable(key, resp, addr, desc)
            return

        part_number = 0
        pos = 0
        while pos < len(payload):
            chunk = payload[pos:pos + MAX_RMC_PAYLOAD_SIZE]
            pos += len(chunk)
            is_last = pos >= len(payload)
            part_number = 0 if is_last else part_number + 1
            resp, desc = make_chunk(chunk, part_number)
            self.queue_reliable(key, resp, addr, desc)

    def send_rmc_request(self, addr, payload, label):
        """Push a server-initiated RMC request (a notification) to a connected client.
        -> True if a connection from `addr` is known."""
        keys = [k for k in self.conn_ctx if k[0] == addr]
        if not keys:
            print(f"[{self.label}][{addr}] cannot send {label}: no connection recorded")
            return False
        for key in keys:
            self._send_rmc_payload(key, self.conn_ctx[key], payload, label)
        return True
