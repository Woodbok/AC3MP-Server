"""The Abstergo Ladder (Ladder 0x7A, ac3server/services/ladder.py) and its boards (PlayerStatistics 0x6C m3-m6).
Offline, a few seconds.

    python tests/ladder_selftest.py

  * A real client's registerNewGame (m1) parses; the game id is non-zero, unique and never handed out again after a
    restart.
  * A whole match, m1 -> m2 -> m3: incomes, ratings, join status and points; a resent endGame counts once; the values
    at login (1/42, 1/43) and on the board pages (m4 / m5 / m6, with their out-param); the private /
    nobody-scored policies; FFA.

Writes only in temporary folders (AC3_DATA_DIR is set before the server loads).
"""
import contextlib
import io
import os
import shutil
import struct
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "cxb"))

TMP = tempfile.mkdtemp(prefix="ac3_ladder_selftest_")
os.environ["AC3_DATA_DIR"] = TMP

import server_modules  # noqa: E402

ADDR = ("10.9.9.1", 3074)
FAILED = []

# A real client's registerNewGame (a team mode, the host on team 1) - the RMC message after the PRUDP header.
LIVE_M1 = bytes.fromhex("27000000fa8300000001000000010000000200000087d61200100000000010c4e0a61f5643ef8a00000001")
LIVE_M1_TEAM0 = bytes.fromhex("27000000fa8e000000010000000100000002000000b1cb7400100000000010c4e0a61f2144df1c00000000")



def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)




def params_of(ps, message):
    if message[4] & 0x80:
        return ps.parse_rmc_request(message)[5]
    return ps.parse_rmc_response(message)[5]


def reply_params(ps, label_reply):
    ok, _e, _p, _c, _m, params = ps.parse_rmc_response(label_reply[1])
    return params if ok else None




def check_register(ps):
    print("\nregisterNewGame (m1): a real client's request")
    _s, is_req, proto, call, method, params = ps.parse_rmc_request(LIVE_M1)
    check("it is Ladder 0x7A m1", (is_req, proto, method) == (True, 0x7A, 1), f"{proto:#x} m{method}")
    one, pos = ps.read_u32(params, 0)
    kind, pos = ps.read_u32(params, pos)
    pid, pos = ps.read_u32(params, pos)
    length = struct.unpack_from("<H", params, pos)[0]
    blob = params[pos + 2:pos + 2 + length]
    check("[1][type 2 = teams][pid][u16 16][blob{team 1}], nothing over",
          (one, kind, pid, length, pos + 2 + length) == (1, 2, 0x0012D687, 16, len(params))
          and ps.parse_ladder_blob(blob) == [1], params.hex())
    check("our writer makes the same team-1 blob", ps.build_ladder_blob([1]) == blob, blob.hex())
    ids = []
    for _ in range(3):
        ok, _e, proto, _c, method, body = ps.parse_rmc_response(ps.handle_ladder_register_new_game(call, params, ADDR)[1])
        check("answered on 0x7A m1 with one u32", ok and (proto, method, len(body)) == (0x7A, 1, 4), body.hex())
        ids.append(struct.unpack("<I", body)[0])
    check("game ids non-zero and unique", 0 not in ids and len(set(ids)) == 3, str(ids))
    check("each game remembers its host and the host's team",
          all(ps.ladder_state()["games"][str(g)]["host"] == 0x0012D687
              and ps.ladder_state()["games"][str(g)]["teams"] == {str(0x0012D687): 1} for g in ids))
    fresh = server_modules.load()          # a restart: the same data folder, every table empty
    _l, out = fresh.handle_ladder_register_new_game(call, params, ADDR)
    after = struct.unpack("<I", reply_params(fresh, (_l, out)))[0]
    check("after a restart the counter carries on - no id handed out twice", after == max(ids) + 1,
          f"{after} after {ids}")


def m3_request(game_id, rows):
    """rows [(pid, score, team score)] -> endGame's parameters."""
    from ac3server.services.ladder import build_ladder_blob, build_ladder_infos
    return struct.pack("<I", game_id) + build_ladder_infos([(p, s, build_ladder_blob([t])) for p, s, t in rows])


def m1_request(pid, kind, team):
    from ac3server.services.ladder import build_ladder_blob
    blob = build_ladder_blob([team])
    return struct.pack("<IIIH", 1, kind, pid, len(blob)) + blob


def m2_request(game_id, pid, team):
    from ac3server.services.ladder import build_ladder_blob
    blob = build_ladder_blob([team])
    return struct.pack("<IIH", game_id, pid, len(blob)) + blob


def register(ps, host, kind, team):
    return struct.unpack("<I", reply_params(ps, ps.handle_ladder_register_new_game(1, m1_request(host, kind, team),
                                                                                     ADDR)))[0]


def end_game(ps, game_id, rows):
    body = reply_params(ps, ps.handle_ladder_end_game(3, m3_request(game_id, rows), ADDR))
    infos, pos = ps.read_ladder_infos(body, 0)
    assert pos == len(body)
    return {p: (v,) + tuple(ps.parse_ladder_blob(b)) for p, v, b in infos}


def login_values(ps, name, infos=(42, 43)):
    req = struct.pack("<III", 1, 1, len(infos)) + struct.pack("<%dI" % len(infos), *infos) + struct.pack("<I", 1)
    req = bytearray(req)
    ps.write_string(req, name)
    body = reply_params(ps, ps.handle_playerstatistics_read(2, bytes(req), ADDR))
    containers, pos = ps.parse_player_stats_containers(body)
    assert pos == len(body)
    if not containers:
        return {}
    return {i: v1["value"] for i, v1, _op, _v2, _v3 in containers[0][2][0][3]}


def parse_board(ps, body):
    """[list<container>][u32] -> ([(pid, name, [(group, rank, score, {info: value})])], out-param)."""
    containers, pos = ps.parse_player_stats_containers(body)
    out = struct.unpack_from("<I", body, pos)[0]
    assert pos + 4 == len(body), f"{len(body) - pos - 4} byte(s) over"
    rows = []
    for pid, name, stats in containers:
        rows.append((pid, name, [(g, rank, struct.unpack("<f", struct.pack("<I", score))[0],
                                  {i: v1["value"] for i, v1, _o, _a, _b in infos})
                                 for g, rank, score, infos, _w in stats]))
    return rows, out


def check_match(ps):
    print("\nA whole match: m1 -> m2 -> m3 (team mode, two players)")
    ps.DATA_DIR = tempfile.mkdtemp(dir=TMP)
    ps.PLAYER_DIRECTORY.clear()
    host, guest = 1234567, 7654321
    ps.PLAYER_DIRECTORY.update({host: {"pid": host, "name": "altair", "account": "altair"},
                                guest: {"pid": guest, "name": "Ezio", "account": "Ezio"}})
    check("before any match: 1/42 = 100 (Ubisoft's default) and 1/43 = 0 (never placed) at login",
          login_values(ps, "altair") == {} or login_values(ps, "altair") == {42: 100, 43: 0},
          str(login_values(ps, "altair")))
    gid = register(ps, host, 2, 0)
    body = reply_params(ps, ps.handle_ladder_participate(2, m2_request(gid, guest, 1), ADDR))
    check("m2 answers with no parameters", body == b"", body.hex())
    check("the game knows both teams", ps.ladder_state()["games"][str(gid)]["teams"] == {str(host): 0, str(guest): 1})
    res = end_game(ps, gid, [(host, 1200, 0), (guest, 900, 0)])
    check("the winners' side: income 25, rating 1216, joined at the start (B 0), 125 points",
          res[host] == (25, 1216, 0, 125), str(res[host]))
    check("the losers' side: income 0, rating 1184, B 0, points stay 100",
          res[guest] == (0, 1184, 0, 100), str(res[guest]))
    again = end_game(ps, gid, [(host, 1200, 0), (guest, 900, 0)])
    check("the same endGame again: the same answer, counted once", again == res
          and ps.ladder_record(host)["games"] == 1, str(again))
    check("at login: 1/42 = 125 points, 1/43 = rating 1216", login_values(ps, "altair") == {42: 125, 43: 1216},
          str(login_values(ps, "altair")))
    check("and for the other player 100 / 1184", login_values(ps, "Ezio") == {42: 100, 43: 1184},
          str(login_values(ps, "Ezio")))
    stored = ps.ladder_state()["players"][str(host)]
    check("ladder.json holds the record", os.path.isfile(os.path.join(ps.DATA_DIR, "ladder.json"))
          and stored["points"] == 125 and stored["account"] == "altair", str(stored))

    gid = register(ps, guest, 2, 1)
    ps.handle_ladder_participate(2, m2_request(gid, host, 0), ADDR)
    res = end_game(ps, gid, [(host, 500, 0), (guest, 1500, 0)])
    check("a rematch the underdog wins pays more than an even win (Elo): income 27",
          res[guest][0] == 27 and res[guest][3] == 127, str(res[guest]))

    print("\nthe boards (PlayerStatistics m4 / m5 / m6, board 43)")
    m4 = struct.pack("<IIIIII", 1, 43, 1, 20, 0, 0)
    rows, total = parse_board(ps, reply_params(ps, ps.handle_playerstatistics_leaderboard_range(4, m4, ADDR)))
    check("m4 from rank 1: both players, best first, ranks 1 and 2, out-param = the board's size 2",
          [(r[0], r[2][0][1]) for r in rows] == [(guest, 1), (host, 2)] and total == 2, str(rows))
    check("each entry: group 1, the points as the float score, infos 43 = points, 4, 44 = category",
          rows[0][2][0][0] == 1 and rows[0][2][0][2] == 127.0 and rows[0][2][0][3][43] == 127
          and set(rows[0][2][0][3]) == {43, 4, 44}, str(rows[0]))
    m4 = struct.pack("<IIIIII", 1, 43, 2, 30, 0, 0)
    rows, total = parse_board(ps, reply_params(ps, ps.handle_playerstatistics_leaderboard_range(4, m4, ADDR)))
    check("m4 from rank 2: one entry, rank 2", [r[2][0][1] for r in rows] == [2] and total == 2, str(rows))
    m5 = bytearray(struct.pack("<II", 1, 43))
    ps.write_string(m5, "altair")
    m5 += struct.pack("<III", 1, 0, 0)
    rows, total = parse_board(ps, reply_params(ps, ps.handle_playerstatistics_leaderboard_for_player(5, bytes(m5),
                                                                                                    ADDR)))
    check("m5 around altair, 1 wanted: altair alone, rank 2", [(r[1], r[2][0][1]) for r in rows] == [("altair", 2)],
          str(rows))
    m5[-12:-8] = struct.pack("<I", 21)
    rows, total = parse_board(ps, reply_params(ps, ps.handle_playerstatistics_leaderboard_for_player(5, bytes(m5),
                                                                                                    ADDR)))
    check("m5 around altair, 21 wanted: the whole board of two", len(rows) == 2 and total == 2, str(rows))
    m6 = bytearray(struct.pack("<IIIII", 2, 1, 43, 1, 43) + struct.pack("<I", 3))
    for n in ("Ezio", "altair", "nobody"):
        ps.write_string(m6, n)
    pairs, names, left = ps.parse_board_names_request(bytes(m6))
    check("m6's request: a list of (group, stat) pairs, then the names", (pairs, names, left) ==
          ([(1, 43), (1, 43)], ["Ezio", "altair", "nobody"], 0), f"{pairs} {names} {left}")
    rows, total = parse_board(ps, reply_params(ps, ps.handle_playerstatistics_leaderboard_by_names(6, bytes(m6), ADDR)))
    check("m6 answers the two known names, a stat block per pair, ranks from the whole board",
          [(r[1], [s[1] for s in r[2]]) for r in rows] == [("Ezio", [1, 1]), ("altair", [2, 2])] and total == 2,
          str(rows))


def check_policies(ps):
    print("\npolicies")
    ps.DATA_DIR = tempfile.mkdtemp(dir=TMP)
    ps.PLAYER_DIRECTORY.clear()
    a, b, c = 501, 502, 503
    for pid in (a, b, c):
        ps.PLAYER_DIRECTORY[pid] = {"pid": pid, "name": f"p{pid}", "account": f"p{pid}"}
    sid = 0x7F000001
    ps.GAME_SESSIONS[sid] = {"type_id": 1, "participants": [a, b], "private_participants": [a, b], "host_pid": a,
                             "attributes": {ps.ATTR_PLAYLIST_ID: 8, ps.ATTR_GAME_MODE: 1}, "created": time.time()}
    try:
        gid = register(ps, a, 2, 0)
        game = ps.ladder_state()["games"][str(gid)]
        check("m1 from the host of a private-playlist session: playlist 8, private", game["playlist"] == 8
              and game["private"] is True, str(game))
        ps.handle_ladder_participate(2, m2_request(gid, b, 1), ADDR)
        before = ps.LADDER_PRIVATE
        ps.LADDER_PRIVATE = "skip"
        res = end_game(ps, gid, [(a, 900, 0), (b, 100, 0)])
        check("ladder_private skip: nothing earned, nothing moved", res[a] == (0, 1200, 0, 100)
              and not ps.has_ladder_record(a), str(res))
        ps.LADDER_PRIVATE = before
        gid = register(ps, a, 2, 0)
        ps.handle_ladder_participate(2, m2_request(gid, b, 1), ADDR)
        res = end_game(ps, gid, [(a, 900, 0), (b, 100, 0)])
        check("ladder_private count (the default): the private match counts", res[a] == (25, 1216, 0, 125), str(res))
    finally:
        ps.GAME_SESSIONS.pop(sid, None)
    gid = register(ps, c, 1, 0)
    res = end_game(ps, gid, [(c, 500, 0)])
    check("one player alone counts (no minimum player count) but has nobody to beat: income 0, rating 1200",
          res[c] == (0, 1200, 0, 100) and ps.ladder_record(c)["games"] == 1, str(res))
    gid = register(ps, a, 2, 0)
    res = end_game(ps, gid, [(a, 0, 0), (b, 0, 0)])
    check("nobody scored: nothing earned", res[a][0] == 0 and res[b][0] == 0
          and ps.ladder_record(a)["games"] == 1, str(res))

    print("\nFFA (type 1): every pair compared")
    ps.DATA_DIR = tempfile.mkdtemp(dir=TMP)
    gid = register(ps, a, 1, 0)
    ps.handle_ladder_participate(2, m2_request(gid, b, 0), ADDR)
    res = end_game(ps, gid, [(a, 3000, 0), (b, 2000, 0), (c, 1000, 0)])
    check("first of three even players: income 25, rating 1216", res[a][:2] == (25, 1216), str(res[a]))
    check("second: income 0, rating unchanged", res[b][:2] == (0, 1200), str(res[b]))
    check("third: income 0, rating 1184", res[c][:2] == (0, 1184), str(res[c]))
    check("the one the game never registered is B 1 (joined in progress), the others B 0",
          (res[a][2], res[b][2], res[c][2]) == (0, 0, 1), str(res))
    before = ps.LADDER
    ps.LADDER = False
    body = reply_params(ps, ps.handle_ladder_end_game(3, m3_request(gid, [(a, 1, 0)]), ADDR))
    ps.LADDER = before
    check("ladder false (the escape hatch): endGame answered with the empty list, nothing stored",
          body == struct.pack("<I", 0) and ps.ladder_record(a)["games"] == 1, body.hex())
    res = end_game(ps, 999999, [(a, 10, 0), (b, 20, 0)])
    check("an endGame for a game this server never registered still counts (as FFA), both B 1",
          res[b][0] > 0 and res[a][2] == 1 and res[b][2] == 1, str(res))




def main():
    ps = server_modules.load()
    check("AC3_DATA_DIR is the temporary one", os.path.abspath(ps.DATA_DIR).startswith(os.path.abspath(TMP)), ps.DATA_DIR)
    check_register(ps)
    check_match(ps)
    check_policies(ps)
    print()
    if FAILED:
        print(f"{len(FAILED)} check(s) FAILED:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
