"""Ladder (protocol 0x7A): the Abstergo Ladder - ladder points, a hidden rating and the board's ranks.

    m1 registerNewGame    [u32 1][u32 type][u32 pid][u16 len][blob]  -> [u32 gameId]     (the host, at match start)
    m2 participateToGame  [u32 gameId][u32 pid][u16 len][blob]       -> nothing          (every other player)
    m3 endGame            [u32 gameId][list<LadderInfo>]             -> list<LadderInfo> (the host, at match end)
    m5 getRewardBitfield  nothing                                    -> u32: Ubisoft's 0x2b

LadderInfo is [u32 pid][u32 value][u16 len][blob] (the game's reader FUN_01193a20). Every blob is big-endian:
[u32 total length][u32 c4e0a61f][u32 crc32(payload)][u32 field]... (build_ladder_blob reproduces Ubisoft's bytes).

* m1's type is 2 in a mode with teams, 1 otherwise, and the blob's one field is the host's team index (0 without
  teams) - FUN_008D5EA6 -> FUN_00DB5525; m2's blob is the joiner's team the same way (FUN_00DB8D39).
* m3's list is each player's [pid][match score][blob{his team's score this round}] (FUN_008B8148); in practice the
  blobs hold 0, so the team result is worked out from the match scores, summed per registered team.
* The reply is the same list: [pid][income][blob{A, B, C}]. The client adds income to its ladder points
  (profile +0xD8, shown as ladderIncome / ladderRate), keeps A at profile +0xDC and B at ladder +0x68
  (ladderJoinStatus); C is not read (FUN_00DC3F56 -> FUN_00DBC595). Ubisoft's C = the points after the match
  (the points at login + income), floor 100. The client does no rating maths.

Our maths (Ubisoft's is unknown): every player has a rating (A, Elo, starts at RATING_START) and ladder points (C, start
at POINTS_START and never fall). A player's advantage a is S - E against the other side(s) - S 1 win / 0.5 draw / 0
loss, E = 1 / (1 + 10^((Ropp - Rown) / 400)): team modes compare team totals and team mean ratings, FFA compares every
pair of players and averages over the opponents. The rating moves by RATING_K * a, income = max(0, round(POINTS_K * a)).
B = 0 for a player the game registered (m1 / m2), 1 for one it did not (joined in progress); B is informational. Private
matches count unless server_config ladder_private is "skip"; a match where nobody scored counts nothing (income 0,
values unchanged). No farming limits: no caps, no minimum player count - a player alone has no opponent and earns
nothing anyway.
server_config ladder false answers endGame with the empty list and stores nothing.

DATA_DIR/ladder.json holds the game id counter, the games in flight and every player's ladder record (plain JSON,
re-read when edited on disk). PlayerStatistics serves the records: group 1 info 42 = points, 43 = rating
(0 = never placed), and the board 43 pages (statistics.py).

0x7A is Ladder by the game's own protocol registration (FUN_01157e20), not acb-rdv's "ShopRenting".
"""

import json
import os
import struct
import time
import zlib

import server_config as config
from ac3server import storage

from ac3server.gamesession.state import (ATTR_GAME_MODE, ATTR_PLAYLIST_ID, ATTR_PRIVATE_SESSION, ATTR_WORLD_ID,
                                        hosted_session_of)
from ac3server.match_names import map_label, mode_label
from ac3server.players.directory import PLAYER_DIRECTORY, refresh_player_directory
from ac3server.players.identity import PLAYER_IDENTITIES
from ac3server.protocol.datatypes import read_u32
from ac3server.protocol.rmc import build_rmc_response
from ac3server.storage import atomic_write, set_aside


LADDER_PROTO = 0x7A
BLOB_TAG = 0xC4E0A61F

RATING_START = 1200             # A before a player's first counted match
RATING_K = 32                   # rating moved per match at advantage 1
POINTS_START = 100              # Ubisoft's default and floor (stat 1/42 was 100 for every unplaced player)
POINTS_K = 50                   # income at advantage 1: an even two-team win pays 25
FIRST_GAME_ID = 1001
GAME_KEEP_SECONDS = 12 * 3600   # a game with no endGame is forgotten after this; an ended one too
PRIVATE_PLAYLISTS = (8, 10, 11)  # private="true" in the served playlistconfig
CATEGORY_UNPLACED = 5           # board info 44: Ubisoft's never-placed players carried 5, the top ones 0


# --- the blob ------------------------------------------------------------------------------------------------------

def build_ladder_blob(fields):
    """[u32 len][u32 c4e0a61f][u32 crc32(payload)][fields], all big-endian."""
    payload = b"".join(struct.pack(">I", f & 0xFFFFFFFF) for f in fields)
    return struct.pack(">III", 12 + len(payload), BLOB_TAG, zlib.crc32(payload) & 0xFFFFFFFF) + payload


def parse_ladder_blob(blob):
    """The blob's u32 fields, or None when its length, tag or crc is wrong."""
    if len(blob) < 12 or len(blob) % 4:
        return None
    total, tag, crc = struct.unpack_from(">III", blob, 0)
    payload = blob[12:]
    if total != len(blob) or tag != BLOB_TAG or zlib.crc32(payload) & 0xFFFFFFFF != crc:
        return None
    return list(struct.unpack(">%dI" % (len(payload) // 4), payload))


def read_ladder_infos(params, pos):
    count, pos = read_u32(params, pos)
    infos = []
    for _ in range(count):
        pid, pos = read_u32(params, pos)
        value, pos = read_u32(params, pos)
        length = struct.unpack_from('<H', params, pos)[0]
        pos += 2
        blob = bytes(params[pos:pos + length])
        if len(blob) != length:
            raise ValueError("a blob runs past the end")
        pos += length
        infos.append((pid, value, blob))
    return infos, pos


def build_ladder_infos(infos):
    buf = bytearray(struct.pack('<I', len(infos)))
    for pid, value, blob in infos:
        buf += struct.pack('<IIH', pid & 0xFFFFFFFF, value & 0xFFFFFFFF, len(blob))
        buf += blob
    return bytes(buf)


# --- the store -----------------------------------------------------------------------------------------------------

LADDER_STORE = {"next_game_id": FIRST_GAME_ID, "games": {}, "players": {}}
_LADDER_MTIME = [None]      # (path, mtime) of the file as last read or written (mtime None = no file)


def _ladder_path():
    return os.path.join(storage.DATA_DIR, "ladder.json")


def ladder_state():
    """The store, re-read when ladder.json changed on disk (a hand edit is never undone by the next save)."""
    path = _ladder_path()
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        stamp = None
    if _LADDER_MTIME[0] == (path, stamp):
        return LADDER_STORE
    fresh = {"next_game_id": FIRST_GAME_ID, "games": {}, "players": {}}
    if stamp is not None:
        try:
            with open(path, encoding="utf-8") as fh:
                stored = json.load(fh)
            fresh["next_game_id"] = max(FIRST_GAME_ID, int(stored.get("next_game_id", FIRST_GAME_ID)))
            fresh["games"] = dict(stored.get("games") or {})
            fresh["players"] = dict(stored.get("players") or {})
        except (OSError, ValueError, TypeError, AttributeError) as e:
            # a damaged file is moved aside; the counter never starts again below an id already handed out
            fresh["next_game_id"] = max(FIRST_GAME_ID, int(LADDER_STORE.get("next_game_id", FIRST_GAME_ID)))
            set_aside(path, e)
            stamp = None
    LADDER_STORE.clear()
    LADDER_STORE.update(fresh)
    _LADDER_MTIME[0] = (path, stamp)
    return LADDER_STORE


def _ladder_save():
    try:
        os.makedirs(storage.DATA_DIR, exist_ok=True)
        atomic_write(_ladder_path(), json.dumps(LADDER_STORE, indent=1, sort_keys=True))
        _LADDER_MTIME[0] = (_ladder_path(), os.path.getmtime(_ladder_path()))
    except OSError as e:
        print(f"      (ladder kept in memory only: {e})")


def ladder_record(pid):
    """A player's ladder record (a copy, with the defaults filled in), whether or not one is stored."""
    stored = ladder_state()["players"].get(str(pid)) or {}
    return {"points": int(stored.get("points", POINTS_START)), "rating": int(stored.get("rating", RATING_START)),
            "games": int(stored.get("games", 0)), "wins": int(stored.get("wins", 0)),
            "updated": stored.get("updated"), "name": stored.get("name")}


def has_ladder_record(pid):
    return str(pid) in ladder_state()["players"]


def served_rating(pid):
    """Stat 1/43: the rating, 0 until the first counted match (Ubisoft's never-placed players read 0)."""
    rec = ladder_record(pid)
    return rec["rating"] if rec["games"] else 0


# --- the board -----------------------------------------------------------------------------------------------------

def ladder_board():
    """Every player the server knows, best first: [(rank, pid, name, record, category)].

    Ordered by points, then rating (placed players before unplaced), then games, then name. The category (board
    info 44) is ours: 0-4 by fifths of the placed players, best first, 5 = never placed."""
    refresh_player_directory()
    players = ladder_state()["players"]
    rows = []
    for pid in set(PLAYER_DIRECTORY) | {int(p) for p in players}:
        rec = ladder_record(pid)
        name = PLAYER_DIRECTORY.get(pid, {}).get("name") or rec.get("name") or str(pid)
        rows.append((pid, name, rec))
    rows.sort(key=lambda r: (-r[2]["points"], -(r[2]["rating"] if r[2]["games"] else 0), -r[2]["games"],
                             str(r[1]).lower(), r[0]))
    placed = sum(1 for _p, _n, rec in rows if rec["games"])
    board, placed_rank = [], 0
    for rank, (pid, name, rec) in enumerate(rows, 1):
        if rec["games"]:
            placed_rank += 1
            category = min(4, 5 * (placed_rank - 1) // max(1, placed))
        else:
            category = CATEGORY_UNPLACED
        board.append((rank, pid, name, rec, category))
    return board


# --- the maths -----------------------------------------------------------------------------------------------------

def _expected(own, other):
    return 1.0 / (1.0 + 10 ** ((other - own) / 400.0))


def _result(a, b):
    return 1.0 if a > b else 0.5 if a == b else 0.0


def ladder_advantages(entries, teams, ratings):
    """{pid: S - E}. entries [(pid, score)], teams {pid: team} (empty = FFA), ratings {pid: rating}.

    Team modes: each team's total score and mean rating, the advantage against every other team averaged. FFA: every
    pair of players, averaged over the opponents. A team game where a player's team is unknown, or where every
    player is on one team, is worked out as FFA."""
    if teams and all(pid in teams for pid, _s in entries) and len({teams[p] for p, _s in entries}) >= 2:
        totals, members = {}, {}
        for pid, score in entries:
            totals[teams[pid]] = totals.get(teams[pid], 0) + score
            members.setdefault(teams[pid], []).append(pid)
        mean = {t: sum(ratings[p] for p in m) / len(m) for t, m in members.items()}
        team_adv = {}
        for t in totals:
            others = [o for o in totals if o != t]
            team_adv[t] = sum(_result(totals[t], totals[o]) - _expected(mean[t], mean[o]) for o in others) / len(others)
        return {pid: team_adv[teams[pid]] for pid, _s in entries}
    adv = {}
    for pid, score in entries:
        others = [(o, s) for o, s in entries if o != pid]
        adv[pid] = (sum(_result(score, s) - _expected(ratings[pid], ratings[o]) for o, s in others) / len(others)
                    if others else 0.0)
    return adv


def _counts(game, entries):
    """(counted, why not)."""
    if not any(score for _pid, score in entries):
        return False, "nobody scored"
    if game and game.get("private") and config.LADDER_PRIVATE == "skip":
        return False, "a private match (ladder_private skip)"
    return True, ""


def ladder_end_game(game_id, infos):
    """Work out and store one match's results -> ([(pid, income, A, B, C)] in the request's order, a note)."""
    state = ladder_state()
    game = state["games"].get(str(game_id))
    if game and game.get("results"):
        # the same game ended twice (a migrated host, a resend the transport did not catch): the same answer
        return [tuple(r) for r in game["results"]], "already ended - the stored results again"
    entries = [(pid, score) for pid, score, _blob in infos]
    registered = {int(p): t for p, t in (game or {}).get("teams", {}).items()}
    teams = registered if game and game.get("type") == 2 else {}
    counted, why = _counts(game, entries)
    ratings = {pid: ladder_record(pid)["rating"] for pid, _s in entries}
    notes = []
    if not game:
        notes.append(f"game {game_id} was never registered here - worked out as FFA")
    elif teams and not all(pid in teams for pid, _s in entries):
        notes.append("a player's team is unknown - worked out as FFA")
    adv = ladder_advantages(entries, teams, ratings) if counted else {}
    refresh_player_directory()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    results = []
    for pid, _score in entries:
        rec = ladder_record(pid)
        join_status = 0 if pid in registered else 1
        income = 0
        if counted:
            a = adv[pid]
            income = max(0, int(round(POINTS_K * a)))
            entry = PLAYER_DIRECTORY.get(pid, {})
            stored = state["players"].setdefault(str(pid), {})
            rec["rating"] = int(round(rec["rating"] + RATING_K * a))
            rec["points"] = max(POINTS_START, rec["points"] + income)
            stored.update({"points": rec["points"], "rating": rec["rating"], "games": rec["games"] + 1,
                           "wins": rec["wins"] + (1 if a > 0 else 0), "updated": now,
                           "name": entry.get("name") or rec.get("name") or str(pid),
                           "account": entry.get("account") or stored.get("account")})
        results.append((pid, income, rec["rating"], join_status, rec["points"]))
    if not counted:
        notes.append(f"not counted: {why}")
    record = game if game is not None else {"type": 0, "teams": {}, "created": time.time()}
    if record.get("host") and not record.get("map"):
        record["map"] = _host_match(record["host"])[3]
    record.update({"results": [list(r) for r in results], "ended": time.time(), "counted": counted})
    state["games"][str(game_id)] = record
    _ladder_save()
    return results, "; ".join(notes)


def _prune_games(state):
    cutoff = time.time() - GAME_KEEP_SECONDS
    for gid in [g for g, rec in state["games"].items() if rec.get("ended", rec.get("created", 0)) < cutoff]:
        del state["games"][gid]


def _team_of(blob):
    fields = parse_ladder_blob(blob)
    return fields[0] if fields else None


def _host_match(pid):
    """(playlist, mode, private, map) of the match session `pid` hosts, as far as the server knows it."""
    _sid, session = hosted_session_of(pid)
    if not session:
        return None, None, False, None
    attrs = session.get("attributes", {})
    playlist = attrs.get(ATTR_PLAYLIST_ID)
    private = bool(attrs.get(ATTR_PRIVATE_SESSION)) or playlist in PRIVATE_PLAYLISTS
    return playlist, attrs.get(ATTR_GAME_MODE), private, attrs.get(ATTR_WORLD_ID) or None


def _match_label(game):
    """"Domination on Galata (Day), private" for a game record."""
    if not game:
        return "mode unknown on map unknown"
    return (f"{mode_label(game.get('mode'))} on {map_label(game.get('map'))}"
            + (", private" if game.get("private") else ""))


# --- the handlers --------------------------------------------------------------------------------------------------

def handle_ladder_get_reward_bitfield(call_id, params, addr):
    return "Ladder.GetRewardBitfield", build_rmc_response(0x7A, call_id, 5,
                                                          bytes.fromhex("2b000000"))


def handle_ladder_register_new_game(call_id, params, addr):     # m1
    """A fresh game id, never 0 and never handed out twice (the counter is saved before the reply goes)."""
    state = ladder_state()
    _prune_games(state)
    game_id = state["next_game_id"]
    state["next_game_id"] = game_id + 1
    record = {"type": 0, "teams": {}, "created": time.time()}
    try:
        _one, pos = read_u32(params, 0)
        game_type, pos = read_u32(params, pos)
        pid, pos = read_u32(params, pos)
        length = struct.unpack_from('<H', params, pos)[0]
        pos += 2
        team = _team_of(bytes(params[pos:pos + length]))
        pos += length
        note = "" if pos == len(params) else f", {len(params) - pos} trailing byte(s)"
        playlist, mode, private, world = _host_match(pid)
        record.update({"type": game_type, "host": pid, "teams": {str(pid): team if team is not None else 0},
                       "playlist": playlist, "mode": mode, "private": private, "map": world})
        kind = {1: "FFA", 2: "team mode"}.get(game_type, f"type {game_type}")
        print(f"[{addr}]   Ladder.RegisterNewGame: host {pid}, {kind}, team {team}, playlist {playlist} "
              f"mode {mode}{' (private)' if private else ''}{note} -> game {game_id} [{_match_label(record)}]")
    except Exception as e:
        record["host"] = (PLAYER_IDENTITIES.get(addr) or {}).get("pid")
        print(f"[{addr}]   Ladder.RegisterNewGame: could not parse the {len(params)}-byte request ({e}) "
              f"-> game {game_id} all the same")
    state["games"][str(game_id)] = record
    _ladder_save()
    return "Ladder.RegisterNewGame", build_rmc_response(LADDER_PROTO, call_id, 1, struct.pack('<I', game_id))


def handle_ladder_participate(call_id, params, addr):              # m2
    try:
        game_id, pos = read_u32(params, 0)
        pid, pos = read_u32(params, pos)
        length = struct.unpack_from('<H', params, pos)[0]
        pos += 2
        team = _team_of(bytes(params[pos:pos + length]))
        pos += length
        note = "" if pos == len(params) else f", {len(params) - pos} trailing byte(s)"
        state = ladder_state()
        game = state["games"].get(str(game_id))
        if game is None:
            print(f"[{addr}]   Ladder.ParticipateToGame: game {game_id}, pid {pid}, team {team}{note} "
                  f"- a game this server never registered (kept, so its end still counts)")
            game = state["games"][str(game_id)] = {"type": 0, "teams": {}, "created": time.time()}
        else:
            print(f"[{addr}]   Ladder.ParticipateToGame: game {game_id}, pid {pid}, team {team}{note}")
        game["teams"][str(pid)] = team if team is not None else 0
        _ladder_save()
    except Exception as e:
        print(f"[{addr}]   Ladder.ParticipateToGame: could not parse the "
              f"{len(params)}-byte request ({e}) -> empty reply")
    return "Ladder.ParticipateToGame", build_rmc_response(LADDER_PROTO, call_id,
                                                          2, b"")


def handle_ladder_end_game(call_id, params, addr):              # m3
    try:
        game_id, pos = read_u32(params, 0)
        infos, pos = read_ladder_infos(params, pos)
        note = "" if pos == len(params) else f", {len(params) - pos} trailing byte(s)"
        if not config.LADDER:
            print(f"[{addr}]   Ladder.EndGame game {game_id}: {len(infos)} player(s) "
                  f"{[(p, v) for p, v, _b in infos]}{note} -> empty list (server_config ladder false)")
            return "Ladder.EndGame", build_rmc_response(LADDER_PROTO, call_id, 3, struct.pack('<I', 0))
        results, why = ladder_end_game(game_id, infos)
        game = ladder_state()["games"].get(str(game_id))
        reply = [(pid, income, build_ladder_blob([a, b, c])) for pid, income, a, b, c in results]
        shown = ", ".join(f"{pid} score {score}: +{r[1]} -> {r[4]} pts, rating {r[2]}"
                          + (" (not registered)" if r[3] else "")
                          for (pid, score, _b), r in zip(infos, results))
        print(f"[{addr}]   Ladder.EndGame game {game_id}: {len(infos)} player(s){note} [{shown}]"
              + (f" - {why}" if why else "") + f" [{_match_label(game)}]")
        body = build_ladder_infos(reply)
    except Exception as e:
        print(f"[{addr}]   Ladder.EndGame: could not work out the {len(params)}-byte "
              f"request ({e}) -> empty list")
        body = struct.pack('<I', 0)
    return "Ladder.EndGame", build_rmc_response(LADDER_PROTO, call_id, 3, body)
