"""Facts about this server process: its ports, the address it advertises, its version, when it started, and the
lock that keeps its threads' work apart.
"""

import os
import threading
import time

import server_config as config


# The game's ports. AC3_AUTH_PORT / AC3_SECURE_PORT move them for a second instance on one machine
# (tests/smoke_test.py); a real client always uses these two.
AUTH_PORT = int(os.environ.get("AC3_AUTH_PORT", 21006))
SECURE_CONNECTION_PORT = int(os.environ.get("AC3_SECURE_PORT", 21007))
# The address the client is told to reach us on. Configure it in
# server_config.json (or AC3_SERVER_ADDRESS) - it has to be this machine's LAN
# address when the game runs on a different machine, otherwise that machine
# tries its own loopback and sees nothing.
LOCAL_IP = config.SERVER_ADDRESS

SERVER_STARTED = time.time()
SERVER_VERSION = os.environ.get("AC3_SERVER_VERSION", "dev")

# The game state (sessions, identities, groups, the send queues of the secure server) is shared by the threads that
# take packets in: the authentication server's, the secure server's and the probe port's. Each holds this lock
# while it handles one packet or runs its periodic checks, so their work never interleaves. The status listener does
# not take it: it hands its work to the secure server's thread instead (status.py).
STATE_LOCK = threading.RLock()
