"""The AC3 game server: Quazal Rendez-Vous over PRUDP, the half of Ubisoft's backend the game talks to after its
first HTTP request (that one is onlineconfig/onlineconfig_server.py).

Run it with `python -m ac3server`. The layout, top down:

    app             start-up: the two PRUDP servers, the status listener, NAT detection
    transport       PortServer: one UDP port's connections, reliability, fragments, RMC dispatch
    dispatch        which handler answers which (protocol, method)
    services/       one module per RMC protocol the game calls (authentication, secure, friends, shop, ...)
    gamesession/    matchmaking: the session table, search, groups, invitations (protocol 0x2A)
    players/        who is who: connections, the player directory, friendships
    notifications   server-initiated pushes (protocol 0x0E)
    status          the 127.0.0.1 /status listener the HTTP process asks
    protocol/       the wire: PRUDP packets, RMC envelopes, Quazal data types, the crypto
    gamesettings    the game settings container (CXB) we serve, built at start-up
    storage, logs, redaction, runtime   files, console log, credential hiding, process facts

docs/ARCHITECTURE.md walks through it.
"""

import os
import sys

# server_config.py sits at the repository root and is shared with the HTTP process; the CXB codecs sit in cxb/, beside
# their data.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (_ROOT, os.path.join(_ROOT, "cxb")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
