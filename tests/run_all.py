"""Run every offline self-test, in order, and say whether all passed. Run it before every deploy.

    python tests/run_all.py

  ac3_transport_selftest.py  the PRUDP send path against a recording socket
  ac3_session_selftest.py    matchmaking, groups, invitations, friends and presence with simulated players
  natdetect_selftest.py      the NAT detection service against a model of the game's own detection steps
  shop_selftest.py           the bought list and free_ability_sets
  reconnect_selftest.py      the presence reset at a re-login, forgotten at the last disconnect
  onlinesave_selftest.py     the profile's shrink guard (a much smaller OnlineSave upload never replaces a full one)
  map_names_selftest.py      every map id the served playlists can hold has its in-game name
  storage_selftest.py        a damaged state file is moved aside, never overwritten (every store)
  ladder_selftest.py         the Abstergo Ladder: the Ladder frames and blobs, a whole m1 -> m2 -> m3 match, the
                             private / nobody-scored policies, the board pages m4 / m5 / m6
  smoke_test.py              starts both server processes on free local ports and talks to them (/status,
                             OnlineConfig, a PRUDP handshake, garbage packets, NAT detection); runs beside a live server

Each test uses temporary folders for what it writes: nothing in prudp/data is written. Exit code 0 = all passed.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = ["ac3_transport_selftest.py", "ac3_session_selftest.py", "natdetect_selftest.py", "shop_selftest.py",
         "reconnect_selftest.py", "onlinesave_selftest.py", "map_names_selftest.py", "storage_selftest.py",
         "ladder_selftest.py", "smoke_test.py"]


def main():
    failed = []
    for name in TESTS:
        start = time.time()
        r = subprocess.run([sys.executable, os.path.join(HERE, name)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        passed = r.returncode == 0 and "all checks passed" in r.stdout
        print(f"{'PASS' if passed else 'FAIL'}  {name}  ({time.time() - start:.0f} s)")
        if not passed:
            failed.append(name)
            tail = (r.stdout + r.stderr).strip().splitlines()[-25:]
            print("      " + "\n      ".join(tail))
    print("\nall tests passed" if not failed else f"\n{len(failed)} failed: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
