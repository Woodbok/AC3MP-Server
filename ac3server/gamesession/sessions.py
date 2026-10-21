"""GameSession (protocol 0x2A): hosting, joining and leaving sessions.

A host's sequence: SearchSessions (empty) -> CreateSession -> AddParticipants (itself) ->
RegisterURLs -> UpdateSession (repeatedly) ... RemoveParticipants -> AbandonSession. A joiner: GetSession ->
JoinSession -> GetParticipants (to learn everyone's station URLs and connect to them peer to peer).

    m1  CreateSession       [u32 typeId][attrs]             -> the key
    m2  UpdateSession       [key][attrs]                    -> nothing
    m4  MigrateSession      [key]                           -> a new key, the same session
    m5  LeaveSession        [key]                           -> nothing, or 0x8012000A if not a participant
    m6  GetSession          [key]                           -> one search result
    m8  AddParticipants     [key][list<pid> public][list<pid> private]
    m9  RemoveParticipants  [key][list<pid>]
    m11 GetParticipants     [key][u32 offset][u32 max]      -> [list] { [pid][name][list<url>] }
    m21 RegisterURLs        [list<station url>]             -> nothing (silence would cost a 15 s timeout)
    m22 JoinSession         [key]                           -> nothing
    m23 AbandonSession      [key]                           -> nothing

Layouts are from Ubisoft's server traffic and the game's embedded DDL; for JoinSession the DDL alone decides it.
Three rules the server keeps on top:

  * a player is in at most one session of each type (`_leave_other_sessions`): a player may be in a group (type 2)
    and a match (type 1) at once, but not in two of either;
  * a session ends with its LAST participant, not its host (`release_session`, `_migrate_host`): the leader tears
    the group session down as a match starts while the members are still in it;
  * a login clears every session the player is still listed in (`forget_stale_sessions`): a killed game never says
    it left.
"""

import struct
import time

import server_config as config
from ac3server import notifications

from ac3server.gamesession.groups import group_member_left, notify_group_leader, notify_group_members
from ac3server.gamesession.state import (GAME_SESSIONS, GROUP_MEMBER_JOINED_MINOR, GROUP_MEMBER_LEFT_MINOR,
    GROUP_SESSION_TYPE_ID, LAST_SEARCH, NEXT_SESSION_ID, PARTICIPANT_INFO, QERROR_SESSION_INVALID_KEY,
    REMOVED_BY_HOST, REMOVED_BY_HOST_TTL,
    QERROR_SESSION_NOT_PARTICIPANT, SEARCH_TO_HOST_WINDOW, SERVER_OWNED_ATTRS, build_search_result, read_attributes,
    read_key, session_host_pid, update_slot_counts)
from ac3server.notifications import (PARTICIPATION_JOINED_MINOR, PARTICIPATION_LEFT_MINOR,
    PARTICIPATION_NOTIFICATION_MAJOR)
from ac3server.players.identity import CONNECTED_PIDS, PLAYER_IDENTITIES
from ac3server.protocol.datatypes import read_string, read_u32, write_string
from ac3server.protocol.rmc import build_rmc_error_response, build_rmc_response


def _notify_session_participants(key, session, actor_pid, joining):
    """Tell the other participants that someone joined or left.

    A group (type id 2) has its own channel (gamesession/groups.py). A match gets the major-3 participation event,
    which is off unless `notify_participants` is set: its minor codes are unverified (notifications.py).
    """
    verb = "joined" if joining else "left"
    if key[0] == GROUP_SESSION_TYPE_ID:
        notify_group_members(
            key[1], session, actor_pid,
            GROUP_MEMBER_JOINED_MINOR if joining else GROUP_MEMBER_LEFT_MINOR,
            verb)
        return
    targets = []
    for pid in session.get("participants", []):
        if pid == actor_pid:
            continue
        info = PARTICIPANT_INFO.get(pid)
        if info and info.get("addr"):
            targets.append((pid, info["addr"]))
    if not targets:
        return
    if not config.NOTIFY_PARTICIPANTS:
        print(f"      (notify_participants is off - would have told "
              f"{len(targets)} participant(s) that {actor_pid} {verb})")
        return
    minor = PARTICIPATION_JOINED_MINOR if joining else PARTICIPATION_LEFT_MINOR
    ntype = PARTICIPATION_NOTIFICATION_MAJOR * 1000 + minor
    for pid, target_addr in targets:
        # as Ubisoft's server shaped a GameSession-category notification: the acting pid in pidSource and param1,
        # the session id in param2
        notifications.send_notification(
            target_addr, pid_source=actor_pid, ntype=ntype,
            param1=actor_pid, param2=key[1], param3=0,
            label=f"Participation notification ({verb}, minor {minor})")


def handle_gamesession_create_session(call_id, params, addr):     # m1
    type_id, attrs = 1, {}
    try:
        type_id, pos = read_u32(params, 0)
        attrs, pos = read_attributes(params, pos)
        if pos != len(params):
            print(f"[{addr}]   GameSession.CreateSession: {len(params) - pos} "
                  f"trailing byte(s) not consumed - layout may be incomplete")
    except Exception as e:
        print(f"[{addr}]   GameSession.CreateSession: could not parse request ({e})")
    session_id = NEXT_SESSION_ID[0]
    NEXT_SESSION_ID[0] += 1
    # a match is hosted right after the host's own search came back empty; a lobby is created cold and is never
    # offered to searchers (state.SEARCH_TO_HOST_WINDOW)
    advertised = (time.time() - LAST_SEARCH.get(addr, 0.0)) < SEARCH_TO_HOST_WINDOW
    session = {
        "type_id": type_id, "attributes": attrs, "host": addr,
        "participants": [], "private_participants": [], "urls": [],
        "advertised": advertised, "created": time.time(),
    }
    GAME_SESSIONS[session_id] = session
    update_slot_counts(session)        # adds the attributes the server owns (state.SERVER_OWNED_ATTRS)
    print(f"[{addr}]   GameSession.CreateSession: hosting session {session_id:#x} "
          f"(typeId={type_id}, {len(attrs)} attributes, "
          f"{len(SERVER_OWNED_ATTRS)} of them added by us)")
    resp = struct.pack('<II', type_id, session_id)
    return "GameSession.CreateSession", build_rmc_response(0x2A, call_id, 1, resp)


def handle_gamesession_update_session(call_id, params, addr):     # m2
    try:
        key, pos = read_key(params, 0)
        attrs, pos = read_attributes(params, pos)
        session = GAME_SESSIONS.get(key[1])
        if session is None:
            print(f"[{addr}]   GameSession.UpdateSession: unknown session {key[1]:#x}")
        else:
            session["attributes"].update(attrs)
            # The client may echo back a stale copy of the slot counts, so
            # re-assert ours from the participant list afterwards.
            update_slot_counts(session)
            print(f"[{addr}]   GameSession.UpdateSession {key[1]:#x}: "
                  f"{len(attrs)} attribute(s) updated")
    except Exception as e:
        print(f"[{addr}]   GameSession.UpdateSession: could not parse request ({e})")
    return "GameSession.UpdateSession", build_rmc_response(0x2A, call_id, 2, b"")


def handle_gamesession_add_participants(call_id, params, addr):   # m8
    try:
        key, pos = read_key(params, 0)
        public_count, pos = read_u32(params, pos)
        public = []
        for _ in range(public_count):
            pid, pos = read_u32(params, pos)
            public.append(pid)
        private = []
        if pos + 4 <= len(params):
            private_count, pos = read_u32(params, pos)
            for _ in range(private_count):
                pid, pos = read_u32(params, pos)
                private.append(pid)
        session = GAME_SESSIONS.get(key[1])
        added = []
        if session is not None:
            for pid in public + private:
                if pid not in session["participants"]:
                    session["participants"].append(pid)
                    added.append(pid)
            for pid in public + private:
                _leave_other_sessions(pid, key[1], session.get("type_id"))
            for pid in private:
                if pid not in session.setdefault("private_participants", []):
                    session["private_participants"].append(pid)
            # the first participant the host adds is the host itself; a
            # search result has to name that pid so a joiner knows who hosts
            if session.get("host_pid") is None and public:
                session["host_pid"] = public[0]
            update_slot_counts(session)
        _remember_participant(addr)
        print(f"[{addr}]   GameSession.AddParticipants {key[1]:#x}: "
              f"public={public} private={private}")
        if session is not None and key[0] == GROUP_SESSION_TYPE_ID:
            # A group's creator adds itself here (CreateGroupSessionMasterTask
            # ends in an AddParticipant); nobody else is in it yet, so that
            # case sends nothing. Anyone added later is announced.
            for pid in added:
                notify_group_members(key[1], session, pid,
                                     GROUP_MEMBER_JOINED_MINOR, "was added")
    except Exception as e:
        print(f"[{addr}]   GameSession.AddParticipants: could not parse request ({e})")
    return "GameSession.AddParticipants", build_rmc_response(0x2A, call_id, 8, b"")


def handle_gamesession_remove_participants(call_id, params, addr):    # m9
    try:
        key, pos = read_key(params, 0)
        count, pos = read_u32(params, pos)
        pids = []
        for _ in range(count):
            pid, pos = read_u32(params, pos)
            pids.append(pid)
        session = GAME_SESSIONS.get(key[1])
        removed = []
        if session is not None:
            removed = [p for p in session["participants"] if p in pids]
            now = time.time()
            for p in removed:                                   # their own LeaveSession may still come
                REMOVED_BY_HOST[(key[1], p)] = now
            session["participants"] = [p for p in session["participants"] if p not in pids]
            session["private_participants"] = [
                p for p in session.get("private_participants", []) if p not in pids]
            update_slot_counts(session)
        print(f"[{addr}]   GameSession.RemoveParticipants {key[1]:#x}: {pids}")
        if session is not None and key[0] == GROUP_SESSION_TYPE_ID:
            for pid in removed:
                notify_group_members(key[1], session, pid,
                                     GROUP_MEMBER_LEFT_MINOR, "was removed")
    except Exception as e:
        print(f"[{addr}]   GameSession.RemoveParticipants: could not parse request ({e})")
    return "GameSession.RemoveParticipants", build_rmc_response(0x2A, call_id, 9, b"")


def handle_gamesession_register_urls(call_id, params, addr):      # m21
    """The caller's own station URLs: the host's for its sessions, and everybody's for GetParticipants."""
    urls = []
    try:
        count, pos = read_u32(params, 0)
        for _ in range(count):
            url, pos = read_string(params, pos)
            urls.append(url)
    except Exception as e:
        print(f"[{addr}]   GameSession.RegisterURLs: could not parse request ({e})")
    for session in GAME_SESSIONS.values():
        if session["host"] == addr:
            session["urls"] = urls
    _remember_participant(addr, urls)
    print(f"[{addr}]   GameSession.RegisterURLs: {len(urls)} url(s) {urls}")
    return "GameSession.RegisterURLs", build_rmc_response(0x2A, call_id, 21, b"")


def _migrate_host(session_id, session, reason):
    """Hand a session to a remaining participant when its host departs, so it does not keep naming somebody gone."""
    remaining = list(session.get("participants", []))
    if not remaining:
        return None
    new_pid = remaining[0]
    if session.get("type_id") == GROUP_SESSION_TYPE_ID:
        # a group: the one the members' games elect themselves - each removes the leader and takes the LOWEST pid left
        # (FUN_00DD4CEF, cmova at 0x00DD4D58), and that member's game migrates the group (MigrateSession, below)
        new_pid = min(remaining)
    info = PARTICIPANT_INFO.get(new_pid, {})
    session["host_pid"] = new_pid
    if info.get("addr"):
        session["host"] = info["addr"]
        session["urls"] = info.get("urls", [])
    print(f"      session {session_id:#x} host migrated to "
          f"{info.get('name', new_pid)} ({reason})")
    return new_pid


def forget_stale_sessions(pid, addr):
    """Drop a freshly logged-in player from every session they are still listed in.

    A login is the one unambiguous "this player is in nothing" signal. Leaving, abandoning and disconnecting all
    depend on the client getting to say so, and a killed game or a restarted server never does.
    """
    stale = [sid for sid, s in GAME_SESSIONS.items()
             if pid in s.get("participants", []) or s.get("host") == addr]
    for sid in stale:
        session = GAME_SESSIONS[sid]
        if pid in session.get("participants", []):
            session["participants"].remove(pid)
        if pid in session.get("private_participants", []):
            session["private_participants"].remove(pid)
        if not session["participants"]:
            GAME_SESSIONS.pop(sid, None)
        else:
            update_slot_counts(session)
            new_host = None
            if session.get("host_pid") == pid or session.get("host") == addr:
                new_host = _migrate_host(sid, session, "previous host logged in again")
            group_member_left(sid, session, pid, new_host)
    if stale:
        print(f"[{addr}]   login clears {len(stale)} stale session(s) "
              f"{[hex(s) for s in stale]} - a client that just logged in "
              f"cannot still be in one")
    return stale


def _leave_other_sessions(pid, keep_session_id, type_id):
    """A player is in at most one session of a given type: out of any other of the same type.

    Without this the sessions a client moved on from pile up and SearchSessionsWithParticipants reports a friend in
    two games at once. Only the same type is touched: a player is legitimately in a group (type 2) and a match
    (type 1) together.
    """
    moved = []
    for other_id in list(GAME_SESSIONS):
        if other_id == keep_session_id:
            continue
        session = GAME_SESSIONS[other_id]
        if session.get("type_id") != type_id:
            continue
        if pid not in session.get("participants", []):
            continue
        session["participants"].remove(pid)
        if pid in session.get("private_participants", []):
            session["private_participants"].remove(pid)
        update_slot_counts(session)
        moved.append(other_id)
        if not session["participants"]:
            GAME_SESSIONS.pop(other_id, None)
        else:
            new_host = None
            if session.get("host_pid") == pid:
                new_host = _migrate_host(other_id, session,
                                         "previous host moved to another session")
            group_member_left(other_id, session, pid, new_host)
    if moved:
        print(f"      {pid} left {len(moved)} earlier type-{type_id} session(s) "
              f"{[hex(s) for s in moved]} - a player is only in one at a time")
    return moved


def release_session(session_id, addr, reason):
    """One participant leaves a session; the session itself ends only with the last of them.

    Ending it on the first abandon would break groups: the leader leaves and abandons the group session as a match
    starts, while the other members are still in it. acb-rdv does the same: remove the caller, delete when empty.
    """
    session = GAME_SESSIONS.get(session_id)
    if session is None:
        return "unknown session"
    pid = CONNECTED_PIDS.get(addr)
    was_host = session.get("host") == addr
    removed_here = pid is not None and pid in session.get("participants", [])
    if removed_here:
        session["participants"].remove(pid)
        if pid in session.get("private_participants", []):
            session["private_participants"].remove(pid)
        update_slot_counts(session)
    remaining = session.get("participants", [])
    if not remaining:
        GAME_SESSIONS.pop(session_id, None)
        return "removed, it was the last participant"
    new_host = None
    if was_host:
        new_host = _migrate_host(session_id, session, reason)
    # A leader abandons the group right after leaving it, so the pid is
    # usually gone already and the "left" event went out with the leave;
    # only the leadership change is news here.
    group_member_left(session_id, session, pid if removed_here else None, new_host)
    return (f"caller removed, {len(remaining)} participant(s) remain so the "
            f"session lives on")


def handle_gamesession_abandon_session(call_id, params, addr):    # m23
    try:
        key, pos = read_key(params, 0)
        outcome = release_session(key[1], addr, "abandoned by its host")
        print(f"[{addr}]   GameSession.AbandonSession {key[1]:#x}: {outcome}")
    except Exception as e:
        print(f"[{addr}]   GameSession.AbandonSession: could not parse request ({e})")
    return "GameSession.AbandonSession", build_rmc_response(0x2A, call_id, 23, b"")


def _remember_participant(addr, urls=None):
    """Index the caller's name, address and (when given) station URLs by pid (state.PARTICIPANT_INFO)."""
    identity = PLAYER_IDENTITIES.get(addr)
    pid = CONNECTED_PIDS.get(addr)
    if pid is None:
        return None
    record = PARTICIPANT_INFO.setdefault(
        pid, {"name": (identity or {}).get("name", f"player{pid}"),
              "urls": [], "addr": addr})
    if identity:
        record["name"] = identity["name"]
    record["addr"] = addr
    if urls is not None:
        record["urls"] = urls
    return record


def handle_gamesession_get_participants(call_id, params, addr):   # m11
    """Every participant with its name and station URLs; the requester is in its own list. Matches Ubisoft's reply
    byte for byte."""
    offset, max_count = 0, 0
    participants = []
    try:
        key, pos = read_key(params, 0)
        if pos + 8 <= len(params):
            offset, pos = read_u32(params, pos)
            max_count, pos = read_u32(params, pos)
        session = GAME_SESSIONS.get(key[1])
        if session is None:
            print(f"[{addr}]   GameSession.GetParticipants: unknown session "
                  f"{key[1]:#x} -> empty list")
        else:
            participants = list(session.get("participants", []))
    except Exception as e:
        print(f"[{addr}]   GameSession.GetParticipants: could not parse request ({e})")
    window = participants[offset:offset + max_count] if max_count else participants[offset:]
    resp = bytearray(struct.pack('<I', len(window)))
    for pid in window:
        info = PARTICIPANT_INFO.get(pid, {"name": f"player{pid}", "urls": []})
        resp += struct.pack('<I', pid)
        write_string(resp, info["name"])
        urls = info.get("urls", [])
        resp += struct.pack('<I', len(urls))
        for url in urls:
            write_string(resp, url)
    print(f"[{addr}]   GameSession.GetParticipants: {len(window)} of "
          f"{len(participants)} participant(s) (offset {offset}, max {max_count}): "
          f"{[PARTICIPANT_INFO.get(p, {}).get('name', p) for p in window]}")
    return "GameSession.GetParticipants", build_rmc_response(
        0x2A, call_id, 11, bytes(resp))


def handle_gamesession_get_session(call_id, params, addr):        # m6
    """The first step of every join, invited or not (TaskRDV2_ProbeSession): one search result for the key."""
    try:
        key, pos = read_key(params, 0)
    except Exception as e:
        print(f"[{addr}]   GameSession.GetSession: could not parse request ({e})")
        return "GameSession.GetSession", build_rmc_error_response(
            0x2A, call_id, QERROR_SESSION_INVALID_KEY)
    session = GAME_SESSIONS.get(key[1])
    if session is None:
        print(f"[{addr}]   GameSession.GetSession: unknown session {key[1]:#x} -> "
              f"error {QERROR_SESSION_INVALID_KEY:#010x}")
        return "GameSession.GetSession", build_rmc_error_response(
            0x2A, call_id, QERROR_SESSION_INVALID_KEY)
    print(f"[{addr}]   GameSession.GetSession {key[1]:#x}: host pid "
          f"{session.get('host_pid', 0)}, {len(session.get('urls', []))} url(s), "
          f"{len(session['attributes'])} attribute(s), "
          f"{len(session.get('participants', []))} participant(s)")
    return "GameSession.GetSession", build_rmc_response(
        0x2A, call_id, 6, build_search_result(key[1], session))


def handle_gamesession_join_session(call_id, params, addr):       # m22
    """The joiner enters the session; the DDL gives it no out parameters. We add the caller to the participants
    ourselves rather than wait for the host's AddParticipants: a list one too full shows a name, one too empty tells
    the host nothing."""
    try:
        key, pos = read_key(params, 0)
        session = GAME_SESSIONS.get(key[1])
        pid = CONNECTED_PIDS.get(addr)
        if session is None:
            print(f"[{addr}]   GameSession.JoinSession: unknown session {key[1]:#x} "
                  f"-> {QERROR_SESSION_INVALID_KEY:#010x}")
            return "GameSession.JoinSession", build_rmc_error_response(
                0x2A, call_id, QERROR_SESSION_INVALID_KEY)
        _remember_participant(addr)
        if pid is not None:
            _leave_other_sessions(pid, key[1], session.get("type_id"))
            if pid not in session["participants"]:
                session["participants"].append(pid)
        update_slot_counts(session)
        name = PARTICIPANT_INFO.get(pid, {}).get("name", pid)
        print(f"[{addr}]   GameSession.JoinSession {key[1]:#x}: {name} joined, "
              f"{len(session['participants'])} participant(s) now - empty reply "
              f"(the DDL declares no out params)")
        if pid is not None:
            _notify_session_participants(key, session, pid, joining=True)
    except Exception as e:
        print(f"[{addr}]   GameSession.JoinSession: could not parse request ({e})")
    return "GameSession.JoinSession", build_rmc_response(0x2A, call_id, 22, b"")


def handle_gamesession_leave_session(call_id, params, addr):      # m5
    """A participant leaves: an empty success (the DDL). Anyone else gets 0x8012000A, the error Ubisoft's server
    answered with for a session the caller was no longer in. A player the host has just removed gets a success
    instead (state.REMOVED_BY_HOST)."""
    try:
        key, pos = read_key(params, 0)
        session = GAME_SESSIONS.get(key[1])
        pid = CONNECTED_PIDS.get(addr)
        removed_at = REMOVED_BY_HOST.pop((key[1], pid), None)
        if removed_at is not None and time.time() - removed_at <= REMOVED_BY_HOST_TTL and (
                session is None or pid not in session.get("participants", [])):
            print(f"[{addr}]   GameSession.LeaveSession {key[1]:#x}: the host had removed them already "
                  f"({time.time() - removed_at:.1f} s ago) - a success")
            return "GameSession.LeaveSession", build_rmc_response(0x2A, call_id, 5, b"")
        if session is None or pid is None or pid not in session.get("participants", []):
            why = ("unknown session" if session is None else
                   "caller is not a participant")
            print(f"[{addr}]   GameSession.LeaveSession {key[1]:#x}: {why} -> "
                  f"error {QERROR_SESSION_NOT_PARTICIPANT:#010x}, as Ubisoft's "
                  f"server answered")
            return "GameSession.LeaveSession", build_rmc_error_response(
                0x2A, call_id, QERROR_SESSION_NOT_PARTICIPANT)
        session["participants"].remove(pid)
        if pid in session.get("private_participants", []):
            session["private_participants"].remove(pid)
        update_slot_counts(session)
        name = PARTICIPANT_INFO.get(pid, {}).get("name", pid)
        print(f"[{addr}]   GameSession.LeaveSession {key[1]:#x}: {name} left, "
              f"{len(session['participants'])} participant(s) remain")
        _notify_session_participants(key, session, pid, joining=False)
    except Exception as e:
        print(f"[{addr}]   GameSession.LeaveSession: could not parse request ({e})")
    return "GameSession.LeaveSession", build_rmc_response(0x2A, call_id, 5, b"")


def handle_gamesession_migrate_session(call_id, params, addr):    # m4
    """A key in, a different key out: the same session - attributes, URLs, participants - under a new id. The old id
    is retired so a stale search result cannot bring it back. A GROUP migrated by one of its members is that member
    taking it over: the same id, the caller its host, the others told (`_migrate_group`)."""
    try:
        key, pos = read_key(params, 0)
    except Exception as e:
        print(f"[{addr}]   GameSession.MigrateSession: could not parse request ({e})")
        return "GameSession.MigrateSession", build_rmc_error_response(
            0x2A, call_id, QERROR_SESSION_INVALID_KEY)
    group = GAME_SESSIONS.get(key[1])
    caller = CONNECTED_PIDS.get(addr)
    if group is not None and group.get("type_id") == GROUP_SESSION_TYPE_ID and caller in group.get("participants", []):
        return _migrate_group(call_id, key, group, caller, addr)
    session = GAME_SESSIONS.pop(key[1], None)
    if session is None:
        print(f"[{addr}]   GameSession.MigrateSession: unknown session {key[1]:#x} -> "
              f"error {QERROR_SESSION_INVALID_KEY:#010x}")
        return "GameSession.MigrateSession", build_rmc_error_response(
            0x2A, call_id, QERROR_SESSION_INVALID_KEY)
    new_id = NEXT_SESSION_ID[0]
    NEXT_SESSION_ID[0] += 1
    GAME_SESSIONS[new_id] = session
    print(f"[{addr}]   GameSession.MigrateSession {key[1]:#x} -> {new_id:#x} "
          f"(attributes, urls and {len(session.get('participants', []))} "
          f"participant(s) carried over)")
    return "GameSession.MigrateSession", build_rmc_response(
        0x2A, call_id, 4, struct.pack('<II', key[0], new_id))


def _migrate_group(call_id, key, session, pid, addr):
    """MigrateSession on a GROUP by one of its members: that member's game has taken the group over.

    The game's TaskRDV2_MigrateHost (from GroupService::migrateHostSession 0x0116B760, run by the online manager's
    "take the group over" 0x00DD46E3 - after electing itself when the leader left, or on a leadership handover) sends
    this and, on success, marks its group as its own (owner flag GroupService+0x16D, the key we return,
    setHost(itself)). The other members learn it only from us: minor 1 with pidSource = the new host, param1 = the
    session id (notify_group_leader). The id stays the same so every member's key stays valid; a new id would leave
    them on one we had retired."""
    old = session_host_pid(session)
    info = PARTICIPANT_INFO.get(pid, {})
    session["host_pid"] = pid
    session["host"] = addr
    if info.get("urls"):
        session["urls"] = info["urls"]
    name = info.get("name", pid)
    print(f"[{addr}]   GameSession.MigrateSession group {key[1]:#x}: {name} takes the group over "
          f"(the host was {PARTICIPANT_INFO.get(old, {}).get('name', old)}); the id is kept, the others are told")
    notify_group_leader(key[1], session, pid)
    return "GameSession.MigrateSession", build_rmc_response(0x2A, call_id, 4, struct.pack('<II', key[0], key[1]))
