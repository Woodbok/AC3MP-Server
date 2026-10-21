"""The reconnect after a sleep: a game that logs in again from the same address takes its identity over and
starts with no presence, a presence never outlives the player's last connection, and the same name from another
address stays a separate identity ("name#2"). Offline, a few seconds.

    python tests/reconnect_selftest.py

Writes only in a temporary folder (AC3_DATA_DIR is set before the server loads).
"""
import contextlib
import io
import os
import struct
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "cxb"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

TMP = tempfile.mkdtemp(prefix="ac3_reconnect_selftest_")
os.environ["AC3_DATA_DIR"] = TMP

import server_modules  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args)


def custom_data(ps, username, password=None):
    """A patched client's login data: the username in all three slots."""
    password = username if password is None else password
    body = bytearray()
    for s in (username, username, password):
        ps.write_string(body, s)
    out = bytearray()
    ps.write_string(out, ps.LOGIN_CUSTOM_DATA_CLASS)
    out += struct.pack("<II", len(body) + 4, len(body))
    return bytes(out + body)


def login(ps, addr, name):
    """LoginEx then RegisterEx from one address, as the game does on every (re)connection."""
    params = bytearray()
    ps.write_string(params, "DummyUbiUser@quazal.com")
    params += custom_data(ps, name)
    _l, resp = quiet(ps.handle_loginex, 7, bytes(params), addr)
    ok1 = ps.parse_rmc_response(resp)[0]
    params = bytearray(struct.pack("<I", 1))
    ps.write_string(params, "prudp:/address=10.0.0.9;port=3074;sid=15;type=2")
    params += custom_data(ps, name)
    _l, resp = quiet(ps.handle_registerex, 8, bytes(params), addr)
    return ok1 and ps.parse_rmc_response(resp)[0]


def set_presence(ps, addr, presence_id=0x1234, blob=b"01000000040000000100000001000000"):
    params = struct.pack("<I", presence_id) + struct.pack("<H", len(blob)) + blob
    quiet(ps.handle_richpresence_set, 1, params, addr)


def get_presence(ps, addr, pids):
    params = struct.pack("<I", len(pids)) + b"".join(struct.pack("<I", p) for p in pids)
    _l, resp = quiet(ps.handle_richpresence_get, 2, params, addr)
    rparams = ps.parse_rmc_response(resp)[5]
    count = struct.unpack_from("<I", rparams, 0)[0]
    out, pos = [], 4
    for _ in range(count):
        pid, online, _sid, length = struct.unpack_from("<IBIH", rparams, pos)
        pos += 11 + length
        out.append(pid)
    return out


def main():
    ps = server_modules.load()
    check("AC3_DATA_DIR is the temporary one", os.path.abspath(ps.DATA_DIR).startswith(os.path.abspath(TMP)), ps.DATA_DIR)
    pid = {n: ps.stable_user_pid(n) for n in ("Alice", "Bob")}
    a, b, b2 = ("10.0.1.1", 3074), ("10.0.1.2", 3074), ("10.0.1.2", 3075)

    print("\nthe presence across a re-login")
    check("Alice and Bob log in", login(ps, a, "Alice") and login(ps, b, "Bob"))
    check("each is the pid of their name", ps.CONNECTED_PIDS.get(a) == pid["Alice"] and ps.CONNECTED_PIDS.get(b) == pid["Bob"])
    set_presence(ps, a)
    set_presence(ps, b)
    check("both set a presence: Alice sees Bob online", get_presence(ps, a, [pid["Bob"]]) == [pid["Bob"]])
    # a game woken from sleep comes back in limited mode and logs in again on a new port: its old connection has not
    # timed out yet, and the game never sets a presence
    check("Bob logs in again from another port", login(ps, b2, "Bob"))
    check("the same address: he takes his identity over - the same pid, not Bob#2, and the old port is no one",
          ps.CONNECTED_PIDS.get(b2) == pid["Bob"] and ps.PLAYER_IDENTITIES[b2]["name"] == "Bob"
          and b not in ps.PLAYER_IDENTITIES, str(ps.PLAYER_IDENTITIES))
    check("RegisterEx dropped his pre-sleep presence (not handed out as online any more)",
          get_presence(ps, a, [pid["Bob"]]) == [] and pid["Bob"] not in ps.PRESENCE)
    check("the old connection timing out leaves the new one alone",
          quiet(ps.player_disconnected, b) is None and ps.REGISTERED_AT.get(pid["Bob"]) is not None
          and pid["Bob"] in ps.online_pids())
    set_presence(ps, b2)
    check("once his game sets a presence again, he is online", get_presence(ps, a, [pid["Bob"]]) == [pid["Bob"]])

    print("\nthe same name from another address")
    c = ("10.0.9.9", 3074)
    check("a second machine logs in as Bob", login(ps, c, "Bob"))
    check("it is Bob#2, with its own pid: both stay connected",
          ps.PLAYER_IDENTITIES[c]["name"] == "Bob#2" and ps.CONNECTED_PIDS.get(c) != pid["Bob"]
          and ps.CONNECTED_PIDS.get(b2) == pid["Bob"], str(ps.PLAYER_IDENTITIES))
    quiet(ps.player_disconnected, c)

    print("\nthe last connection ends")
    quiet(ps.player_disconnected, b2)
    check("Bob's presence and timestamps are forgotten",
          pid["Bob"] not in ps.PRESENCE and pid["Bob"] not in ps.PRESENCE_AT and pid["Bob"] not in ps.REGISTERED_AT)
    check("and he is not handed out as online", get_presence(ps, a, [pid["Bob"]]) == [])

    print("\nall checks passed" if not FAILED else f"\n{len(FAILED)} FAILED: {', '.join(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
