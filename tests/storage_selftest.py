"""A damaged state file is moved aside, never overwritten: every store, at start-up and while running. Offline, a few
seconds.

    python tests/storage_selftest.py

Writes only in a temporary folder (AC3_DATA_DIR is set before anything loads).
"""
import glob
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

TMP = tempfile.mkdtemp(prefix="ac3_storage_selftest_")
os.environ["AC3_DATA_DIR"] = TMP

import server_modules  # noqa: E402

FAILED = []
BROKEN = '{"this is": "a hand edit with a typo"'


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def path(name):
    return os.path.join(TMP, name)


def damage(name):
    """Write a broken file, with a modification time the stores cannot have seen."""
    p = path(name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(BROKEN)
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def asides(name):
    return sorted(glob.glob(path(name) + ".unreadable-*"))


def kept_intact(name):
    """Exactly one copy moved aside, holding the broken bytes unchanged."""
    found = asides(name)
    return len(found) == 1 and open(found[0], encoding="utf-8").read() == BROKEN


def reads(name):
    try:
        with open(path(name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def reset(name):
    for p in asides(name) + [path(name)]:
        if os.path.exists(p):
            os.remove(p)


def check_directory(ps):
    print("\nplayers.json (the player directory)")
    damage("players.json")
    ps.remember_player({"pid": 1001, "name": "Alice", "account": "alice"})
    check("broken at start-up: moved aside intact", kept_intact("players.json"))
    check("and a new roster started beside it", [p["pid"] for p in reads("players.json") or []] == [1001])
    reset("players.json")
    ps.PLAYER_DIRECTORY.clear()
    ps.remember_player({"pid": 1001, "name": "Alice", "account": "alice"})
    ps.remember_player({"pid": 1002, "name": "Bob", "account": "bob"})
    damage("players.json")
    check("broken while running: the roster in memory is kept", ps.find_player(pid=1002) is not None)
    check("the broken file moved aside intact", kept_intact("players.json"))
    check("and the roster written back", sorted(p["pid"] for p in reads("players.json") or []) == [1001, 1002])


def check_friendships(ps):
    print("\nfriends.json")
    damage("friends.json")
    ps.add_friendship(1001, 1002)
    check("broken at start-up: moved aside intact", kept_intact("friends.json"))
    check("and the new friendship saved beside it", reads("friends.json") == {"1001": [1002], "1002": [1001]})
    damage("friends.json")
    ps.add_friendship(1001, 1003)
    check("broken again in the same second: both broken copies kept, neither overwritten",
          len(asides("friends.json")) == 2 and all(open(f, encoding="utf-8").read() == BROKEN
                                                    for f in asides("friends.json")))
    for f in asides("friends.json")[1:]:
        os.remove(f)
    check("broken while running: moved aside intact", kept_intact("friends.json"))
    check("the friendships in memory kept and written back",
          reads("friends.json") == {"1001": [1002, 1003], "1002": [1001], "1003": [1001]}, str(reads("friends.json")))


def check_player_files(ps):
    print("\nA player's balance, purchases and career")
    addr = ("10.0.0.5", 3074)
    ps.PLAYER_IDENTITIES[addr] = {"account": "alice", "name": "Alice", "pid": 1001}
    damage("currency_alice.json")
    balance = ps._currency_get(addr)
    check("balance: the configured default served", balance == list(ps.STARTING_CURRENCY), str(balance))
    check("balance: the broken file moved aside intact", kept_intact("currency_alice.json"))
    ps._currency_save(addr, [0, 100])
    check("balance: the next save does not touch it", kept_intact("currency_alice.json") and
          reads("currency_alice.json") == [0, 100])
    damage("purchases_alice.json")
    check("purchases: none served", ps._purchases_get(addr) == [])
    check("purchases: the broken file moved aside intact", kept_intact("purchases_alice.json"))
    damage("stats_alice.json")
    check("career: none served", ps._stats_load("alice") == {})
    check("career: the broken file moved aside intact", kept_intact("stats_alice.json"))




def check_address():
    print("\nThe advertised address check (server_config.address_problem)")
    import server_config
    check("loopback is on this machine", server_config.is_local_address("127.0.0.1"))
    check("a documentation address is not", not server_config.is_local_address("203.0.113.77"))
    lan = server_config._detect_lan_address()
    check("this machine's own outward address is found by binding to it, whatever the host name resolves to",
          lan == "127.0.0.1" or server_config.is_local_address(lan), lan)




def main():
    check_address()
    ps = server_modules.load()
    check("the server's data folder is the temporary one", os.path.abspath(ps.DATA_DIR) == os.path.abspath(TMP))
    check_directory(ps)
    check_friendships(ps)
    check_player_files(ps)
    print("")
    if FAILED:
        print(f"{len(FAILED)} check(s) FAILED:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
