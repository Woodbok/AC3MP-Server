"""The session layer, offline: matchmaking, groups, invitations, friends and presence, driven through the real
handlers with simulated players.

What it verifies: the friends list and joining a friend, a login clearing stale sessions, no session leaks, a group's
member events, session lifetime and host migration, the group relay, SessionType filtering, the invitation push,
presence, the statistics service, the game settings patches, the search result's attributes, and a whole host /
search / join / leave run between two simulated clients.

It writes nothing outside temporary folders.

    python tests/ac3_session_selftest.py
"""
import importlib.util
import os
import struct
import sys
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "cxb"))

import server_modules  # noqa: E402

ps = server_modules.load(quiet=False)

GAMESESSION = 0x2A

FAILURES = []


def check(name, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {name}"
          + (f" - {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)






def read_participants(params):
    """Decode a GetParticipants reply with the layout we also write."""
    count, pos = ps.read_u32(params, 0)
    out = []
    for _ in range(count):
        pid, pos = ps.read_u32(params, pos)
        name, pos = ps.read_string(params, pos)
        url_count, pos = ps.read_u32(params, pos)
        urls = []
        for _ in range(url_count):
            url, pos = ps.read_string(params, pos)
            urls.append(url)
        out.append((pid, name, urls))
    return out, pos


def build_participants(entries):
    """Rebuild the reply body exactly as handle_gamesession_get_participants
    does, from the same per-participant record."""
    body = bytearray(struct.pack('<I', len(entries)))
    for pid, name, urls in entries:
        body += struct.pack('<I', pid)
        ps.write_string(body, name)
        body += struct.pack('<I', len(urls))
        for url in urls:
            ps.write_string(body, url)
    return bytes(body)


# A real CreateSession request as the client sends it. It lacks attributes
# 0x05, 0x06 and 0x6F, so the test exercises the server adding them rather
# than an idealised request.
LIVE_CREATE_SESSION_PACKET = bytes.fromhex(
    "99000000aa5e03000001000000"
    "0100000011000000"
    "0700000000000000" "0300000008000000" "0400000000000000"
    "6c00000002000000" "7400000001000000" "7500000004000000"
    "6b00000001000000" "6e00000000000000" "6700000001000000"
    "7000000000000000" "6500000034e1c311" "6400000048b9bbb7"
    "7200000001000000" "7700000001000000" "7100000000000000"
    "7300000001000000" "7800000000000000")

# Every attribute id present in all fourteen non-empty session records
# Ubisoft's server returned. The server must add the three the client never
# sends, or the joining game crashes.
UBISOFT_ATTR_IDS = {0x03, 0x04, 0x05, 0x06, 0x07, 0x64, 0x65, 0x67, 0x6B,
                    0x6C, 0x6E, 0x6F, 0x70, 0x71, 0x72, 0x73, 0x74, 0x75,
                    0x77, 0x78}


def read_friend_records(params):
    """Decode a GetDetailedList reply.

    FriendData, per AC3's own embedded DDL and acb-rdv's writer, which agree
    field for field: pid, name, relationship byte, details, status string.
    Note the status is a STRING - the sibling RelationshipData type has a
    byte there, and confusing the two desynchronises the whole list.
    """
    count, pos = ps.read_u32(params, 0)
    out = []
    for _ in range(count):
        pid, pos = ps.read_u32(params, pos)
        name, pos = ps.read_string(params, pos)
        relationship = params[pos]
        pos += 1
        details, pos = ps.read_u32(params, pos)
        status, pos = ps.read_string(params, pos)
        out.append((pid, name, relationship, details, status))
    return out, pos


def fresh_friendships():
    """Start a check with no friendships, under the default "requests" policy.

    The server keeps its friendship table in memory and reads friends.json
    again only when that file changes, so a fresh temp data dir alone would
    still carry the previous check's friendships; and the policy comes from
    the local server_config.json, which must not decide these checks.
    """
    ps.FRIENDS_POLICY = "requests"
    ps.FRIENDSHIPS.clear()
    del ps._FRIENDSHIPS_LOADED[:]
    ps._FRIENDSHIPS_MTIME[0] = None


def check_friends_list():
    """The friends list that replaces Uplay's, so players can be invited.

    Its members are the server's own friendships (the default "requests"
    policy): made by a friend request, both ways at once.
    """
    print("\nFriends list (0x14 m12): the server's own friendships")
    import tempfile
    ps.DATA_DIR = tempfile.mkdtemp()
    ps.PLAYER_DIRECTORY.clear()
    ps.PLAYER_IDENTITIES.clear()
    fresh_friendships()
    a_addr, b_addr = ("10.5.5.1", 3074), ("10.5.5.2", 3074)
    a = ps.resolve_player_identity(a_addr, "playerone")
    b = ps.resolve_player_identity(b_addr, "playertwo")
    ps.CONNECTED_PIDS[a_addr] = a["pid"]
    ps.CONNECTED_PIDS[b_addr] = b["pid"]

    # Playing on the same server does not make two players friends...
    _label, resp = ps.handle_friends_get_detailed_list(5, bytes([1, 0]), a_addr)
    ok, _err, _p, _c, _m, params = ps.parse_rmc_response(resp)
    check("two players are not friends before either asks",
          params == b"\x00\x00\x00\x00", params.hex())
    # ...unless the policy is "everyone".
    ps.FRIENDS_POLICY = "everyone"
    _label, resp = ps.handle_friends_get_detailed_list(6, bytes([1, 0]), a_addr)
    ok, _err, _p, _c, _m, params = ps.parse_rmc_response(resp)
    records, _pos = read_friend_records(params)
    check("under the 'everyone' policy they are, with no request",
          [r[0] for r in records] == [b["pid"]], str([r[1] for r in records]))
    ps.FRIENDS_POLICY = "requests"

    # What a friend request records (a Ubisoft Connect friend named in m3,
    # or Friends AddFriend), in the temp data dir's friends.json.
    ps.add_friendship(a["pid"], b["pid"])

    _label, resp = ps.handle_friends_get_detailed_list(1, bytes([1, 0]), a_addr)
    ok, _err, _p, _c, _m, params = ps.parse_rmc_response(resp)
    records, pos = read_friend_records(params)
    check("the reply decodes with nothing left over", pos == len(params),
          f"consumed {pos} of {len(params)}")
    check("the other player appears as a friend", len(records) == 1,
          str([r[1] for r in records]))
    if records:
        pid, name, relationship, details, status = records[0]
        check("the friend is the other player", pid == b["pid"] and name == b["name"],
              f"{name} ({pid})")
        check("relationship is 1 = Friend", relationship == 1, str(relationship))
        check("details is zero", details == 0, str(details))
        check("a connected friend reads as Online", status == "Online", status)

    # Disconnect B: still listed, now offline.
    del ps.PLAYER_IDENTITIES[b_addr]
    _label, resp = ps.handle_friends_get_detailed_list(2, bytes([1, 0]), a_addr)
    ok, _err, _p, _c, _m, params = ps.parse_rmc_response(resp)
    records, pos = read_friend_records(params)
    check("an offline friend is still listed", len(records) == 1, str(len(records)))
    check("and reads as Offline", records and records[0][4] == "Offline",
          records[0][4] if records else "none")
    check("the offline reply also decodes exactly", pos == len(params))

    # The client asks once per relationship type; only friends has content.
    _label, resp = ps.handle_friends_get_detailed_list(3, bytes([2, 0]), a_addr)
    ok, _err, _p, _c, _m, params = ps.parse_rmc_response(resp)
    check("the pending query is empty, not a copy of the friends list",
          params == b"\x00\x00\x00\x00", params.hex())

    # Nobody is their own friend, and the friendship is mutual.
    _label, resp = ps.handle_friends_get_detailed_list(4, bytes([1, 0]), b_addr)
    ok, _err, _p, _c, _m, params = ps.parse_rmc_response(resp)
    records, _pos = read_friend_records(params)
    check("a player never sees themselves in their own list",
          all(r[0] != b["pid"] for r in records), str([r[1] for r in records]))
    check("the other player lists the asker back",
          [r[0] for r in records] == [a["pid"]], str([r[1] for r in records]))


def check_login_clears_stale_sessions():
    """A fresh login means the player is in nothing.

    Sessions a client never abandoned (it went away without saying so)
    would otherwise keep reporting the player as hosting. A login is the
    one unambiguous signal the protocol gives that a player is in nothing.
    """
    print("")
    print("Login clears stale sessions")
    import tempfile
    ps.DATA_DIR = tempfile.mkdtemp()
    ps.PLAYER_DIRECTORY.clear()
    ps.PLAYER_IDENTITIES.clear()
    ps.GAME_SESSIONS.clear()
    fresh_friendships()
    a_addr, b_addr = ("10.0.9.1", 3074), ("10.0.9.2", 3074)
    a = ps.resolve_player_identity(a_addr, "asker")
    b = ps.resolve_player_identity(b_addr, "ghost")
    ps.CONNECTED_PIDS[a_addr] = a["pid"]
    ps.CONNECTED_PIDS[b_addr] = b["pid"]

    # A game and a group, never abandoned. The
    # game is hosted the way a client does it, right after its own search.
    for type_id in (1, 2):
        create = struct.pack("<III", type_id, 1, ps.ATTR_SESSION_TYPE)
        create += struct.pack("<I", 2)
        ps.handle_gamesession_search_sessions(0, struct.pack("<III", 1, 3, 0), b_addr)
        _label, resp = ps.handle_gamesession_create_session(1, create, b_addr)
        _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
        t, session_id = struct.unpack("<II", params)
        key = struct.pack("<II", t, session_id)
        ps.handle_gamesession_add_participants(
            2, key + struct.pack("<II", 1, b["pid"]) + struct.pack("<I", 0), b_addr)
    check("two stale sessions exist to begin with",
          len(ps.GAME_SESSIONS) == 2, str(len(ps.GAME_SESSIONS)))

    names = bytearray(struct.pack("<I", 1))
    ps.write_string(names, b["name"])

    def in_session_flag():
        _label, resp = ps.handle_contacts_game_sessions(3, bytes(names), a_addr)
        _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
        count, pos = ps.read_u32(params, 0)
        if count == 0:
            return None
        _name, pos = ps.read_string(params, pos)
        _pid, pos = ps.read_u32(params, pos)
        return params[pos]

    check("before the login the friend is wrongly reported in a session",
          in_session_flag() == 1, str(in_session_flag()))

    ps.forget_stale_sessions(b["pid"], b_addr)
    check("logging in clears every stale session",
          len(ps.GAME_SESSIONS) == 0, str(len(ps.GAME_SESSIONS)))
    check("and the friend is reported as not in a session",
          in_session_flag() == 0, str(in_session_flag()))

    _label, resp = ps.handle_gamesession_search_with_participants(
        4, struct.pack("<III", 1, 1, b["pid"]), a_addr)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    check("and no session is offered for them any more",
          struct.unpack_from("<I", params, 0)[0] == 0)


def check_no_session_leak():
    """A player is in ONE session of a type, and disconnecting clears them out.

    Clients do not abandon every session they create; without this,
    SearchSessionsWithParticipants could report a friend in two games at
    once, which leaves the friends row in a state where invite never appears.
    """
    print("")
    print("Session leaks: one session per type, and disconnect cleans up")

    class SilentSocket:
        def sendto(self, *_):
            pass

        def bind(self, *_):
            pass

        def settimeout(self, *_):
            pass

    import tempfile
    ps.DATA_DIR = tempfile.mkdtemp()
    ps.PLAYER_DIRECTORY.clear()
    ps.PLAYER_IDENTITIES.clear()
    ps.GAME_SESSIONS.clear()
    a_addr, b_addr = ("10.0.5.1", 3074), ("10.0.5.2", 3074)
    a = ps.resolve_player_identity(a_addr, "watcher2")
    b = ps.resolve_player_identity(b_addr, "hopper")
    ps.CONNECTED_PIDS[a_addr] = a["pid"]
    ps.CONNECTED_PIDS[b_addr] = b["pid"]

    def host_new(addr, type_id):
        create = struct.pack("<III", type_id, 1, ps.ATTR_SESSION_TYPE)
        create += struct.pack("<I", 2)
        _label, resp = ps.handle_gamesession_create_session(1, create, addr)
        _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
        t, session_id = struct.unpack("<II", params)
        key = struct.pack("<II", t, session_id)
        ps.handle_gamesession_add_participants(
            2, key + struct.pack("<II", 1, ps.CONNECTED_PIDS[addr])
            + struct.pack("<I", 0), addr)
        return session_id, key

    # The client creates a fresh match for every search attempt.
    host_new(b_addr, 1)
    host_new(b_addr, 1)
    third, third_key = host_new(b_addr, 1)
    check("three matches in a row collapse to the newest one",
          list(ps.GAME_SESSIONS) == [third], str([hex(s) for s in ps.GAME_SESSIONS]))

    _label, resp = ps.handle_gamesession_search_with_participants(
        3, struct.pack("<III", 1, 1, b["pid"]), a_addr)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    count = struct.unpack_from("<I", params, 0)[0]
    check("a friend is reported in exactly one game, never two",
          count == 1, str(count))

    # A group is a different type, so it must coexist with the match.
    group_id, _gk = host_new(b_addr, 2)
    check("a group and a match coexist", group_id in ps.GAME_SESSIONS
          and third in ps.GAME_SESSIONS, str([hex(s) for s in ps.GAME_SESSIONS]))

    # Disconnect must clear the player out of everything, hosted or not.
    ps.handle_gamesession_join_session(4, third_key, a_addr)
    server = ps.PortServer(0, ps.SECURE_DISPATCH, "secure", on_disconnect=ps.player_disconnected)
    server.sock = SilentSocket()
    server.drop_connection_state(b_addr)
    check("the emptied group is removed on disconnect",
          group_id not in ps.GAME_SESSIONS)
    check("the match the other player is still in survives",
          third in ps.GAME_SESSIONS)
    check("and the connection's pid and name are forgotten (/status counts only who is online)",
          b_addr not in ps.CONNECTED_PIDS and b_addr not in ps.CONNECTED_USERS
          and b["pid"] not in ps.online_pids())
    if third in ps.GAME_SESSIONS:
        survivor = ps.GAME_SESSIONS[third]
        check("the departed player is gone from its participant list",
              survivor["participants"] == [a["pid"]],
              str(survivor["participants"]))
        check("and the host migrated to whoever is left",
              survivor.get("host_pid") == a["pid"],
              str(survivor.get("host_pid")))


def check_group_events():
    """A GROUP tells its members about each other on its own channel.

    Games get the opt-in major-3 participation event. A group (key type
    id 2) gets the major-7 events the client's own group consumer parses:
    minor 3 "member joined" (the leader has no other way to learn it has a
    member, and it dissolves a group with fewer than two known members),
    minor 2 "member left", minor 1 "leader is now pidSource". Fields follow
    Ubisoft's 7002: subject pid in pidSource and param1, session id in
    param2, empty string, key type id in param3.
    """
    print("")
    print("Group events: games get major 3 (opt-in), groups get major 7")

    class RecordingSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append((data, addr))

        def bind(self, *_):
            pass

        def settimeout(self, *_):
            pass

    import tempfile
    ps.DATA_DIR = tempfile.mkdtemp()
    ps.PLAYER_DIRECTORY.clear()
    ps.PLAYER_IDENTITIES.clear()
    ps.GAME_SESSIONS.clear()
    was = ps.NOTIFY_PARTICIPANTS
    was_group = ps.NOTIFY_GROUP_EVENTS
    ps.NOTIFY_PARTICIPANTS = True
    ps.NOTIFY_GROUP_EVENTS = True
    server = ps.PortServer(0, ps.SECURE_DISPATCH, "notify-test")
    server.sock = RecordingSocket()
    ps.SECURE_SERVER[0] = server

    leader, member = ("10.1.2.1", 3074), ("10.1.2.2", 3074)
    third = ("10.1.2.3", 3074)
    a = ps.resolve_player_identity(leader, "notifyleader")
    b = ps.resolve_player_identity(member, "notifymember")
    c = ps.resolve_player_identity(third, "notifythird")
    ps.CONNECTED_PIDS[leader] = a["pid"]
    ps.CONNECTED_PIDS[member] = b["pid"]
    ps.CONNECTED_PIDS[third] = c["pid"]

    class FakePacket:
        pass

    for addr in (leader, member, third):
        pkt = FakePacket()
        pkt.src_vport = ps.VPort(stream_type=3, port=15)
        pkt.dst_vport = ps.VPort(stream_type=3, port=1)
        pkt.session_id = 0x4F
        server._remember_conn((addr, 15), pkt, addr)

    def pushes():
        out = []
        for data, dest in server.sock.sent:
            payload = ps.PrudpPacket.parse(data).payload
            if not payload:
                continue
            _s, _req, proto, _call, method, params = ps.parse_rmc_request(payload)
            source, pos = ps.read_u32(params, 0)
            ntype, pos = ps.read_u32(params, pos)
            p1, pos = ps.read_u32(params, pos)
            p2, pos = ps.read_u32(params, pos)
            text, pos = ps.read_string(params, pos)
            p3, pos = ps.read_u32(params, pos)
            out.append(dict(dest=dest, proto=proto, method=method, source=source,
                            ntype=ntype, p1=p1, p2=p2, text=text, p3=p3,
                            tail=len(params) - pos))
        return out

    def create(type_id):
        req = struct.pack("<III", type_id, 1, ps.ATTR_SESSION_TYPE)
        req += struct.pack("<I", 2)
        _label, resp = ps.handle_gamesession_create_session(1, req, leader)
        _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
        t, session_id = struct.unpack("<II", params)
        key = struct.pack("<II", t, session_id)
        ps.handle_gamesession_add_participants(
            2, key + struct.pack("<II", 1, a["pid"]) + struct.pack("<I", 0), leader)
        return key, session_id

    # A game: the opt-in major-3 event, unchanged.
    key, _sid = create(1)
    server.sock.sent = []
    ps.handle_gamesession_join_session(3, key, member)
    ps.handle_gamesession_leave_session(4, key, leader)
    game = pushes()
    check("a GAME still notifies on join and leave", len(game) == 2, str(len(game)))
    check("and those are major-3 participation events",
          all(p["ntype"] // 1000 == 3 for p in game),
          str([p["ntype"] for p in game]))

    # A group of two: join tells the leader, leave tells the member.
    key, sid = create(2)
    server.sock.sent = []
    ps.handle_gamesession_join_session(3, key, member)
    joined = pushes()
    check("a member joining a GROUP pushes exactly one event",
          len(joined) == 1, str(len(joined)))
    if joined:
        ev = joined[0]
        check("it goes to the leader, not the joiner", ev["dest"] == leader, str(ev["dest"]))
        check("on the notification protocol, method 1",
              ev["proto"] == 0x0E and ev["method"] == 1, hex(ev["proto"]))
        check("type 7003 = major 7 group, minor 3 member joined",
              ev["ntype"] == 7003, str(ev["ntype"]))
        check("param1 is the joiner's pid - the field the consumer reads",
              ev["p1"] == b["pid"], str(ev["p1"]))
        check("pidSource is the joiner's pid too, as Ubisoft's 7002 places it",
              ev["source"] == b["pid"], str(ev["source"]))
        check("param2 is the group session id", ev["p2"] == sid, hex(ev["p2"]))
        check("param3 is 2, the key type the consumer gates on",
              ev["p3"] == 2, str(ev["p3"]))
        check("the string is empty and nothing trails",
              ev["text"] == "" and ev["tail"] == 0, repr(ev["text"]))

    server.sock.sent = []
    ps.handle_gamesession_leave_session(4, key, leader)
    left = pushes()
    check("the leader leaving pushes exactly one event", len(left) == 1, str(len(left)))
    if left:
        ev = left[0]
        check("it goes to the remaining member", ev["dest"] == member, str(ev["dest"]))
        check("type 7002 = member left, about the leader",
              ev["ntype"] == 7002 and ev["p1"] == a["pid"] and ev["source"] == a["pid"],
              f"{ev['ntype']} p1={ev['p1']}")
        check("param2 session id, param3 = 2", ev["p2"] == sid and ev["p3"] == 2,
              f"{ev['p2']:#x}/{ev['p3']}")

    server.sock.sent = []
    ps.handle_gamesession_abandon_session(5, key, leader)
    after = pushes()
    check("the abandon that follows a leave repeats nothing: the leaver was "
          "already gone and the sole remaining member is the new leader",
          len(after) == 0, str([(p["ntype"], p["dest"]) for p in after]))

    # A group of three: the leader abandoning migrates the lead to the
    # member the games elect themselves (the lowest pid, FUN_00DD4CEF), and
    # the member who did not inherit it is told who did.
    ps.GAME_SESSIONS.clear()
    key, sid = create(2)
    ps.handle_gamesession_join_session(3, key, member)
    ps.handle_gamesession_join_session(4, key, third)
    # the recording socket never acks, and outgoing DATA is windowed per
    # connection, so drop the earlier scenarios' unacked pushes first
    server.pending.clear()
    server.send_queue.clear()
    server.sock.sent = []
    ps.handle_gamesession_abandon_session(5, key, leader)
    ev3 = pushes()
    lefts = [p for p in ev3 if p["ntype"] == 7002]
    leads = [p for p in ev3 if p["ntype"] == 7001]
    elected, other = ((b, third) if b["pid"] < c["pid"] else (c, member))
    check("leader abandoning a group of three: both members hear it left",
          len(lefts) == 2 and {p["dest"] for p in lefts} == {member, third},
          str([(p["ntype"], p["dest"]) for p in ev3]))
    check("exactly one leader-changed event, to the member who is NOT the new leader",
          len(leads) == 1 and leads[0]["dest"] == other, str([p["dest"] for p in leads]))
    if leads:
        check("it names the new leader (the lowest pid left) in pidSource and the group's session id in param1 "
              "(GroupService+0x168)",
              leads[0]["source"] == elected["pid"] and leads[0]["p1"] == sid and leads[0]["p2"] == sid,
              f"{leads[0]['source']} p1={leads[0]['p1']:#x}")
        check("the session now names that member as host",
              ps.GAME_SESSIONS[sid].get("host_pid") == elected["pid"])

    # The elected member's game migrates the group (MigrateSession):
    # the same key back, the caller the host, the others told (minor 1)
    server.pending.clear()
    server.send_queue.clear()
    server.sock.sent = []
    elected_addr = member if elected is b else third
    _label, resp = ps.handle_gamesession_migrate_session(6, key, elected_addr)
    ok, _err, _p, _c, _m, out = ps.parse_rmc_response(resp)
    ev4 = pushes()
    check("a member migrating its group gets the SAME key back (every member's key stays valid)",
          ok and out == key and sid in ps.GAME_SESSIONS, out.hex() if ok else "error")
    check("... and is the group's host",
          ps.GAME_SESSIONS[sid].get("host_pid") == elected["pid"] and ps.GAME_SESSIONS[sid].get("host") == elected_addr)
    check("... and the other member is told: 7001, pidSource the new host, param1 the session id",
          len(ev4) == 1 and ev4[0]["ntype"] == 7001 and ev4[0]["dest"] == other and
          ev4[0]["source"] == elected["pid"] and ev4[0]["p1"] == sid and ev4[0]["p3"] == 2,
          str([(p["ntype"], p["dest"], p["source"]) for p in ev4]))
    # a member who is not the host migrating (a handover): it becomes the host
    server.pending.clear()
    server.send_queue.clear()
    server.sock.sent = []
    _label, resp = ps.handle_gamesession_migrate_session(7, key, other)
    ok, _err, _p, _c, _m, out = ps.parse_rmc_response(resp)
    ev5 = pushes()
    check("the other member taking the group over: the same key, it hosts, the old host told",
          ok and out == key and ps.GAME_SESSIONS[sid].get("host") == other and
          [(p["ntype"], p["dest"], p["p1"]) for p in ev5] == [(7001, elected_addr, sid)],
          str([(p["ntype"], p["dest"]) for p in ev5]))

    # "Return to the lobby": after the leader's type-2 broadcast, a member
    # leaving the group is expected and must NOT be reported, or the
    # leader dissolves the group.
    ps.GAME_SESSIONS.clear()
    key, sid = create(2)
    ps.handle_gamesession_join_session(3, key, member)
    # the recording socket never acks, and outgoing DATA is windowed per
    # connection, so drop the earlier scenarios' unacked pushes first
    server.pending.clear()
    server.send_queue.clear()
    server.sock.sent = []
    msg = bytearray(key)
    ps.write_string(msg, "02000000" + "00" * 12)
    ps.handle_gamesession_send_text_message(4, bytes(msg), leader)
    relayed = pushes()
    check("the type-2 broadcast is relayed as 7012 to the member and echoed to the leader",
          len(relayed) == 2 and all(p["ntype"] == 7012 for p in relayed)
          and {p["dest"] for p in relayed} == {member, leader},
          str([(p["ntype"], p["dest"]) for p in relayed]))
    server.sock.sent = []
    ps.handle_gamesession_leave_session(5, key, member)
    check("the member leaving after 'return to lobby' is NOT reported to the leader",
          len(pushes()) == 0, str([(p["ntype"], p["dest"]) for p in pushes()]))
    check("the leader is still the group's only participant",
          ps.GAME_SESSIONS[sid]["participants"] == [a["pid"]])
    ps.GAME_SESSIONS[sid]["returning_to_lobby"] = 0.0
    ps.handle_gamesession_join_session(6, key, member)
    server.pending.clear()
    server.send_queue.clear()
    server.sock.sent = []
    ps.handle_gamesession_leave_session(7, key, member)
    check("once the grace period is over a leave is reported again",
          len([p for p in pushes() if p["ntype"] == 7002]) == 1)

    # Switched off, nothing goes out and the group still lives on.
    ps.NOTIFY_GROUP_EVENTS = False
    server.sock.sent = []
    ps.handle_gamesession_join_session(6, key, leader)
    check("with notify_group_events off nothing is pushed",
          len(pushes()) == 0, str(len(pushes())))
    ps.NOTIFY_PARTICIPANTS = was
    ps.NOTIFY_GROUP_EVENTS = was_group


def check_session_lifetime():
    """A session dies only when its LAST participant leaves.

    The leader tears the group session down the moment a match starts -
    LeaveSession then AbandonSession - while the other member is still in
    it; destroying the session then would leave the two players in the same
    lobby but no longer grouped. As in acb-rdv: remove the caller, delete
    only when no participants remain.
    """
    print("")
    print("Session lifetime: a group survives its leader leaving")
    import tempfile
    ps.DATA_DIR = tempfile.mkdtemp()
    ps.PLAYER_DIRECTORY.clear()
    ps.PLAYER_IDENTITIES.clear()
    ps.GAME_SESSIONS.clear()
    leader, member = ("10.2.2.1", 3074), ("10.2.2.2", 3074)
    a = ps.resolve_player_identity(leader, "theleader")
    b = ps.resolve_player_identity(member, "themember")
    ps.CONNECTED_PIDS[leader] = a["pid"]
    ps.CONNECTED_PIDS[member] = b["pid"]

    create = struct.pack("<III", 2, 1, ps.ATTR_SESSION_TYPE) + struct.pack("<I", 2)
    _label, resp = ps.handle_gamesession_create_session(1, create, leader)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    type_id, session_id = struct.unpack("<II", params)
    key = struct.pack("<II", type_id, session_id)
    ps.handle_gamesession_add_participants(
        2, key + struct.pack("<II", 1, a["pid"]) + struct.pack("<I", 0), leader)
    ps.handle_gamesession_join_session(3, key, member)

    # Exactly what the leader's client does when a match starts.
    ps.handle_gamesession_leave_session(4, key, leader)
    ps.handle_gamesession_abandon_session(5, key, leader)
    check("the group survives the leader abandoning it",
          session_id in ps.GAME_SESSIONS, "gone" if session_id not in
          ps.GAME_SESSIONS else "still there")
    if session_id not in ps.GAME_SESSIONS:
        return
    session = ps.GAME_SESSIONS[session_id]
    check("only the remaining member is left in it",
          session["participants"] == [b["pid"]], str(session["participants"]))
    check("the host migrates to them rather than naming somebody gone",
          session.get("host_pid") == b["pid"] and session.get("host") == member,
          str(session.get("host_pid")))

    # The session still exists, so the member's own leave succeeds.
    _label, resp = ps.handle_gamesession_leave_session(6, key, member)
    ok, err, _p, _c, _m, _out = ps.parse_rmc_response(resp)
    check("the remaining member can then leave cleanly", ok is True,
          "error %#010x" % err)
    ps.handle_gamesession_abandon_session(7, key, member)
    check("and the session is finally removed when it is empty",
          session_id not in ps.GAME_SESSIONS)


def check_group_relay():
    """A group broadcast must be relayed to the other members.

    The client's notification handler sends major-7 events with a minor
    other than 5, 7, 8 or 9 to its group consumer, which gates on
    param3 == 2 and only hex-decodes the string for minor 12. So the relay
    is type 7012 with param3 taken from the session key's type id, and the
    string must go out untouched or the client's hex parse shifts.
    """
    print("")
    print("Group broadcast relay (0x2A m20 -> 0x0E m1 type 7012)")

    class RecordingSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append((data, addr))

        def bind(self, *_):
            pass

        def settimeout(self, *_):
            pass

    import tempfile
    ps.DATA_DIR = tempfile.mkdtemp()
    ps.PLAYER_DIRECTORY.clear()
    ps.PLAYER_IDENTITIES.clear()
    ps.GAME_SESSIONS.clear()
    server = ps.PortServer(0, ps.SECURE_DISPATCH, "relay-test")
    server.sock = RecordingSocket()
    ps.SECURE_SERVER[0] = server

    leader, follower = ("10.3.3.1", 3074), ("10.3.3.2", 3074)
    a = ps.resolve_player_identity(leader, "groupleader")
    b = ps.resolve_player_identity(follower, "groupmember")
    ps.CONNECTED_PIDS[leader] = a["pid"]
    ps.CONNECTED_PIDS[follower] = b["pid"]

    class FakePacket:
        pass

    pkt = FakePacket()
    pkt.src_vport = ps.VPort(stream_type=3, port=15)
    pkt.dst_vport = ps.VPort(stream_type=3, port=1)
    pkt.session_id = 0x4F
    server._remember_conn((follower, 15), pkt, follower)
    server._remember_conn((leader, 15), pkt, leader)

    # A group is session type 2, as the client creates it.
    create = struct.pack("<III", 2, 1, ps.ATTR_SESSION_TYPE) + struct.pack("<I", 2)
    _label, resp = ps.handle_gamesession_create_session(1, create, leader)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    type_id, session_id = struct.unpack("<II", params)
    key = struct.pack("<II", type_id, session_id)
    ps.handle_gamesession_add_participants(
        2, key + struct.pack("<II", 1, a["pid"]) + struct.pack("<I", 0), leader)
    ps.handle_gamesession_join_session(3, key, follower)

    message = ("03000000140000000300000001080000000400000001000000006c000000"
               "0102000000740000000101000000750000000104000000")
    body = bytearray(key)
    ps.write_string(body, message)
    server.sock.sent = []
    ps.handle_gamesession_send_text_message(4, bytes(body), leader)

    pushes = []
    for data, dest in server.sock.sent:
        parsed = ps.PrudpPacket.parse(data)
        if not parsed.payload:
            continue
        _s, is_req, proto, _call, method, par = ps.parse_rmc_request(parsed.payload)
        if is_req and proto == 0x0E and method == 1:
            pushes.append((dest, par))
    check("two notifications are pushed: the other member AND the sender's own echo",
          len(pushes) == 2 and {d for d, _p in pushes} == {follower, leader},
          str([d for d, _p in pushes]))
    to_follower = [p for d, p in pushes if d == follower]
    if not to_follower:
        return
    par = to_follower[0]
    check("the sender gets it too - its consumer's owner branch for type 2 "
          "(createLobbySession) can only run on its own broadcast",
          any(d == leader for d, _p in pushes))
    source, pos = ps.read_u32(par, 0)
    ntype, pos = ps.read_u32(par, pos)
    param1, pos = ps.read_u32(par, pos)
    param2, pos = ps.read_u32(par, pos)
    relayed, pos = ps.read_string(par, pos)
    param3, pos = ps.read_u32(par, pos)
    check("the event decodes with nothing left over", pos == len(par),
          str(pos) + " of " + str(len(par)))
    check("type 7012 = major 7, minor 12, the only branch that decodes the string",
          ntype == 7012, str(ntype))
    check("param3 carries the session key's type id, which the consumer gates on",
          param3 == 2, str(param3))
    check("param1 and param2 are zero; the consumer never reads them",
          param1 == 0 and param2 == 0, "%d/%d" % (param1, param2))
    check("the sender is named as the source", source == a["pid"], str(source))
    check("THE MESSAGE IS RELAYED VERBATIM - re-encoding shifts the hex parse",
          relayed == message,
          "identical" if relayed == message else "CHANGED")

    # With nobody else in the group only the sender's own echo goes out.
    ps.handle_gamesession_leave_session(5, key, follower)
    server.pending.clear()
    server.send_queue.clear()
    server.sock.sent = []
    ps.handle_gamesession_send_text_message(6, bytes(body), leader)
    pushes = [d for data, d in server.sock.sent
              if ps.PrudpPacket.parse(data).payload]
    check("a lone leader gets only its own echo", pushes == [leader], str(pushes))


def check_session_type_filtering():
    """A group is a session too, and must not be offered as a game.

    The client creates a group with session type id 2 where a game is type
    id 1, and every SearchSessionsWithParticipants request asks for type 1.
    Ignoring the type would offer the group itself as a joinable game.
    """
    print("")
    print("Session type filtering: a group is not a game")
    import tempfile
    ps.DATA_DIR = tempfile.mkdtemp()
    ps.PLAYER_DIRECTORY.clear()
    ps.PLAYER_IDENTITIES.clear()
    ps.GAME_SESSIONS.clear()
    fresh_friendships()
    a_addr, b_addr = ("10.9.9.1", 3074), ("10.9.9.2", 3074)
    a = ps.resolve_player_identity(a_addr, "searcher")
    b = ps.resolve_player_identity(b_addr, "hoster")
    ps.CONNECTED_PIDS[a_addr] = a["pid"]
    ps.CONNECTED_PIDS[b_addr] = b["pid"]

    ids = {}
    for type_id in (1, 2):
        create = struct.pack("<III", type_id, 1, ps.ATTR_SESSION_TYPE)
        create += struct.pack("<I", 2)
        _label, resp = ps.handle_gamesession_create_session(1, create, b_addr)
        _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
        _t, session_id = struct.unpack("<II", params)
        ids[type_id] = session_id
        key = struct.pack("<II", type_id, session_id)
        ps.handle_gamesession_add_participants(
            2, key + struct.pack("<II", 1, b["pid"]) + struct.pack("<I", 0), b_addr)

    for asked in (1, 2):
        _label, resp = ps.handle_gamesession_search_with_participants(
            3, struct.pack("<III", asked, 1, b["pid"]), a_addr)
        _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
        count, pos = ps.read_u32(params, 0)
        check("asking for session type %d returns exactly one session" % asked,
              count == 1, str(count))
        _t, pos = ps.read_u32(params, pos)
        found, pos = ps.read_u32(params, pos)
        check("and it is the type-%d one, not the other" % asked,
              found == ids[asked], hex(found))

    # A group's LOBBY session is key type 1 like a match, but SessionType 1.
    # Public queries ask SessionType 2, so SessionType is honoured whether
    # or not strict matching is on; otherwise a searcher could be handed an
    # idle friend's lobby.
    print("  idle lobby: a session hosted without a preceding search is never offered")
    ps.GAME_SESSIONS.clear()
    ps.LAST_SEARCH.clear()
    # two different hosts: a player is only in one key-type-1 session at a
    # time, so one host creating both would empty and delete the first
    c_addr = ("10.9.9.3", 3074)
    c = ps.resolve_player_identity(c_addr, "lobbyhoster")
    ps.CONNECTED_PIDS[c_addr] = c["pid"]
    # the lobby: CREATE_GROUP creates it cold, SessionType 1, as the
    # client does - and a group leader's MATCHES are SessionType 1 too, so
    # SessionType is deliberately the same on both sessions here
    lobby = struct.pack("<IIII", 1, 1, ps.ATTR_SESSION_TYPE, 1)
    _label, resp = ps.handle_gamesession_create_session(4, lobby, c_addr)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    _t, lobby_id = struct.unpack("<II", params)
    ps.handle_gamesession_add_participants(
        5, struct.pack("<II", 1, lobby_id) + struct.pack("<II", 1, c["pid"])
        + struct.pack("<I", 0), c_addr)
    # the match: its host searches first, gets nothing, then hosts
    query = struct.pack("<III", 1, 3, 2) + struct.pack("<IIII", 0x66, 1,
                                                        ps.ATTR_SESSION_TYPE, 2)
    ps.handle_gamesession_search_sessions(6, query, b_addr)
    match = struct.pack("<IIII", 1, 1, ps.ATTR_SESSION_TYPE, 1)
    _label, resp = ps.handle_gamesession_create_session(7, match, b_addr)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    _t, match_id = struct.unpack("<II", params)
    ps.handle_gamesession_add_participants(
        8, struct.pack("<II", 1, match_id) + struct.pack("<II", 1, b["pid"])
        + struct.pack("<I", 0), b_addr)
    was_strict = ps.STRICT_SESSION_MATCHING
    ps.STRICT_SESSION_MATCHING = False
    _label, resp = ps.handle_gamesession_search_sessions(9, query, a_addr)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    count, pos = ps.read_u32(params, 0)
    check("a public search with strict matching OFF returns one session",
          count == 1, str(count))
    if count == 1:
        _t, pos = ps.read_u32(params, pos)
        found, pos = ps.read_u32(params, pos)
        check("and it is the match hosted after a search, not the cold lobby",
              found == match_id, hex(found))
    # a session with no game mode chosen is a lobby whatever preceded it
    ps.handle_gamesession_search_sessions(10, query, c_addr)
    nomode = struct.pack("<IIIIII", 1, 2, ps.ATTR_SESSION_TYPE, 1, ps.ATTR_GAME_MODE, 0xFFFFFFFF)
    _label, resp = ps.handle_gamesession_create_session(11, nomode, c_addr)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    _t, nomode_id = struct.unpack("<II", params)
    ps.handle_gamesession_add_participants(
        12, struct.pack("<II", 1, nomode_id) + struct.pack("<II", 1, c["pid"])
        + struct.pack("<I", 0), c_addr)
    _label, resp = ps.handle_gamesession_search_sessions(13, query, a_addr)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    count, pos = ps.read_u32(params, 0)
    _t, pos = ps.read_u32(params, pos)
    found, pos = ps.read_u32(params, pos)
    check("a session with GameMode 0xFFFFFFFF is hidden even after a search",
          count == 1 and found == match_id, f"{count} {found:#x}")
    ps.STRICT_SESSION_MATCHING = was_strict

    # The friends row shows a friend as "in a session, not joinable" - which
    # looks like Offline - for anyone whose session has SessionType 1.
    # So the contacts flag must stay 0 for a lobby-only player and turn on
    # for a match. Each name asked is a friend request and the reply is the
    # asker's whole friends list, so the row is found by name.
    print("  contacts flag: a lobby (SessionType 1) is not 'in a session', a match is")
    for who, expected in ((c, 0), (b, 1)):
        req = bytearray(struct.pack("<I", 1))
        ps.write_string(req, who["name"])
        _label, resp = ps.handle_contacts_game_sessions(10, bytes(req), a_addr)
        _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
        flags = {}
        n, pos = ps.read_u32(params, 0)
        for _ in range(n):
            name, pos = ps.read_string(params, pos)
            _pid, pos = ps.read_u32(params, pos)
            flags[name] = params[pos]
            pos += 1
        check("%s (%s) reports flag %d" % (
            who["name"], "lobby host" if expected == 0 else "match host", expected),
            flags.get(who["name"]) == expected, str(flags))




def check_invitation_push():
    """An invitation must be PUSHED - the client never polls for one.

    The client sends SendInvitation but never calls GetInvitationsReceived,
    so without the push the Invites screen stays empty.
    """
    print("")
    print("Invitation push (0x2A m12 -> 0x0E m1)")

    class RecordingSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append((data, addr))

        def bind(self, *_):
            pass

        def settimeout(self, *_):
            pass

    import tempfile
    ps.DATA_DIR = tempfile.mkdtemp()
    ps.PLAYER_DIRECTORY.clear()
    ps.PLAYER_IDENTITIES.clear()
    ps.INVITATIONS.clear()
    server = ps.PortServer(0, ps.SECURE_DISPATCH, "invite-test")
    server.sock = RecordingSocket()
    ps.SECURE_SERVER[0] = server

    a_addr, b_addr = ("10.4.4.1", 3074), ("10.4.4.2", 3074)
    a = ps.resolve_player_identity(a_addr, "inviter")
    b = ps.resolve_player_identity(b_addr, "invitee")
    ps.CONNECTED_PIDS[a_addr] = a["pid"]
    ps.CONNECTED_PIDS[b_addr] = b["pid"]

    class FakePacket:
        pass

    pkt = FakePacket()
    pkt.src_vport = ps.VPort(stream_type=3, port=15)
    pkt.dst_vport = ps.VPort(stream_type=3, port=1)
    pkt.session_id = 0x4F
    server._remember_conn((b_addr, 15), pkt, b_addr)

    key = struct.pack("<II", 1, 0x22D600)
    req = bytearray(key + struct.pack("<II", 1, b["pid"]))
    ps.write_string(req, "Game invite")
    server.sock.sent = []
    ps.handle_gamesession_send_invitation(0x61, bytes(req), a_addr)

    check("exactly one packet is pushed to the invitee",
          len(server.sock.sent) == 1, str(len(server.sock.sent)))
    if not server.sock.sent:
        return
    data, dest = server.sock.sent[0]
    check("it goes to the invitee, not the inviter", dest == b_addr, str(dest))
    parsed = ps.PrudpPacket.parse(data)
    _size, is_req, proto, call, method, params = ps.parse_rmc_request(parsed.payload)
    check("it is a server-initiated request", is_req is True)
    check("on the notification protocol, method 1",
          proto == 0x0E and method == 1, hex(proto))
    check("with a call id from the server's own 0xC000 range",
          call >= 0xC000, hex(call))

    source, pos = ps.read_u32(params, 0)
    ntype, pos = ps.read_u32(params, pos)
    param1, pos = ps.read_u32(params, pos)
    param2, pos = ps.read_u32(params, pos)
    message, pos = ps.read_string(params, pos)
    param3, pos = ps.read_u32(params, pos)
    check("the event decodes with nothing left over", pos == len(params),
          str(pos) + " of " + str(len(params)))
    check("it names the inviter as the source", source == a["pid"], str(source))
    check("type 7005 = major 7 GameSession, minor 5 InviteReceived",
          ntype == 7005 and ntype // 1000 == 7 and ntype % 1000 == 5, str(ntype))
    check("param1 is zero, as acb-rdv writes it", param1 == 0, str(param1))
    check("param2 carries the session id", param2 == 0x22D600, hex(param2))
    check("param3 carries the session TYPE id, the branch the client has code for",
          param3 == 1, str(param3))
    check("the invitation message is carried", message == "Game invite", message)

    # An invitee who is not connected must not crash the sender's call.
    del ps.PLAYER_IDENTITIES[b_addr]
    server.sock.sent = []
    _label, resp = ps.handle_gamesession_send_invitation(0x62, bytes(req), a_addr)
    ok, _e, _p, _c, _m, out = ps.parse_rmc_response(resp)
    check("inviting an offline player still answers normally",
          ok is True and out == b"", out.hex())
    check("and pushes nothing", len(server.sock.sent) == 0,
          str(len(server.sock.sent)))


def check_presence():
    """RichPresence 0x6D: the call that decides whether a friend is Online.

    An empty method-2 reply makes every friend read Offline.
    The client merges these records into its contacts by principal id, and
    only the online branch ever looks at session attributes - which is
    where the invite and join fields come from.
    """
    print("")
    print("RichPresence 0x6D: the online flag")
    import tempfile
    ps.DATA_DIR = tempfile.mkdtemp()
    ps.PLAYER_DIRECTORY.clear()
    ps.PLAYER_IDENTITIES.clear()
    ps.PRESENCE.clear()
    a_addr, b_addr = ("10.8.8.1", 3074), ("10.8.8.2", 3074)
    a = ps.resolve_player_identity(a_addr, "watcher")
    b = ps.resolve_player_identity(b_addr, "target")
    ps.CONNECTED_PIDS[a_addr] = a["pid"]
    ps.CONNECTED_PIDS[b_addr] = b["pid"]
    empty_list = struct.pack("<I", 0)

    # Before B sets presence it must NOT be reported: inventing a blob
    # would fake the very field being reported.
    req = struct.pack("<II", 1, b["pid"])
    _label, resp = ps.handle_richpresence_get(1, req, a_addr)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    check("a player who has not set presence is omitted",
          params == empty_list, params.hex())

    # B sets presence exactly as a real client does.
    blob = b"01000000050000000100000000000000"
    set_req = struct.pack("<I", 0x0B) + struct.pack("<H", len(blob)) + blob
    ps.handle_richpresence_set(2, set_req, b_addr)
    check("SetPresence is stored against the player",
          ps.PRESENCE.get(b["pid"]) == (0x0B, blob), str(ps.PRESENCE.get(b["pid"])))

    _label, resp = ps.handle_richpresence_get(3, req, a_addr)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    count, pos = ps.read_u32(params, 0)
    check("now the player is reported", count == 1, str(count))
    pid, pos = ps.read_u32(params, pos)
    online = params[pos]
    pos += 1
    presence_id, pos = ps.read_u32(params, pos)
    length = struct.unpack_from("<H", params, pos)[0]
    pos += 2
    body = params[pos:pos + length]
    pos += length
    check("the reply decodes with nothing left over", pos == len(params),
          str(pos) + " of " + str(len(params)))
    check("it is the right player, flagged online",
          pid == b["pid"] and online == 1, "pid=%d online=%d" % (pid, online))
    check("the presence id is echoed back unchanged", presence_id == 0x0B,
          hex(presence_id))
    # the menus' presence has no game mode: the server adds one (property 4, the session's mode or 11) - an invitation
    # from a player without it is ignored by the invitee's game (see _presence_with_mode)
    check("a mode-less blob comes back with the game mode added, with no NUL appended",
          body == blob[:1] + b"2" + blob[2:] + b"04000000010000000b000000" if blob[:2] == b"01" else body == blob,
          body.decode("ascii", "replace"))
    with_mode = b"0200000005000000010000000000000004000000010000000e000000"
    check("a blob that has a game mode is echoed raw", ps._presence_with_mode(b["pid"], with_mode) == with_mode)

    # A disconnected player must not be reported as online.
    del ps.PLAYER_IDENTITIES[b_addr]
    _label, resp = ps.handle_richpresence_get(4, req, a_addr)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    check("a disconnected player is omitted again",
          params == empty_list, params.hex())


def check_player_statistics():
    """0x6C m6 is PlayerStatistics (the board by player names), and its reply has a second out-param."""
    import tempfile
    ps.DATA_DIR = tempfile.mkdtemp()
    ps.PLAYER_DIRECTORY.clear()
    print("")
    print("0x6C m6: player statistics, not friends")
    req = bytearray(struct.pack("<IIII", 1, 1, 0x2B, 1))
    ps.write_string(req, "Ezio")
    _label, resp = ps.handle_playerstatistics_leaderboard_by_names(1, bytes(req), ("10.7.7.1", 3074))
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    check("an unknown name: an empty list PLUS the second out-parameter",
          params == struct.pack("<II", 0, 0), params.hex())
    check("no third-party player records are served",
          not hasattr(ps, "_FRIENDS_CAPTURED_RECORDS"))






def check_gamesettings_patch():
    """The game settings are edited one segment at a time, and the checksum the game verifies is recomputed."""
    print("")
    print("gamesettings: the one-segment CXB patch (here: the DLC maps added to Public / Private DLC)")
    sys.path.insert(0, os.path.join(ROOT, "cxb"))
    import cxb_reader
    import cxb_patch
    import extra_maps

    raw = ps.load_gamesettings_cxb()
    entries, trailer, body = cxb_patch.read_table(raw)
    check("the container's table accounts for every byte of its body",
          sum(s for _n, s in entries) == len(raw) - body,
          f"{sum(s for _n, s in entries)} vs {len(raw) - body}")
    check("its trailer row is the number 3652845281, not CXBTool's 0x30",
          trailer.split(b"\x00")[0] == b"3652845281", repr(trailer[:12]))
    stored, recomputed = cxb_patch.verify_checksum(raw)
    check("that trailer is a CHECKSUM - crc32 with its own field zeroed - "
          "and Ubisoft's own container verifies",
          stored == recomputed == 3652845281, f"{stored} vs {recomputed}")

    ours = cxb_reader.parse_cxb(raw)["playlistconfig"]
    # Charlestown and Fort St Mathieu, day / night / weather: the DLC maps v1.00's playlists 9 and 10 do not list
    dlc = [2569229107, 1453016576, 1453016579, 2569229101, 1453016564, 1453016567]
    patched, changes = extra_maps.add_maps(ours, {"9": dlc, "10": dlc})
    check("both DLC playlists gain the six maps",
          sorted((pid, len(ids)) for pid, ids in changes) == [("10", 6), ("9", 6)], str(changes))

    out = cxb_patch.replace_segment(raw, "playlistconfig", patched)
    kept = cxb_patch.untouched_segments(raw, out)
    check("twenty of twenty-one segments keep Ubisoft's own bytes",
          len(kept) == 20 and "playlistconfig" not in kept, str(len(kept)))
    back = cxb_reader.parse_cxb(out)
    check("the patched container re-reads with its segments in order",
          list(back) == list(cxb_reader.parse_cxb(raw)))
    # A checksum copied through instead of recomputed fails verification and
    # puts the client into limited mode.
    stored, recomputed = cxb_patch.verify_checksum(out)
    check("the PATCHED container's checksum is recomputed, not copied",
          stored == recomputed and stored != 3652845281,
          f"{stored} vs {recomputed}")
    tampered = bytearray(out)
    tampered[-1] ^= 0xFF
    s2, r2 = cxb_patch.verify_checksum(bytes(tampered))
    check("and a single flipped byte makes it fail, the way the client sees it",
          s2 != r2)
    by_id = {p.get("id"): p for p in ET.fromstring(back["playlistconfig"])}
    check("Public DLC and Private DLC now offer nine maps each (Martinique's three and these six)",
          all(len(by_id[p].find("maps")) == 9 for p in ("9", "10")),
          str([len(by_id[p].find("maps")) for p in ("9", "10")]))
    check("the base playlists are untouched",
          len(by_id["1"].find("maps")) == 22 and len(by_id["5"].find("maps")) == 21,
          f"{len(by_id['1'].find('maps'))}, {len(by_id['5'].find('maps'))}")




def check_contacts_and_join_friend():
    """The "join a friend's game" chain, plus the name-length crash guard."""
    print("\nContacts, join-a-friend, and the 48-byte name limit")
    import tempfile
    ps.DATA_DIR = tempfile.mkdtemp()
    ps.PLAYER_DIRECTORY.clear()
    ps.PLAYER_IDENTITIES.clear()
    ps.GAME_SESSIONS.clear()
    fresh_friendships()
    a_addr, b_addr = ("10.6.6.1", 3074), ("10.6.6.2", 3074)
    a = ps.resolve_player_identity(a_addr, "seeker")
    b = ps.resolve_player_identity(b_addr, "hosted")
    ps.CONNECTED_PIDS[a_addr] = a["pid"]
    ps.CONNECTED_PIDS[b_addr] = b["pid"]

    # The client copies every contact name into a 48-byte buffer with
    # _strncpy_s, which ABORTS the process rather than truncating.
    long_name = "z" * 60
    clamped = ps.contact_safe_name(long_name)
    check("an over-long name is clamped below the client's 48-byte buffer",
          len(clamped.encode("utf-8")) <= 47, str(len(clamped)))
    check("a normal name is untouched", ps.contact_safe_name("Altair") == "Altair")

    # Contacts: the client uploads cached names; only ours match.
    req = bytearray(struct.pack('<I', 2))
    ps.write_string(req, "somebody-elses-uplay-friend")
    ps.write_string(req, b["name"])
    _label, resp = ps.handle_contacts_game_sessions(1, bytes(req), a_addr)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    count, pos = ps.read_u32(params, 0)
    check("only the name on this server matches", count == 1, str(count))
    name, pos = ps.read_string(params, pos)
    pid, pos = ps.read_u32(params, pos)
    flag = params[pos]
    pos += 1
    check("the contact reply decodes exactly", pos == len(params),
          f"{pos} of {len(params)}")
    check("it names the right player", name == b["name"] and pid == b["pid"], name)
    check("nobody in a session yet, so the flag is 0", flag == 0, str(flag))

    # Put B in a session; the flag must flip, because it is what makes the
    # client ask which session. A match is hosted right after the host's
    # own search comes back empty - a session created cold is a lobby.
    create = struct.pack('<III', 1, 1, ps.ATTR_SESSION_TYPE) + struct.pack('<I', 2)
    ps.handle_gamesession_search_sessions(1, struct.pack('<III', 1, 3, 0), b_addr)
    _label, resp = ps.handle_gamesession_create_session(2, create, b_addr)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    type_id, session_id = struct.unpack('<II', params)
    key = struct.pack('<II', type_id, session_id)
    ps.handle_gamesession_add_participants(
        3, key + struct.pack('<II', 1, b["pid"]) + struct.pack('<I', 0), b_addr)
    _label, resp = ps.handle_contacts_game_sessions(4, bytes(req), a_addr)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    _count, pos = ps.read_u32(params, 0)
    _name, pos = ps.read_string(params, pos)
    _pid, pos = ps.read_u32(params, pos)
    check("once they are hosting, the flag turns on", params[pos] == 1,
          str(params[pos]))

    # And the follow-up the flag triggers must find that session.
    _label, resp = ps.handle_gamesession_search_with_participants(
        5, struct.pack('<III', 1, 1, b["pid"]), a_addr)
    _ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    count, pos = ps.read_u32(params, 0)
    check("SearchSessionsWithParticipants finds the friend's session",
          count == 1, str(count))
    for _ in range(count):
        _t, pos = ps.read_u32(params, pos)
        found_id, pos = ps.read_u32(params, pos)
        host_pid, pos = ps.read_u32(params, pos)
        url_count, pos = ps.read_u32(params, pos)
        for _ in range(url_count):
            _u, pos = ps.read_string(params, pos)
        attr_count, pos = ps.read_u32(params, pos)
        for _ in range(attr_count):
            _k, pos = ps.read_u32(params, pos)
            _v, pos = ps.read_u32(params, pos)
        part_count, pos = ps.read_u32(params, pos)
        parts = []
        for _ in range(part_count):
            p, pos = ps.read_u32(params, pos)
            parts.append(p)
        check("it is the right session, hosted by the friend",
              found_id == session_id and host_pid == b["pid"], f"{found_id:#x}")
        check("and it carries the trailing participant list the DDL adds",
              parts == [b["pid"]], str(parts))
    check("the whole reply decodes with nothing left over", pos == len(params),
          f"{pos} of {len(params)}")

    # The two friend methods this client can actually send.
    _label, resp = ps.handle_friends_add_with_details(
        6, struct.pack('<III', b["pid"], 0, 0), a_addr)
    ok, _e, _p, _c, _m, params = ps.parse_rmc_response(resp)
    check("AddFriendWithDetails succeeds for a known player", ok is True)
    pid, pos = ps.read_u32(params, 0)
    name, pos = ps.read_string(params, pos)
    relationship = params[pos]
    pos += 1
    _details, pos = ps.read_u32(params, pos)
    status = params[pos]
    pos += 1
    check("its record decodes exactly", pos == len(params), f"{pos} of {len(params)}")
    check("status here is a BYTE, not a string like FriendData's",
          status in (0, 1), str(status))
    check("it reports the friendship", pid == b["pid"] and relationship == 1, name)

    unknown = bytearray()
    ps.write_string(unknown, "nobody-here")
    _label, resp = ps.handle_friends_add_by_name_with_details(7, bytes(unknown), a_addr)
    ok, err, _p, _c, _m, _params = ps.parse_rmc_response(resp)
    check("adding an unknown name is an error, not a fake success",
          ok is False and err == ps.QERROR_FRIENDS_USER_NOT_FOUND, f"{err:#010x}")


CONTACTS = 0x72




def check_search_result_attributes():
    """A search result carries all 20 of Ubisoft's attributes, not just the client's 17.

    The three the client never sends are server-owned, and the joining
    client's Hermes parser asserts by name when they are absent.
    """
    print("\nSearch-result attributes: the three server-owned ones")
    addr = ("10.9.9.9", 1000)
    ps.CONNECTED_PIDS[addr] = 2001
    ps.PLAYER_IDENTITIES[addr] = {"account": "c", "name": "CrashRepro", "pid": 2001}
    _size, _req, _proto, _call, _method, create_params = ps.parse_rmc_request(
        LIVE_CREATE_SESSION_PACKET)
    check("the real CreateSession request is the 17-attribute one",
          len(create_params) == 8 + 17 * 8, str(len(create_params)))
    _label, resp = ps.handle_gamesession_create_session(1, create_params, addr)
    ok, err, proto, call, method, params = ps.parse_rmc_response(resp)
    _type_id, session_id = struct.unpack('<II', params)
    session = ps.GAME_SESSIONS[session_id]

    built = ps.build_search_result(session_id, session)
    # Decode it back the way the client would.
    pos = 12
    url_count, pos = ps.read_u32(built, pos)
    for _ in range(url_count):
        _url, pos = ps.read_string(built, pos)
    attr_count, pos = ps.read_u32(built, pos)
    ids = set()
    values = {}
    for _ in range(attr_count):
        key, pos = ps.read_u32(built, pos)
        val, pos = ps.read_u32(built, pos)
        ids.add(key)
        values[key] = val
    check("the built result re-reads with nothing left over", pos == len(built),
          f"consumed {pos} of {len(built)}")
    check("the client's 17 attributes are all still there",
          len(ids) >= 17, str(len(ids)))
    for attr, name in ((0x05, "CurrentPublicSlots"),
                       (0x06, "CurrentPrivateSlots"),
                       (0x6F, "Accessibility")):
        check(f"{name} ({attr:#04x}) is present - without it the joiner crashes",
              attr in ids)
    check("the attribute set matches Ubisoft's exactly",
          ids == UBISOFT_ATTR_IDS,
          f"missing {sorted(UBISOFT_ATTR_IDS - ids)}, extra {sorted(ids - UBISOFT_ATTR_IDS)}")
    check("20 attributes, as in every record Ubisoft's server returned", len(ids) == 20, str(len(ids)))
    check("Accessibility is 0, as in all fourteen of Ubisoft's records",
          values[0x6F] == 0, str(values[0x6F]))
    check("an empty session reports 0 current public slots",
          values[0x05] == 0, str(values[0x05]))
    check("the client's own values are passed through untouched",
          values[0x03] == 8 and values[0x6C] == 2 and values[0x75] == 4,
          f"MaxPublicSlots={values[0x03]}, SessionType={values[0x6C]}, "
          f"GameMode={values[0x75]}")

    # And the counts must track the participant list, as Ubisoft's records
    # show them moving between 1 and 8.
    key_bytes = struct.pack('<II', 1, session_id)
    ps.handle_gamesession_add_participants(
        2, key_bytes + struct.pack('<II', 1, 2001) + struct.pack('<I', 0), addr)
    check("CurrentPublicSlots follows the participant count",
          session["attributes"][0x05] == 1,
          str(session["attributes"][0x05]))
    joiner = ("10.9.9.10", 1000)
    ps.CONNECTED_PIDS[joiner] = 2002
    ps.PLAYER_IDENTITIES[joiner] = {"account": "d", "name": "Joiner2", "pid": 2002}
    ps.handle_gamesession_join_session(3, key_bytes, joiner)
    check("a join increments it", session["attributes"][0x05] == 2,
          str(session["attributes"][0x05]))
    ps.handle_gamesession_leave_session(4, key_bytes, joiner)
    check("a leave decrements it", session["attributes"][0x05] == 1,
          str(session["attributes"][0x05]))
    check("CurrentPrivateSlots stays 0 for a public session",
          session["attributes"][0x06] == 0, str(session["attributes"][0x06]))


def simulate_two_clients():
    """Drive the join path with two fake clients.

    Checks that the handlers agree with each other: a join shows up in
    GetParticipants, a leave is accepted only from a real participant, and
    a non-participant gets the exact error bytes Ubisoft used.
    """
    print("\nTwo simulated clients through the join path")
    host_addr, joiner_addr = ("10.0.0.1", 1000), ("10.0.0.2", 1000)
    ps.CONNECTED_PIDS[host_addr] = 1001
    ps.CONNECTED_PIDS[joiner_addr] = 1002
    ps.PLAYER_IDENTITIES[host_addr] = {"account": "a", "name": "Host", "pid": 1001}
    ps.PLAYER_IDENTITIES[joiner_addr] = {"account": "b", "name": "Joiner", "pid": 1002}

    # Host creates a session, adds itself, registers its endpoint.
    create = struct.pack('<III', 1, 1, ps.ATTR_SESSION_TYPE) + struct.pack('<I', 2)
    _label, resp = ps.handle_gamesession_create_session(1, create, host_addr)
    ok, err, proto, call, method, params = ps.parse_rmc_response(resp)
    type_id, session_id = struct.unpack('<II', params)
    check("CreateSession returns an 8-byte key", len(params) == 8, params.hex())

    key_bytes = struct.pack('<II', type_id, session_id)
    ps.handle_gamesession_add_participants(
        2, key_bytes + struct.pack('<II', 1, 1001) + struct.pack('<I', 0), host_addr)
    urls = ["prudp:/address=10.0.0.1;port=3074;PID=1001;RVCID=1"]
    url_buf = bytearray(struct.pack('<I', len(urls)))
    for url in urls:
        ps.write_string(url_buf, url)
    ps.handle_gamesession_register_urls(3, bytes(url_buf), host_addr)

    # The joiner asks about the session, then joins it.
    _label, resp = ps.handle_gamesession_get_session(4, key_bytes, joiner_addr)
    ok, err, proto, call, method, params = ps.parse_rmc_response(resp)
    check("GetSession succeeds for a real session", ok is True, f"{err:#010x}")
    check("GetSession names the host pid",
          struct.unpack_from('<I', params, 8)[0] == 1001)

    _label, resp = ps.handle_gamesession_get_session(
        5, struct.pack('<II', 1, 0xDEAD), joiner_addr)
    ok, err, *_ = ps.parse_rmc_response(resp)
    check("GetSession on an unknown session is an error", ok is False, f"{err:#010x}")

    _label, resp = ps.handle_gamesession_join_session(6, key_bytes, joiner_addr)
    ok, err, proto, call, method, params = ps.parse_rmc_response(resp)
    check("JoinSession succeeds with an empty reply", ok and params == b"",
          params.hex() or "empty")

    # Both players must now appear, with the host's registered URL.
    req = key_bytes + struct.pack('<II', 0, 20)
    _label, resp = ps.handle_gamesession_get_participants(7, req, host_addr)
    ok, err, proto, call, method, params = ps.parse_rmc_response(resp)
    entries, consumed = read_participants(params)
    check("GetParticipants lists both players", len(entries) == 2,
          str([e[1] for e in entries]))
    check("GetParticipants output re-reads cleanly", consumed == len(params))
    host_entry = [e for e in entries if e[0] == 1001]
    check("the host's registered url is reported",
          bool(host_entry) and host_entry[0][2] == urls,
          str(host_entry[0][2]) if host_entry else "host missing")

    # Leaving: a real participant succeeds, a stranger gets Ubisoft's error.
    stranger = ("10.0.0.3", 1000)
    ps.CONNECTED_PIDS[stranger] = 1003
    _label, resp = ps.handle_gamesession_leave_session(8, key_bytes, stranger)
    ok, err, *_ = ps.parse_rmc_response(resp)
    check("a non-participant leaving gets Ubisoft's error",
          ok is False and err == ps.QERROR_SESSION_NOT_PARTICIPANT, f"{err:#010x}")

    _label, resp = ps.handle_gamesession_leave_session(9, key_bytes, joiner_addr)
    ok, err, proto, call, method, params = ps.parse_rmc_response(resp)
    check("a real participant leaving succeeds", ok is True and params == b"",
          f"{err:#010x}")
    _label, resp = ps.handle_gamesession_get_participants(10, req, host_addr)
    ok, err, proto, call, method, params = ps.parse_rmc_response(resp)
    entries, _ = read_participants(params)
    check("the leaver is gone from the participant list", len(entries) == 1,
          str([e[1] for e in entries]))

    # The host removes a participant before their own LeaveSession arrives (it notices the leave first):
    # their leave succeeds - an error would leave the leaver's game stuck "leaving game session" at its next join.
    ps.handle_gamesession_join_session(16, key_bytes, joiner_addr)
    ps.handle_gamesession_remove_participants(17, key_bytes + struct.pack('<II', 1, 1002), host_addr)
    _label, resp = ps.handle_gamesession_leave_session(18, key_bytes, joiner_addr)
    ok, err, *_ = ps.parse_rmc_response(resp)
    check("the host removed them first - their own leave still succeeds", ok is True, f"{err:#010x}")
    _label, resp = ps.handle_gamesession_leave_session(19, key_bytes, joiner_addr)
    ok, err, *_ = ps.parse_rmc_response(resp)
    check("... once (a second leave gets Ubisoft's error)",
          ok is False and err == ps.QERROR_SESSION_NOT_PARTICIPANT, f"{err:#010x}")

    # Migration keeps the session's contents under a new id.
    _label, resp = ps.handle_gamesession_migrate_session(15, key_bytes, host_addr)
    ok, err, proto, call, method, params = ps.parse_rmc_response(resp)
    check("MigrateSession returns an 8-byte key", ok and len(params) == 8,
          params.hex())
    new_type, new_id = struct.unpack('<II', params)
    check("the migrated key is a different session id", new_id != session_id,
          f"{session_id:#x} -> {new_id:#x}")
    check("the old session id is retired", session_id not in ps.GAME_SESSIONS)
    check("participants survive the migration",
          ps.GAME_SESSIONS[new_id]["participants"] == [1001],
          str(ps.GAME_SESSIONS[new_id]["participants"]))
    session_id, key_bytes = new_id, struct.pack('<II', new_type, new_id)

    # Invitations: stored, listed, then consumed on accept.
    invite = bytearray(key_bytes + struct.pack('<II', 1, 1002))
    ps.write_string(invite, "come play")
    ps.handle_gamesession_send_invitation(11, bytes(invite), host_addr)
    _label, resp = ps.handle_gamesession_get_invitations_received(
        12, struct.pack('<III', 1, 0, 20), joiner_addr)
    ok, err, proto, call, method, params = ps.parse_rmc_response(resp)
    count = struct.unpack_from('<I', params, 0)[0]
    check("the invitation reaches the recipient's list", count == 1, str(count))
    sender = struct.unpack_from('<I', params, 12)[0]
    check("the invitation names its sender", sender == 1001, str(sender))
    message, pos = ps.read_string(params, 16)
    check("the invitation carries its message", message == "come play", message)
    check("the record ends with an 8-byte datetime", pos + 8 == len(params),
          f"{len(params) - pos} byte(s) left")
    ps.handle_gamesession_accept_invitation(13, key_bytes, joiner_addr)
    _label, resp = ps.handle_gamesession_get_invitations_received(
        14, struct.pack('<III', 1, 0, 20), joiner_addr)
    ok, err, proto, call, method, params = ps.parse_rmc_response(resp)
    check("accepting clears the invitation",
          struct.unpack_from('<I', params, 0)[0] == 0)




def main():
    check_friends_list()
    check_contacts_and_join_friend()
    check_login_clears_stale_sessions()
    check_no_session_leak()
    check_group_events()
    check_session_lifetime()
    check_group_relay()
    check_session_type_filtering()
    check_invitation_push()
    check_presence()
    check_player_statistics()
    check_gamesettings_patch()
    check_search_result_attributes()
    simulate_two_clients()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
