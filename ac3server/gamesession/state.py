"""The session table, and the wire forms every GameSession (protocol 0x2A) handler shares.

A *session* is anything the game hosts through GameSession, and there are three kinds:

    a match     key type 1, created by a host right after its own search came back empty; offered to searchers
    a lobby     key type 1 too, created cold (CREATE_GROUP, a private match's menu) or with no game mode chosen;
                never offered to searchers
    a group     key type 2: a party of players who search and play together (gamesession/groups.py)

A key is (type id, session id). The server picks the ids, starting near where Ubisoft's were (around 0x0022D59F)
and far above the ids below 1000 the game treats as reserved. Each session is a dict:

    type_id, attributes {id: u32}, host (the creating connection's address), host_pid, participants [pid],
    private_participants [pid], urls (the host's station URLs), advertised (a match, not a lobby), created,
    returning_to_lobby (groups: when the leader last said so)

Sessions are kept in memory only; a restart forgets them and the games create new ones.
"""

import struct

from ac3server.players.identity import PLAYER_IDENTITIES
from ac3server.protocol.datatypes import read_u32, write_string


GAME_SESSIONS = {}
# (session id, pid) -> when the session's host removed that participant. The leaver's own LeaveSession usually
# arrives after it (the host notices the leave first), and answering it with "not a participant" leaves the leaver's
# game believing it is still in the session: its next join shows "leaving game session" and hangs for good.
REMOVED_BY_HOST = {}
REMOVED_BY_HOST_TTL = 600.0
NEXT_SESSION_ID = [0x0022D600]

# The only GameSession error code known from Ubisoft's server: its answer to a LeaveSession for a session the caller
# was no longer in.
QERROR_SESSION_NOT_PARTICIPANT = 0x8012000A
QERROR_SESSION_INVALID_KEY = 0x8012000A

GROUP_SESSION_TYPE_ID = 2


# --- attributes -------------------------------------------------------------------------------------------------
# AC3's own ids, NOT acb-rdv's (Brotherhood puts GameMode at 0x6C and SessionType at 0x6E; AC3 has SessionType at
# 0x6C, GameMode at 0x75 and the private flag at 0x6E). Read from the exe three ways that agree: the parameter block
# dumped by name (FUN_00db582f), written into property ids (FUN_00dd7c16), and its constructor (FUN_00ddaef5).
SESSION_ATTR_NAMES = {
    0x03: "MaxPublicSlots", 0x04: "MaxPrivateSlots",
    0x05: "CurrentPublicSlots", 0x06: "CurrentPrivateSlots",
    0x64: "PrivateCode", 0x65: "WorldId", 0x67: "Language",
    0x6B: "GameType", 0x6C: "SessionType", 0x6E: "PrivateSession",
    0x70: "DLCId", 0x72: "NATType", 0x73: "SessionLevel",
    0x74: "PlaylistId", 0x75: "GameMode", 0x77: "HostNATType",
}
ATTR_MAX_PUBLIC_SLOTS = 0x03
ATTR_MAX_PRIVATE_SLOTS = 0x04
ATTR_PRIVATE_CODE = 0x64
ATTR_WORLD_ID = 0x65          # the map: an MpWorld uid (the leader's match settings carry the same id)
ATTR_SESSION_TYPE = 0x6C
ATTR_PRIVATE_SESSION = 0x6E
ATTR_PLAYLIST_ID = 0x74
ATTR_GAME_MODE = 0x75

# Three attributes the SERVER owns: the client never sends them, and its search-result parser (FUN_0116ecd0) asserts
# "current public slot not found" without them and works out free slots as 0x03 - 0x05 and 0x04 - 0x06. Ubisoft's
# session records have all three, and acb-rdv adds the same three. Without them the joining game crashes.
ATTR_ACCESSIBILITY = 0x6F
SERVER_OWNED_ATTRS = (ATTR_CURRENT_PUBLIC_SLOTS, ATTR_CURRENT_PRIVATE_SLOTS,
                      ATTR_ACCESSIBILITY) = (0x05, 0x06, 0x6F)


def update_slot_counts(session):
    """Keep the current-slot attributes in step with the participant list."""
    private = set(session.get("private_participants", ()))
    participants = session.get("participants", [])
    session["attributes"][ATTR_CURRENT_PUBLIC_SLOTS] = len(
        [p for p in participants if p not in private])
    session["attributes"][ATTR_CURRENT_PRIVATE_SLOTS] = len(
        [p for p in participants if p in private])
    session["attributes"].setdefault(ATTR_ACCESSIBILITY, 0)


def describe_attrs(attrs):
    return ", ".join(
        f"{SESSION_ATTR_NAMES.get(k, hex(k))}={v}" for k, v in sorted(attrs.items()))


# --- match or lobby -----------------------------------------------------------------------------------------------
# A session created less than this after its host's own SearchSessions is a match being advertised; anything else is
# a lobby. Search -> empty result -> CreateSession takes a few hundred milliseconds. SessionType cannot tell them
# apart: every query asks for SessionType 2, and a group leader hosts its matches as SessionType 1.
SEARCH_TO_HOST_WINDOW = 3.0
LAST_SEARCH = {}  # addr -> time of the client's last SearchSessions


# --- wire forms ---------------------------------------------------------------------------------------------------

def read_attributes(params, pos):
    """[u32 count] { [u32 id][u32 value] } - returns {id: value}."""
    count, pos = read_u32(params, pos)
    attrs = {}
    for _ in range(count):
        key, pos = read_u32(params, pos)
        val, pos = read_u32(params, pos)
        attrs[key] = val
    return attrs, pos


def read_key(params, pos):
    """A GameSessionKey: [u32 typeId][u32 sessionId]."""
    type_id, pos = read_u32(params, pos)
    session_id, pos = read_u32(params, pos)
    return (type_id, session_id), pos


def build_search_result(session_id, session):
    """One GameSessionSearchResult, as SearchSessions, GetSession and SearchSessionsWithParticipants return it:

        [u32 typeId][u32 sessionId][u32 hostPid]
        [u32 urlCount] { [u16 len][url bytes incl NUL] }
        [u32 attrCount] { [u32 key][u32 value] }

    Ubisoft's SearchSessions replies parse exactly this way, with nothing left over.
    """
    out = bytearray()
    out += struct.pack('<III', session["type_id"], session_id,
                       session.get("host_pid", 0))
    urls = session.get("urls", [])
    out += struct.pack('<I', len(urls))
    for url in urls:
        write_string(out, url)
    attrs = session.get("attributes", {})
    out += struct.pack('<I', len(attrs))
    for key, value in attrs.items():
        out += struct.pack('<II', key, value)
    return bytes(out)


# --- who is in a session: pid -> name and station URLs ----------------------------------------------------------
# GetParticipants names every participant and gives their station URLs; both come from the client (its login and its
# RegisterURLs) and are indexed by pid here.
PARTICIPANT_INFO = {}  # pid -> {"name", "urls", "addr"}


# --- lookups ------------------------------------------------------------------------------------------------------

def session_of(pid):
    """(session_id, session) the player is currently in, or (None, None)."""
    for session_id, session in GAME_SESSIONS.items():
        if pid in session.get("participants", []):
            return session_id, session
    return None, None


def match_session_of(pid):
    """The MATCH the player is in, or (None, None) - groups and lobbies do not count.

    This decides what the game's friends list shows. A friend "in a session" is joinable only when that session's
    SessionType is 2, 3 or 5 with a free public slot (the friends-row builder FUN_007D025E); any other session shows
    them as "in a session, not joinable", which reads as Offline for a friend who is only in a group lobby. So
    groups and lobbies are reported as no session at all, which leaves the friend on the plain online row where the
    invite option is. This is an inference about Ubisoft's server, whose known lookups all targeted public matches.
    """
    for session_id, session in GAME_SESSIONS.items():
        if pid not in session.get("participants", []):
            continue
        if session.get("type_id") != 1:
            continue
        # the same lobby rule as SearchSessions
        if not session.get("advertised"):
            continue
        if session.get("attributes", {}).get(ATTR_GAME_MODE) == 0xFFFFFFFF:
            continue
        return session_id, session
    return None, None


def session_host_pid(session):
    """Who hosts a session: the pid the host added publicly, or else the player whose connection created it (a private
    match's creator joins as a private participant, which leaves host_pid unset)."""
    if session.get("host_pid") is not None:
        return session["host_pid"]
    identity = PLAYER_IDENTITIES.get(session.get("host"))
    return identity.get("pid") if identity else None


def hosted_session_of(pid, since=None):
    """(session id, session): the key-type-1 session `pid` hosts and is in (the newest), or (None, None). `since`: only
    one created at or after that time (a launch takes the match its host's game starts, not a lobby from before)."""
    found = (None, None)
    for sid, session in list(GAME_SESSIONS.items()):
        if session.get("type_id") == 1 and pid in session.get("participants", []) and session_host_pid(session) == pid:
            if since is not None and session.get("created", 0) < since:
                continue
            found = (sid, session)
    return found


# --- a group's own channel: notification major 7 ------------------------------------------------------------------
# The game keeps a group's member list itself (Hermes::GroupService). A joiner fills its own with GetParticipants;
# the LEADER has no call that refreshes it and learns of members only from these pushes. Without them the leader
# believes it is alone and leaves the group the moment it creates a match session (FUN_00dc643f -> FUN_00dc6d1e:
# "in a group with fewer than two known members -> leave it"). The group consumer (FUN_00DD8EDB) requires
# param3 == 2 and reads the member from param1.
GROUP_NOTIFICATION_MAJOR = 7
GROUP_LEADER_CHANGED_MINOR = 1      # the leader is now pidSource (ignored by the new leader itself)
GROUP_MEMBER_LEFT_MINOR = 2         # member param1 left
GROUP_MEMBER_JOINED_MINOR = 3       # GroupService::addMember(param1)
