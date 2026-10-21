"""PlayerStatistics (protocol 0x6C): each player's career - XP and every other number a match adds to.

    m2 SendPlayerStats              one match's numbers, sent by the game at the end of it   -> nothing
    m3 ReadPlayerStats              a player's career, asked for at every login              -> the stored totals
    m4 ReadStatsLeaderboardByRange           a page of a board, from a rank     -> [list<m3 container>][u32 total]
    m5 ReadStatsLeaderboardByRangeForPlayer  the board around a player          -> the same
    m6 ReadStatsLeaderboardByPlayerNames     named players (friends) on a board -> the same

The boards: the game asks only for board 43, the Abstergo Ladder (services/ladder.py keeps it), whose stored values
are also served at login as group 1 info 42 (ladder points) and 43 (the rating, 0 = never placed).

XP is group 1 info 1; the level is worked out by the game from it (the XP table is in the exe), and the game reads
its career only at login. Group 9 is the emblem screen (avatar, emblem symbol and pattern and their colours), stored
the same way. A career is DATA_DIR/stats_<account>.json, and every raw report is also kept in stats_history/ so a
wrong folding rule can be replayed into a right one.

The records, from acb-rdv's HermesPlayerStatisticsService and checked against AC3's own bytes (the game's m2 reports
parse with nothing left over, and a full m3 reply from Ubisoft's server re-encodes byte for byte):

    value V   [u32 int][u64][u64][string][u8 type]       type 1 = the int, 4 = a double in the 2nd u64, 5 = the string
    m2 info   [u8 infoId][V][u8 op]
    m2 body   [u32 n] { [u32 statId][u32 n] { m2 info } }
    m3 info   [u8 infoId][V][u8 op][V][V]
    m3 stat   [u32 statId][u32 rank][f32 score][u32 n] { m3 info } [u64 datetime]   rank and score 0 at login
    m3 body   [u32 n] { [u32 pid][string name][u32 n] { m3 stat } }

The board replies (m4/m5/m6) are the m3 body plus a u32: the client's reader FUN_0117C730 reads the list with m3's
own reader FUN_0117C580 and then out-param 1 (PlayerStatistics' reply dispatcher FUN_0117C7D0); the declared lengths
of Ubisoft's m6 replies leave exactly 4 bytes after the list. Each board entry's stat is [group 1][rank][f32 the
board value] and three infos: 43 = the value (op byte 0), 4 = the player's stat 1/4 (op 1), 44 = a category 0-5
(op 2), as in Ubisoft's own entries.

The `op` byte of each reported value is the folding rule, one fixed rule per stat, sent by the game:
0 = add (XP is op 0: a match reports the XP it shows as earned), 2 = replace (group 9's packed values),
3 = keep the larger (the "best"/"max" stats - inferred). An op never seen replaces, and is logged: replacing loses at
most one match, adding could corrupt a value for good. `stats_accumulate` false serves the last report instead.
"""

import json
import os
import struct
import time

import server_config as config
from ac3server import storage

from ac3server.players.directory import PLAYER_DIRECTORY, refresh_player_directory
from ac3server.players.identity import contact_safe_name, onlinesave_user
from ac3server.services.ladder import has_ladder_record, ladder_board, ladder_record, served_rating
from ac3server.protocol.datatypes import q_datetime_now, read_string, read_string_list, read_u32, write_string
from ac3server.protocol.rmc import build_rmc_response
from ac3server.storage import atomic_write, prune_history, set_aside


def handle_playerstatistics_read(call_id, params, addr):     # m3 ReadPlayerStats
    """[u32 n] { [u32 groupId][list<u32> statIds] } [list<string> names]: the stored values of exactly the stats asked
    for, in the order asked - the reply echoes the request's groups and ids."""
    names, queries = [], []
    try:
        pos = 0
        group_count = struct.unpack_from('<I', params, pos)[0]
        pos += 4
        for _ in range(group_count):
            group_id = struct.unpack_from('<I', params, pos)[0]
            pos += 4
            stat_count = struct.unpack_from('<I', params, pos)[0]
            pos += 4
            stat_ids = list(struct.unpack_from('<%dI' % stat_count, params, pos))
            pos += 4 * stat_count
            queries.append((group_id, stat_ids))
        name_count = struct.unpack_from('<I', params, pos)[0]
        pos += 4
        for _ in range(name_count):
            name, pos = read_string(params, pos)
            names.append(name)
    except Exception as e:
        print(f"[{addr}]   PlayerStatistics.3: could not parse request ({e}) - "
              f"answering with an empty list anyway")
        queries, names = [], []
    body, described = _player_stats_containers(names, queries)
    if described:
        print(f"[{addr}]   PlayerStatistics.ReadPlayerStats for {names}: "
              + "; ".join(described))
    else:
        print(f"[{addr}]   PlayerStatistics.ReadPlayerStats for {names or '?'}: "
              f"nothing stored for any of them -> empty list")
    return "PlayerStatistics.ReadPlayerStats", \
        build_rmc_response(0x6C, call_id, 3, body)


STATS_MEM = {}          # username -> {(statId, infoId): record}
STATS_MTIME = {}        # username -> the mtime that cache was read from
STATS_TYPE_INT = 1
STATS_OP_ADD = 0
STATS_OP_SET = 2
STATS_OP_MAX = 3
STATS_KNOWN_OPS = (STATS_OP_ADD, STATS_OP_SET, STATS_OP_MAX)


def _stats_path(user):
    return os.path.join(storage.DATA_DIR, f"stats_{user}.json")


def _stats_load(user):
    """Cached, but the cache is dropped when the file changes underneath it.

    The store is plain JSON on purpose - it is meant to be editable by
    hand or by a tool. Holding a stale
    copy in memory would silently undo somebody's edit at the next save,
    so the modification time is part of the cache key.
    """
    path = _stats_path(user)
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        stamp = None
    cached = STATS_MEM.get(user)
    if cached is not None and STATS_MTIME.get(user) == stamp:
        return cached
    stats = {}
    if stamp is not None:
        try:
            with open(path, encoding="utf-8") as fh:
                for row in json.load(fh):
                    stats[(row["group"], row["info"])] = row
        except (OSError, ValueError, KeyError, TypeError) as e:
            stats = {}
            set_aside(path, e)
            stamp = None
        if cached is not None:
            print(f"      (stats for '{user}' were edited on disk - reloaded)")
    STATS_MEM[user] = stats
    STATS_MTIME[user] = stamp
    return stats


def _stats_save(user, stats):
    rows = sorted(stats.values(), key=lambda r: (r["group"], r["info"]))
    try:
        os.makedirs(storage.DATA_DIR, exist_ok=True)
        path = _stats_path(user)
        atomic_write(path, json.dumps(rows, indent=1))
        try:
            STATS_MTIME[user] = os.path.getmtime(path)
        except OSError:
            STATS_MTIME.pop(user, None)
        return True
    except OSError as e:
        print(f"      (stats for '{user}' kept in memory only: {e})")
        return False


def _read_stat_value(params, pos):
    """V = [u32 int][u64][u64][string][u8 type]."""
    number, pos = read_u32(params, pos)
    a, b = struct.unpack_from('<QQ', params, pos)
    pos += 16
    text, pos = read_string(params, pos)
    vtype = params[pos]
    return {"value": number, "a": a, "b": b, "text": text, "type": vtype}, pos + 1


def _write_stat_value(buf, v):
    buf += struct.pack('<I', v["value"] & 0xFFFFFFFF)
    buf += struct.pack('<QQ', v["a"], v["b"])
    write_string(buf, v["text"])
    buf.append(v["type"])


def parse_player_stats_report(params):
    """method 2: [u32 n] { [u32 statId][u32 n] { [u8 infoId][V][u8 op] } }.

    Returns (groups, bytes_consumed) where groups is
    [(statId, [(infoId, value, op), ...]), ...].
    """
    pos = 0
    group_count, pos = read_u32(params, pos)
    groups = []
    for _ in range(group_count):
        group_id, pos = read_u32(params, pos)
        info_count, pos = read_u32(params, pos)
        infos = []
        for _ in range(info_count):
            info_id = params[pos]
            pos += 1
            value, pos = _read_stat_value(params, pos)
            op = params[pos]
            pos += 1
            infos.append((info_id, value, op))
        groups.append((group_id, infos))
    return groups, pos


def parse_player_stats_containers(params):
    """method 3's reply, the mirror of `_player_stats_containers`.

    Only the tests need this. A full reply from Ubisoft's server parsed with
    it and re-encoded with `build_player_stats_containers` comes back
    identical, which is what makes the layout evidence rather than a reading.
    """
    pos = 0
    count, pos = read_u32(params, pos)
    containers = []
    for _ in range(count):
        head, pos = read_u32(params, pos)
        name, pos = read_string(params, pos)
        stat_count, pos = read_u32(params, pos)
        stats = []
        for _ in range(stat_count):
            group_id, pos = read_u32(params, pos)
            unk1, pos = read_u32(params, pos)
            unk2, pos = read_u32(params, pos)
            info_count, pos = read_u32(params, pos)
            infos = []
            for _ in range(info_count):
                info_id = params[pos]
                pos += 1
                v1, pos = _read_stat_value(params, pos)
                op = params[pos]
                pos += 1
                v2, pos = _read_stat_value(params, pos)
                v3, pos = _read_stat_value(params, pos)
                infos.append((info_id, v1, op, v2, v3))
            when = struct.unpack_from('<Q', params, pos)[0]
            pos += 8
            stats.append((group_id, unk1, unk2, infos, when))
        containers.append((head, name, stats))
    return containers, pos


def build_player_stats_containers(containers):
    """Encode what `parse_player_stats_containers` returns."""
    buf = bytearray(struct.pack('<I', len(containers)))
    for head, name, stats in containers:
        buf += struct.pack('<I', head)
        write_string(buf, name)
        buf += struct.pack('<I', len(stats))
        for group_id, unk1, unk2, infos, when in stats:
            buf += struct.pack('<IIII', group_id, unk1, unk2, len(infos))
            for info_id, v1, op, v2, v3 in infos:
                buf.append(info_id)
                _write_stat_value(buf, v1)
                buf.append(op)
                _write_stat_value(buf, v2)
                _write_stat_value(buf, v3)
            buf += struct.pack('<Q', when)
    return bytes(buf)


def _stats_keep_raw_report(user, groups):
    """Keep every report as it arrived, the way profile blobs are kept.

    The folding rules below are a reading of the client's `op` byte. If one
    of them turns out wrong, a career can be rebuilt from these files
    instead of being lost.
    """
    try:
        hist = os.path.join(storage.DATA_DIR, "stats_history")
        os.makedirs(hist, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        rows = [{"group": g, "info": i, "op": op, **v}
                for g, infos in groups for i, v, op in infos]
        with open(os.path.join(hist, f"{user}_{stamp}.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(rows, fh, indent=1)
        prune_history(hist, user, ".json")
    except OSError:
        pass


def _stats_fold(old, new, op):
    """Apply the client's own per-stat rule. See STATS_OP_* above."""
    if op == STATS_OP_ADD:
        return old + new
    if op == STATS_OP_MAX:
        return max(old, new)
    return new                                   # STATS_OP_SET, and anything new


def _stats_record_report(user, groups):
    """Fold one match's report into the player's stored career."""
    stats = _stats_load(user)
    _stats_keep_raw_report(user, groups)
    changed = []
    for group_id, infos in groups:
        for info_id, value, op in infos:
            key = (group_id, info_id)
            row = stats.get(key)
            if row is None:
                row = {"group": group_id, "info": info_id, "total": 0}
                stats[key] = row
            if op not in STATS_KNOWN_OPS:
                print(f"      (stat {group_id}/{info_id} carries an unknown "
                      f"op {op} - stored as a replacement, not added)")
            row["last"] = value["value"]
            row["a"], row["b"] = value["a"], value["b"]
            row["text"], row["type"], row["op"] = value["text"], value["type"], op
            if value["type"] != STATS_TYPE_INT:
                # Doubles and text are carried whole; there is nothing to add.
                row["total"] = value["value"]
                continue
            before = int(row.get("total", 0))
            effective = op if config.STATS_ACCUMULATE else STATS_OP_SET
            row["total"] = _stats_fold(before, int(value["value"]), effective)
            if value["value"] or row["total"] != before:
                sign = {STATS_OP_ADD: "+", STATS_OP_MAX: "^"}.get(effective, "=")
                changed.append(f"{group_id}/{info_id}{sign}{value['value']}"
                               f"={row['total']}")
    _stats_save(user, stats)
    return changed


def _stats_served_value(row):
    if row.get("type", STATS_TYPE_INT) != STATS_TYPE_INT:
        return int(row.get("last", row.get("total", 0)) or 0)
    return int(row.get("total", 0))


def _ladder_values(pid):
    """The ladder's stats as login reads them: 1/42 ladder points (100 before any match, Ubisoft's default), 1/43 the
    rating (0 = never placed); every career Ubisoft's server sent carried both."""
    return {(1, 42): ladder_record(pid)["points"], (1, 43): served_rating(pid)}


def _player_stats_containers(names, queries):
    """Build the method-3 reply for the named players, from what they sent."""
    refresh_player_directory()
    wanted = {n.lower(): n for n in names}
    found = []
    for entry in PLAYER_DIRECTORY.values():
        for candidate in (entry.get("name", ""), entry.get("account", "")):
            if str(candidate).lower() in wanted:
                found.append(entry)
                break
    # Files are named after the account (see `onlinesave_user`), so a
    # player asked for by a chosen display name still finds their career.
    body = bytearray()
    described = []
    containers = bytearray()
    written = 0
    for entry in found:
        stats = _stats_load(_career_user(entry))
        ladder = _ladder_values(entry["pid"])
        if not stats and not has_ladder_record(entry["pid"]):
            continue
        containers += struct.pack('<I', entry["pid"] & 0xFFFFFFFF)
        write_string(containers, contact_safe_name(entry["name"]))
        containers += struct.pack('<I', len(queries))
        for group_id, info_ids in queries:
            containers += struct.pack('<III', group_id, 0, 0)
            containers += struct.pack('<I', len(info_ids))
            shown = 0
            for info_id in info_ids:
                row = stats.get((group_id, info_id))
                containers.append(info_id & 0xFF)
                if (group_id, info_id) in ladder:
                    value = _int_value(ladder[(group_id, info_id)])
                    shown += 1
                elif row is None:
                    value = {"value": 0, "a": 0, "b": 0, "text": "",
                             "type": STATS_TYPE_INT}
                else:
                    value = {"value": _stats_served_value(row),
                             "a": int(row.get("a", 0)),
                             "b": int(row.get("b", 0)),
                             "text": row.get("text", ""),
                             "type": int(row.get("type", STATS_TYPE_INT))}
                    shown += 1
                _write_stat_value(containers, value)
                containers.append(0)                       # op, always 0 in Ubisoft's replies
                empty = {"value": 0, "a": 0, "b": 0, "text": "",
                         "type": value["type"]}
                _write_stat_value(containers, empty)
                _write_stat_value(containers, empty)
            containers += struct.pack('<Q', q_datetime_now())
            described.append(f"{entry['name']} group {group_id}: "
                             f"{shown}/{len(info_ids)} known")
        written += 1
    body += struct.pack('<I', written)
    body += containers
    return bytes(body), described


LADDER_BOARD = 43         # the Abstergo Ladder's board id, the only one the game asks for
BOARD_INFOS = ((43, 0), (4, 1), (44, 2))   # (info id, op byte) of every board entry, as Ubisoft's
BOARD_PAGE_MAX = 100      # the game asks 20-30 at a time; never build more than this


def _career_user(entry):
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(entry.get("account") or entry["name"]))


def _career_int(entry, group_id, info_id):
    row = _stats_load(_career_user(entry)).get((group_id, info_id))
    return 0 if row is None else _stats_served_value(row)


def _int_value(number):
    return {"value": int(number), "a": 0, "b": 0, "text": "", "type": STATS_TYPE_INT}


def _board_entries(buf, rows, pairs):
    """m3 containers for board rows [(rank, pid, name, record, category)], one stat block per (group, stat) pair."""
    when = q_datetime_now()
    for rank, pid, name, rec, category in rows:
        entry = PLAYER_DIRECTORY.get(pid) or {"name": name}
        buf += struct.pack('<I', pid & 0xFFFFFFFF)
        write_string(buf, contact_safe_name(name))
        buf += struct.pack('<I', len(pairs))
        for group_id, stat_id in pairs:
            if stat_id != LADDER_BOARD:
                buf += struct.pack('<IIfI', group_id, 0, 0.0, 0)
                buf += struct.pack('<Q', when)
                continue
            values = {43: rec["points"], 4: _career_int(entry, 1, 4), 44: category}
            buf += struct.pack('<IIfI', group_id, rank, float(rec["points"]), len(BOARD_INFOS))
            for info_id, op in BOARD_INFOS:
                buf.append(info_id)
                _write_stat_value(buf, _int_value(values[info_id]))
                buf.append(op)
                _write_stat_value(buf, _int_value(0))
                _write_stat_value(buf, _int_value(0))
            buf += struct.pack('<Q', when)


def build_board_reply(rows, pairs, total):
    """[list<m3 container>][u32]: the out-param is the board's size (what Ubisoft's server put there is unknown)."""
    buf = bytearray(struct.pack('<I', len(rows)))
    _board_entries(buf, rows, pairs)
    buf += struct.pack('<I', total)
    return bytes(buf)


def _board_for(stat_id):
    return ladder_board() if stat_id == LADDER_BOARD else []


def _named(row, wanted):
    """Is board row `row` one of the lower-cased names in `wanted` (display name or account)?"""
    account = str((PLAYER_DIRECTORY.get(row[1]) or {}).get("account", "")).lower()
    return str(row[2]).lower() in wanted or (account and account in wanted)


def parse_board_range_request(params):
    """m4: [u32 group][u32 statId][u32 startRank][u32 count][u32 0][u32 0] (24 bytes)."""
    group_id, pos = read_u32(params, 0)
    stat_id, pos = read_u32(params, pos)
    start, pos = read_u32(params, pos)
    count, pos = read_u32(params, pos)
    return group_id, stat_id, start, count, len(params) - pos - 8


def parse_board_player_request(params):
    """m5: [u32 group][u32 statId][string name][u32 count][u32 0][u32 0]."""
    group_id, pos = read_u32(params, 0)
    stat_id, pos = read_u32(params, pos)
    name, pos = read_string(params, pos)
    count, pos = read_u32(params, pos)
    return group_id, stat_id, name, count, len(params) - pos - 8


def parse_board_names_request(params):
    """m6: [u32 n] { [u32 group][u32 statId] } [list<string> names]. The pairs are a list: the game can ask for more
    than one stat at once."""
    count, pos = read_u32(params, 0)
    pairs = []
    for _ in range(count):
        group_id, pos = read_u32(params, pos)
        stat_id, pos = read_u32(params, pos)
        pairs.append((group_id, stat_id))
    names, pos = read_string_list(params, pos)
    return pairs, names, len(params) - pos


def handle_playerstatistics_leaderboard_range(call_id, params, addr):   # m4
    try:
        group_id, stat_id, start, count, left = parse_board_range_request(params)
        board = _board_for(stat_id)
        first = max(1, start)
        rows = board[first - 1:first - 1 + min(count, BOARD_PAGE_MAX)]
        print(f"[{addr}]   PlayerStatistics.ReadStatsLeaderboardByRange: board {stat_id}, {count} from rank {start}"
              f"{f', {left} byte(s) unread' if left else ''} -> {len(rows)} of {len(board)}")
        body = build_board_reply(rows, [(group_id, stat_id)], len(board))
    except Exception as e:
        print(f"[{addr}]   PlayerStatistics.ReadStatsLeaderboardByRange: could not parse ({e}) -> empty board")
        body = build_board_reply([], [], 0)
    return "PlayerStatistics.ReadStatsLeaderboardByRange", build_rmc_response(0x6C, call_id, 4, body)


def handle_playerstatistics_leaderboard_for_player(call_id, params, addr):   # m5
    """The `count` ranks around the named player, as near the middle as the board allows (at rank 1, the player and
    the ranks under them, as Ubisoft's server answered)."""
    try:
        group_id, stat_id, name, count, left = parse_board_player_request(params)
        board = _board_for(stat_id)
        mine = [r for r in board if _named(r, {name.lower()})]
        rows = []
        count = min(count, BOARD_PAGE_MAX)
        if mine and count:
            first = max(1, mine[0][0] - count // 2)
            last = min(len(board), first + count - 1)
            first = max(1, last - count + 1)
            rows = board[first - 1:last]
        print(f"[{addr}]   PlayerStatistics.ReadStatsLeaderboardByRangeForPlayer: board {stat_id}, {count} around "
              f"{name!r}{f' (rank {mine[0][0]})' if mine else ' (not on it)'}"
              f"{f', {left} byte(s) unread' if left else ''} -> {len(rows)} of {len(board)}")
        body = build_board_reply(rows, [(group_id, stat_id)], len(board))
    except Exception as e:
        print(f"[{addr}]   PlayerStatistics.ReadStatsLeaderboardByRangeForPlayer: could not parse ({e}) -> empty")
        body = build_board_reply([], [], 0)
    return "PlayerStatistics.ReadStatsLeaderboardByRangeForPlayer", build_rmc_response(0x6C, call_id, 5, body)


def handle_playerstatistics_leaderboard_by_names(call_id, params, addr):   # m6
    """The named players' entries, each with its rank on the whole board, in the board's order."""
    try:
        pairs, names, left = parse_board_names_request(params)
        board = _board_for(LADDER_BOARD) if any(stat == LADDER_BOARD for _g, stat in pairs) else []
        wanted = {n.lower() for n in names}
        rows = [r for r in board if _named(r, wanted)][:BOARD_PAGE_MAX]
        print(f"[{addr}]   PlayerStatistics.ReadStatsLeaderboardByPlayerNames: {len(names)} name(s), "
              f"stats {pairs}{f', {left} byte(s) unread' if left else ''} -> {len(rows)} on the board")
        body = build_board_reply(rows, pairs, len(board))
    except Exception as e:
        print(f"[{addr}]   PlayerStatistics.ReadStatsLeaderboardByPlayerNames: could not parse ({e}) -> empty")
        body = build_board_reply([], [], 0)
    return "PlayerStatistics.ReadStatsLeaderboardByPlayerNames", build_rmc_response(0x6C, call_id, 6, body)


def handle_playerstatistics_send(call_id, params, addr):     # m2 SendPlayerStats
    """One match's numbers, folded into the career. A report with any byte left over is logged and not stored."""
    user = onlinesave_user(addr)
    try:
        groups, pos = parse_player_stats_report(params)
    except Exception as e:
        print(f"[{addr}]   PlayerStatistics.SendPlayerStats for '{user}': could "
              f"not parse the {len(params)}-byte report ({e}) - nothing stored")
        return "PlayerStatistics.SendPlayerStats", \
            build_rmc_response(0x6C, call_id, 2, b"")
    if pos != len(params):
        # Never fold a report we did not fully understand into the totals.
        print(f"[{addr}]   PlayerStatistics.SendPlayerStats for '{user}': "
              f"{len(params) - pos} trailing byte(s) after "
              f"{len(groups)} group(s) - not stored, layout may have changed")
        return "PlayerStatistics.SendPlayerStats", \
            build_rmc_response(0x6C, call_id, 2, b"")
    changed = _stats_record_report(user, groups)
    summary = ", ".join(changed[:8]) + ("..." if len(changed) > 8 else "")
    print(f"[{addr}]   PlayerStatistics.SendPlayerStats for '{user}': "
          f"{len(groups)} group(s), "
          f"{sum(len(i) for _g, i in groups)} stat(s) stored"
          + (f" [{summary}]" if summary else " (all zero)"))
    return "PlayerStatistics.SendPlayerStats", \
        build_rmc_response(0x6C, call_id, 2, b"")
