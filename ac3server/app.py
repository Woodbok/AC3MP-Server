"""Start-up: the game server's listeners, and the thread each one runs on.

    UDP 21006   the authentication server (PortServer "auth", the main thread): Login / LoginEx / RequestTicket
    UDP 21007   the secure server (PortServer "secure", its own thread): every game service after login
    TCP 21080   the status listener, 127.0.0.1 only - the HTTP process asks it (status)
    UDP 11000+  NAT type detection, only with `nat_detect` "own" (natdetect)

The OnlineConfig document also advertises 21005 (SandboxUrlWS) and 21011 (SandboxUrlPriv): the game sends nothing
to either, so nothing listens there.
"""

import threading

import server_config as config
from ac3server import natdetect

from ac3server.connections import player_disconnected
from ac3server.dispatch import AUTH_DISPATCH, SECURE_DISPATCH
from ac3server.logs import setup_file_logging
from ac3server.notifications import SECURE_SERVER
from ac3server.runtime import AUTH_PORT, SECURE_CONNECTION_PORT
from ac3server.status import run_status_requests, start_status_listener
from ac3server.transport import PortServer


def start_nat_detect():
    """Our answer to the game's NAT type detection (server_config nat_detect "own"; ac3server/natdetect.py)."""
    if config.NAT_DETECT != "own":
        return None
    try:
        return natdetect.Detector("0.0.0.0", config.NAT_DETECT_PORT, config.NAT_DETECT_ADDRESS2).start()
    except OSError as exc:
        print(f"[natdetect] off: UDP {config.NAT_DETECT_PORT}-{config.NAT_DETECT_PORT + 2} - {exc}")
    return None


def main():
    setup_file_logging()

    auth_server = PortServer(AUTH_PORT, AUTH_DISPATCH, "auth")
    # The secure server owns the game state: it hears of connections ending, and answers the status listener's
    # questions on its own thread (status.py), under runtime.STATE_LOCK like every packet.
    secure_server = PortServer(SECURE_CONNECTION_PORT, SECURE_DISPATCH, "secure",
                               on_disconnect=player_disconnected, on_tick=run_status_requests)
    # Handlers are plain functions with no reference to the server, but a
    # notification has to be pushed to a client *other* than the one whose
    # call is being handled. This is the only way back to the transport.
    SECURE_SERVER[0] = secure_server

    threading.Thread(target=secure_server.run, daemon=True).start()
    start_status_listener()
    start_nat_detect()
    auth_server.run()
