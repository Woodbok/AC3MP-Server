"""End-to-end smoke test: start BOTH server processes the way a host runs them, and talk to them over the network.

    python tests/smoke_test.py

Everything is on 127.0.0.1 and on free ports picked at run time (AC3_HTTP_PORT, AC3_AUTH_PORT, AC3_SECURE_PORT,
status_port in a temporary config), so it runs beside a server that is already up. Logs and data go to a temporary
folder (AC3_LOG_DIR, AC3_DATA_DIR); a SYN / CONNECT handshake writes no player state, so prudp/data is only read.

Checks: /status answers 503 while only the HTTP side runs and 200 once the game server is up; the OnlineConfig reply is
served; a SYN to each PRUDP port gets its SYN-ACK; garbage does not stop the listener; the run's log file exists and
carries the packet lines; the NAT detect service answers; /status goes back to 503 when the game server stops. Exit code 0 = all passed.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def free_port(kind):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def free_udp_run(n):
    """The first of n consecutive free UDP ports."""
    for base in range(42000, 60000, 11):
        socks = []
        try:
            for p in range(base, base + n):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                socks.append(s)
                s.bind(("0.0.0.0", p))
            return base
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
    raise SystemExit("no free UDP ports")


def get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except OSError as e:
        return None, str(e).encode()


def wait_for(fn, seconds):
    end = time.time() + seconds
    while time.time() < end:
        out = fn()
        if out:
            return out
        time.sleep(0.25)
    return None


def syn(port):
    """Send a client SYN; return the server's reply parsed, or None."""
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):      # importing it builds the game settings, loudly
        from ac3server.protocol import prudp as ps
    pkt = ps.PrudpPacket()
    pkt.src_vport = ps.VPort(stream_type=3, port=15)
    pkt.dst_vport = ps.VPort(stream_type=3, port=1)
    pkt.ptype = 0
    pkt.flags = ps.FLAG_NEED_ACK
    pkt.session_id = 0
    pkt.signature = 0
    pkt.seq_id = 0
    pkt.connection_signature = 0x1234
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        s.sendto(pkt.build(), ("127.0.0.1", port))
        data, _ = s.recvfrom(4096)
        return ps.PrudpPacket.parse(data)
    except OSError:
        return None
    finally:
        s.close()


def main():
    tmp = tempfile.mkdtemp(prefix="ac3_smoke_")
    http_port, status_port = free_port(socket.SOCK_STREAM), free_port(socket.SOCK_STREAM)
    auth_port, secure_port = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
    detect_port = free_udp_run(3)
    config = os.path.join(tmp, "server_config.json")
    with open(config, "w", encoding="utf-8") as fh:
        json.dump({"address": "127.0.0.1", "status_port": status_port,
                   "log_packets": True, "log_max_mb": 5, "log_keep": 3,
                   "nat_detect": "own", "nat_detect_port": detect_port}, fh)
    env = dict(os.environ, AC3_CONFIG=config, AC3_LOG_DIR=os.path.join(tmp, "logs"),
               AC3_DATA_DIR=os.path.join(tmp, "data"), AC3_HTTP_PORT=str(http_port),
               AC3_AUTH_PORT=str(auth_port), AC3_SECURE_PORT=str(secure_port),
               AC3_SERVER_VERSION="smoke", PYTHONUNBUFFERED="1")
    os.makedirs(env["AC3_DATA_DIR"])
    print(f"temporary folder {tmp}; http {http_port}, status {status_port}, auth {auth_port}, secure {secure_port}")
    base = f"http://127.0.0.1:{http_port}"
    procs = []

    def start(args, name):
        log = open(os.path.join(tmp, f"{name}.out"), "w", encoding="utf-8")
        p = subprocess.Popen([sys.executable] + args, cwd=ROOT, env=env,
                             stdout=log, stderr=subprocess.STDOUT)
        procs.append(p)
        return p

    try:
        print("\nthe HTTP side alone")
        start([os.path.join(ROOT, "onlineconfig", "onlineconfig_server.py")], "http")
        status = wait_for(lambda: (lambda r: r if r[0] else None)(get(base + "/status")), 30)
        check("the HTTP service answers", status is not None)
        if status:
            body = json.loads(status[1] or b"{}")
            check("/status is 503 while the game server is down", status[0] == 503 and body.get("prudp", {}).get("ok") is False,
                  f"{status[0]} {body}")
        code, data = get(base + "/OnlineConfigService.svc/GetOnlineConfig")
        try:
            names = {e["Name"] for e in json.loads(data)}
        except (ValueError, TypeError, KeyError):
            names = set()
        check("the OnlineConfig reply is served", code == 200 and "SandboxUrl" in names, f"{code} {data[:120]!r}")
        site_code, page = get(base + "/")
        check("/ says what this is and the address to patch in", site_code == 200 and b"--host 127.0.0.1" in page,
              f"{site_code} {page[:120]!r}")
        urls = next((e["Values"] for e in json.loads(data) if e["Name"] == "punch_DetectUrls"), []) if names else []
        check("nat_detect own: the game is pointed at our detect servers",
              urls == [f"127.0.0.1:{detect_port}", f"127.0.0.1:{detect_port + 2}"], str(urls))

        print("\nboth processes")
        prudp = start(["-m", "ac3server"], "prudp")
        up = wait_for(lambda: (lambda r: r if r[0] == 200 else None)(get(base + "/status")), 60)
        check("/status is 200 once the game server is up", up is not None)
        if up:
            body = json.loads(up[1])
            check("it reports the game server's state", body["prudp"].get("service") == "ac3-server prudp"
                  and "players_online" in body["prudp"] and body.get("version") == "smoke", str(body))
        for label, port in (("auth", auth_port), ("secure", secure_port)):
            reply = syn(port)
            check(f"a SYN to the {label} port gets its SYN-ACK", reply is not None and reply.ptype == 0
                  and reply.flags & 1 and reply.connection_signature != 0, repr(reply))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for junk in (b"", b"\x00", os.urandom(40), b"\xff" * 300):
            s.sendto(junk, ("127.0.0.1", auth_port))
        s.close()
        time.sleep(0.5)
        check("garbage does not stop the listener", syn(auth_port) is not None and prudp.poll() is None)
        d = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        d.settimeout(3)
        d.sendto(bytes.fromhex("000d00010500") + bytes(7), ("127.0.0.1", detect_port))
        try:
            data, src = d.recvfrom(64)
        except OSError:
            data, src = b"", None
        d.close()
        check("the NAT detect service answers in the 19-byte form", len(data) == 19 and data[:6] == bytes.fromhex(
            "001300010502") and src == ("127.0.0.1", detect_port), f"{data.hex()} from {src}")
        logs = os.listdir(env["AC3_LOG_DIR"]) if os.path.isdir(env["AC3_LOG_DIR"]) else []
        text = ""
        for name in logs:
            with open(os.path.join(env["AC3_LOG_DIR"], name), encoding="utf-8", errors="replace") as fh:
                text += fh.read()
        check("the run's log file is written to AC3_LOG_DIR", len(logs) == 1, str(logs))
        check("the log carries the version, the settings and the packet lines",
              "ac3-server smoke" in text and "[config] logging:" in text and "RECV" in text)

        print("\nthe game server stops")
        prudp.terminate()
        prudp.wait(10)
        down = wait_for(lambda: (lambda r: r if r[0] == 503 else None)(get(base + "/status")), 15)
        check("/status goes back to 503", down is not None)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
    if FAILED:
        print(f"\n{len(FAILED)} check(s) failed; the processes' output is in {tmp}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
