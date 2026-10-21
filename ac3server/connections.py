"""What the game state does when a player's secure connection ends: a DISCONNECT, or CONNECTION_TIMEOUT seconds
of silence (ac3server/transport.py calls this as the secure server's `on_disconnect`).

The authentication connection is not wired to this: it comes from the same address (only the vport differs) and
closes early in every normal login, so treating its end as the player leaving would wreck a healthy session.
"""

from ac3server.gamesession.sessions import release_session
from ac3server.gamesession.state import GAME_SESSIONS
from ac3server.players.friendships import announce_friend_status
from ac3server.players.identity import CONNECTED_PIDS, CONNECTED_USERS, PLAYER_IDENTITIES, online_pids
from ac3server.services.presence import presence_forget


def player_disconnected(addr):
    pid = CONNECTED_PIDS.get(addr)
    # Out of every session they were in, hosting or not - the same rule as AbandonSession: a session only ends
    # with its last participant, so other players' groups and lobbies survive the host leaving. Releasing only
    # hosted sessions would leave a departed player in other people's participant lists for ever, and
    # SearchSessionsWithParticipants would report a friend in a game long gone.
    involved = [sid for sid, s in GAME_SESSIONS.items()
                if s.get("host") == addr
                or (pid is not None and pid in s.get("participants", []))]
    dropped, kept = [], []
    for sid in involved:
        release_session(sid, addr, "client disconnected")
        (dropped if sid not in GAME_SESSIONS else kept).append(sid)
    if dropped:
        print(f"[secure][{addr}] dropped {len(dropped)} empty session(s) "
              f"{[hex(s) for s in dropped]} - host disconnected")
    if kept:
        print(f"[secure][{addr}] kept {len(kept)} session(s) "
              f"{[hex(s) for s in kept]} - other participants are still in them")
    # Release the identity, so a reconnect is not taken for a second copy of a connected account ("name#2"). The
    # friends hear of it first, while the identity still says who it was.
    if addr in PLAYER_IDENTITIES:
        PLAYER_IDENTITIES.pop(addr, None)
        announce_friend_status(pid, False)
    CONNECTED_PIDS.pop(addr, None)
    CONNECTED_USERS.pop(addr, None)
    if pid is not None and pid not in online_pids():
        presence_forget(pid)                 # otherwise a stale presence would be served after a re-login
