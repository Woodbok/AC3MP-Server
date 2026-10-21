"""The status listener: how the HTTP process (and so GET /status on port 80) learns that the game server is alive.

An HTTP server on 127.0.0.1:21080 (`status_port`), never reachable from outside:

    GET   /status            players online, sessions, uptime - what GET /status on port 80 reports

The answer is worked out on the SECURE server's thread: the request is queued, the secure loop runs it
(run_status_requests, wired as its on_tick) and the HTTP thread waits up to 3 s. A hung game server therefore shows as
/status not answering.
"""

import collections
import http.server
import json
import threading
import time
import urllib.parse

import server_config as config

from ac3server.gamesession.state import GAME_SESSIONS
from ac3server.notifications import SECURE_SERVER
from ac3server.players.identity import online_pids
from ac3server.runtime import SERVER_STARTED, SERVER_VERSION


_REQUESTS = collections.deque()


def run_status_requests():
    """The secure server's loop: answer what the status listener queued."""
    while _REQUESTS:
        fn, done, out = _REQUESTS.popleft()
        try:
            out.append(fn())
        except Exception as exc:
            out.append({"ok": False, "message": f"The server failed: {exc}"})
        done.set()


def _on_secure_thread(fn, timeout=3.0):
    """Run fn on the secure server's thread and return its answer (inline when that server does not run: the tests)."""
    if SECURE_SERVER[0] is None:
        return fn()
    done, out = threading.Event(), []
    _REQUESTS.append((fn, done, out))
    if not done.wait(timeout):
        return {"ok": False, "message": "The game server is busy."}
    return out[0]


def server_status():
    """What /status reports."""
    return {"ok": True, "service": "ac3-server prudp", "version": SERVER_VERSION,
            "uptime_s": int(time.time() - SERVER_STARTED),
            "players_online": len(online_pids()),
            "sessions": len(GAME_SESSIONS)}


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if urllib.parse.urlparse(self.path).path != "/status":
            self._send(404, {"ok": False, "message": "unknown"})
            return
        body = _on_secure_thread(server_status)
        self._send(200 if body.get("ok") else 503, body)

    def _send(self, status, body):
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):   # a monitor may ask often: not worth a line each
        pass


def start_status_listener(port=None):
    """The status listener, on this machine only. Returns the server (its port is .server_address[1]), or None when
    the port is taken."""
    port = config.STATUS_PORT if port is None else port
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _StatusHandler)
    except OSError as exc:
        print(f"[status] off: 127.0.0.1:{port} - {exc}")
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[status] answering on 127.0.0.1:{srv.server_address[1]} (the HTTP process asks)")
    return srv
