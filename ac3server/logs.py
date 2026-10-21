"""The console log: everything printed also goes, time-stamped, to a file in LOG_DIR (prudp/logs/, or AC3_LOG_DIR),
rolled over at log_max_mb and pruned to the newest log_keep files.

Per-packet lines (RECV / SEND / RESEND and payload dumps) are only printed with log_packets on; the RMC call lines
and everything the services say always are.
"""

import os
import sys
import threading
import time

import server_config as config

from ac3server.runtime import SERVER_VERSION


class _Tee:
    """Mirrors writes to multiple streams - used to log everything printed
    to the console into a file too, without touching every print() call.
    Also stamps the start of each line with a wall-clock time - print()
    issues a separate write() for its trailing newline, so naive per-write
    prefixing would double up; line-buffer state avoids that."""
    def __init__(self, *streams):
        self.streams = streams
        self._at_line_start = True

    def write(self, data):
        for chunk in data.splitlines(keepends=True):
            if self._at_line_start and chunk.strip():
                chunk = f"[{time.time():.3f}] {chunk}"
            for s in self.streams:
                s.write(chunk)
            self._at_line_start = chunk.endswith("\n")
        for s in self.streams:
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()


class _RollingLog:
    """The log file: a new <time>.log once the current one passes LOG_MAX_MB,
    and only the newest LOG_KEEP files kept (0 = all)."""
    def __init__(self, folder):
        self.folder = folder
        self.lock = threading.Lock()
        self.fh = None
        self.size = 0
        self._open()

    def _open(self):
        if self.fh:
            self.fh.close()
        path = os.path.join(self.folder, f"{time.strftime('%Y%m%d_%H%M%S')}.log")
        if self.fh and os.path.exists(path):          # rolled over within the same second
            path = path[:-4] + f"_{int(time.time() * 1000) % 1000:03d}.log"
        self.path = path
        self.fh = open(path, "a", encoding="utf-8")
        self.size = 0
        if config.LOG_KEEP > 0:
            logs = sorted(n for n in os.listdir(self.folder) if n.endswith(".log"))
            for name in logs[:-config.LOG_KEEP]:
                try:
                    os.remove(os.path.join(self.folder, name))
                except OSError:
                    pass

    def write(self, data):
        with self.lock:
            self.fh.write(data)
            self.size += len(data)
            if config.LOG_MAX_MB > 0 and self.size > config.LOG_MAX_MB * 1024 * 1024 and data.endswith("\n"):
                self._open()

    def flush(self):
        with self.lock:
            self.fh.flush()


_LOG_FILE = [None]


def setup_file_logging():
    """Every run's console output also goes to a time-stamped file in LOG_DIR (prudp/logs/, or AC3_LOG_DIR). Files roll
    over at log_max_mb and the oldest beyond log_keep are deleted (server_config).

    Called first thing at start-up, before the modules that print while they load (the game settings, the shop
    catalogue), so their lines reach the file too. Only the first call does anything."""
    if _LOG_FILE[0] is not None:
        return
    os.makedirs(config.LOG_DIR, exist_ok=True)
    log_file = _LOG_FILE[0] = _RollingLog(config.LOG_DIR)
    sys.stdout = _Tee(sys.stdout, log_file)
    sys.stderr = _Tee(sys.stderr, log_file)
    print(f"Logging this run to {log_file.path}")
    print(f"ac3-server {SERVER_VERSION}")
    print(config.describe())
