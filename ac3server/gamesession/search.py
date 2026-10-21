"""GameSession (protocol 0x2A) searches: finding a match to join, and finding which session a friend is in.

    m7  SearchSessions                    [GameSessionQuery]                 -> [list] { search result }
    m24 SearchSessionsWithParticipants    [u32 typeId][list<pid>]            -> [list] { search result, [list<pid>] }

A GameSessionQuery is [u32 typeId][u32 queryId][u32 count] { [u32 attrId][u32 value] } - the three query sizes the
game sends (44, 52, 60 bytes) are exactly 4, 5 and 6 attributes. SearchSessions offers every session hosted by
somebody else that is of the asked key type and is a match, not a lobby (state.py). Comparing the query's game mode,
session type and playlist is `strict_session_matching`; off, the log still says which sessions it would have
dropped and why.

m24 is behind a friends list's "Join game": the contacts reply flags who is in a session (services/contacts.py), and
the game then asks which. Each element is an ordinary search result followed by that session's participant list
(Ubisoft's non-empty replies decode exactly that way).
"""

import struct
import time

import server_config as config

from ac3server.gamesession.state import (ATTR_GAME_MODE, ATTR_PLAYLIST_ID, ATTR_SESSION_TYPE, GAME_SESSIONS,
    LAST_SEARCH, SESSION_ATTR_NAMES, build_search_result, describe_attrs, read_attributes)
from ac3server.players.identity import CONNECTED_PIDS
from ac3server.protocol.datatypes import read_u32
from ac3server.protocol.rmc import build_rmc_response


def handle_gamesession_search_with_participants(call_id, params, addr):          # m24 SearchSessionsWithParticipants
    wanted = []
    type_id = None
    try:
        type_id, pos = read_u32(params, 0)
        count, pos = read_u32(params, pos)
        for _ in range(count):
            pid, pos = read_u32(params, pos)
            wanted.append(pid)
    except Exception as e:
        print(f"[{addr}]   GameSession.SearchSessionsWithParticipants: "
              f"could not parse the {len(params)}-byte request ({e}) - empty reply")
        return "GameSession.SearchSessionsWithParticipants", build_rmc_response(
            0x2A, call_id, 24, struct.pack('<I', 0))

    caller = CONNECTED_PIDS.get(addr)
    found = []
    wrong_type = []
    for session_id, session in GAME_SESSIONS.items():
        participants = session.get("participants", [])
        if session.get("host") == addr:
            continue
        if not any(pid in participants for pid in wanted):
            continue
        # the asked type matters: a group is a session too (type 2), and ignoring it would report a friend in two
        # games, one of them the group they are both in
        if type_id is not None and session.get("type_id") != type_id:
            wrong_type.append(session_id)
            continue
        found.append((session_id, session, participants))
    resp = bytearray(struct.pack('<I', len(found)))
    for session_id, session, participants in found:
        resp += build_search_result(session_id, session)
        resp += struct.pack('<I', len(participants))
        for pid in participants:
            resp += struct.pack('<I', pid)
    print(f"[{addr}]   GameSession.SearchSessionsWithParticipants: "
          f"{len(wanted)} pid(s) asked by {caller}, typeId={type_id} -> "
          f"{len(found)} session(s) {[hex(s) for s, _x, _y in found]}"
          + (f" ({len(wrong_type)} skipped as the wrong session type: "
             f"{[hex(s) for s in wrong_type]})" if wrong_type else ""))
    return "GameSession.SearchSessionsWithParticipants", build_rmc_response(
        0x2A, call_id, 24, bytes(resp))


def _read_session_query(params):
    """[u32 typeId][u32 queryId][u32 count] { [u32 id][u32 value] }."""
    type_id, pos = read_u32(params, 0)
    query_id, pos = read_u32(params, pos)
    attrs, pos = read_attributes(params, pos)
    return {"type_id": type_id, "query_id": query_id, "attributes": attrs,
            "trailing": len(params) - pos}


def _query_excludes(query, session):
    """Which query fields a session fails on, as a list of readable reasons.

    Only what decides whether a match is playable at all: game mode, session type and playlist. A filter too strict
    returns nothing and looks exactly like a broken server, so slot counts and levels are left out.

    `PrivateCode` (0x64) is deliberately not compared: the game puts the game settings hash in it.
    """
    reasons = []
    for attr in (ATTR_GAME_MODE, ATTR_SESSION_TYPE, ATTR_PLAYLIST_ID):
        want = query["attributes"].get(attr)
        have = session["attributes"].get(attr)
        if want is None or have is None:
            continue
        if want != have:
            reasons.append(f"{SESSION_ATTR_NAMES[attr]} {have} != requested {want}")
    return reasons


def handle_gamesession_search_sessions(call_id, params, addr):    # m7
    """The matches other players are hosting. Never the caller's own: a client must not find and join itself. An
    empty list is four zero bytes, exactly Ubisoft's reply."""
    query = None
    try:
        query = _read_session_query(params)
        note = "" if not query["trailing"] else \
            f", {query['trailing']} trailing byte(s) unconsumed"
        print(f"[{addr}]   GameSession.SearchSessions query: typeId="
              f"{query['type_id']} queryId={query['query_id']} "
              f"[{describe_attrs(query['attributes'])}]{note}")
    except Exception as e:
        print(f"[{addr}]   GameSession.SearchSessions: could not parse the "
              f"{len(params)}-byte query ({e}) - returning everything")

    others = {sid: s for sid, s in GAME_SESSIONS.items() if s.get("host") != addr}
    # the query's own key type: a player searching for a match must not be offered somebody's group
    if query is not None:
        mismatched = [sid for sid, s in others.items()
                      if s.get("type_id") != query["type_id"]]
        if mismatched:
            print(f"      skipping {len(mismatched)} session(s) of a different "
                  f"type than the requested {query['type_id']}: "
                  f"{[hex(s) for s in mismatched]}")
            others = {sid: s for sid, s in others.items() if sid not in mismatched}
    # never a lobby, which a searcher cannot play in. The rule is in state.py (SEARCH_TO_HOST_WINDOW): created
    # without a preceding search, or with no game mode chosen.
    if others:
        idle = [sid for sid, s in others.items()
                if not s.get("advertised")
                or s.get("attributes", {}).get(ATTR_GAME_MODE) == 0xFFFFFFFF]
        if idle:
            print(f"      skipping {len(idle)} idle lobby session(s) {[hex(s) for s in idle]}"
                  f" - created without a preceding search, or with no game mode")
            others = {sid: s for sid, s in others.items() if sid not in idle}
    if query is not None:
        LAST_SEARCH[addr] = time.time()
    if query is not None and others:
        kept = {}
        for sid, session in others.items():
            reasons = _query_excludes(query, session)
            if not reasons:
                kept[sid] = session
            elif config.STRICT_SESSION_MATCHING:
                print(f"      filtered out {sid:#x}: {'; '.join(reasons)}")
            else:
                print(f"      {sid:#x} would be filtered out ({'; '.join(reasons)}) "
                      f"- returning it anyway, strict_session_matching is off")
                kept[sid] = session
        others = kept
    if not others:
        print(f"[{addr}]   GameSession.SearchSessions ({len(params)}-byte query): "
              f"no sessions hosted by anyone else -> empty result list")
        return "GameSession.SearchSessions", build_rmc_response(
            0x2A, call_id, 7, struct.pack('<I', 0))
    resp = bytearray(struct.pack('<I', len(others)))
    for session_id, session in others.items():
        resp += build_search_result(session_id, session)
    print(f"[{addr}]   GameSession.SearchSessions ({len(params)}-byte query): "
          f"returning {len(others)} session(s): {[hex(s) for s in others]} "
          f"(matching {'on' if config.STRICT_SESSION_MATCHING else 'off'})")
    return "GameSession.SearchSessions", build_rmc_response(0x2A, call_id, 7, bytes(resp))
