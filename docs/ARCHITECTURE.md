# How the server works

A guide to the code, in the order a player meets it. Running it is [RUNNING.md](RUNNING.md).

## The big picture

Assassin's Creed III's multiplayer talks to Ubisoft's backend in two steps:

1. **One HTTP request** to `onlineconfigservice.ubi.com`: the *OnlineConfig* document, which says where everything
   else is. We answer it from `onlineconfig/onlineconfig_server.py` (TCP 80). A client patched with
   `patcher/ac3mp_patch.py --host` (or a hosts-file line) sends that request to us instead of Ubisoft.
2. **Quazal Rendez-Vous** over **PRUDP** (Quazal's reliable UDP) for everything after: login, matchmaking, friends,
   the shop, careers. That is the `ac3server` package (`python -m ac3server`), on UDP 21006 and 21007.

Matches themselves are peer to peer between the players' games (UDP 3074). The server introduces the players to
each other and keeps everything around the match: who is online, who is in which session, profiles, careers, money,
friendships.

```
 game ──HTTP──▶ onlineconfig_server.py :80       "the servers are at <address>:21006"
  │                  │  (also GET /status)
  │                  └──HTTP 127.0.0.1:21080──▶ ┐
  │                                              │
  ├──PRUDP──▶ auth server     :21006 ──┐         │   ac3server (one process)
  │            LoginEx: "you are pid N, here is a ticket, go to :21007"
  └──PRUDP──▶ secure server   :21007 ──┴── services ── game state ── prudp/data/*.json
               every game service: matchmaking, friends, shop, stats, profile ...
```

### One process, a few threads, one lock

`ac3server/app.py` starts:

| thread | what | where |
|---|---|---|
| main | the authentication server, UDP 21006 | `transport.PortServer` |
| secure | the secure server, UDP 21007 - every game service | `transport.PortServer` |
| status listener | HTTP on 127.0.0.1:21080, asked by the HTTP process | `status.py` |
| NAT detection | UDP 11000-11002, only with `nat_detect: "own"` | `natdetect.py` |

The game state - sessions, identities, groups - lives in module-level tables and is shared. Every thread that takes
packets in holds `runtime.STATE_LOCK` while it handles one, so their work never interleaves. The status listener does
not touch state at all: it queues its question for the secure thread (`status.run_status_requests`), so a hung game
server reads as not answering.

### Where state lives

In memory: who is connected, sessions, invitations, groups. A restart forgets them, and the games log in again.

On disk, in `prudp/data/` (`AC3_DATA_DIR`), written atomically (`storage.atomic_write`):
`players.json` (everyone ever seen), `friends.json`, `ladder.json`, and per player - named after the **account** (the
Ubisoft name the game logs in with), never the display name - `onlinesave_<account>.bin` (the profile blob),
`stats_<account>.json` (the career), `currency_<account>.json`, `purchases_<account>.json`. `history/` and
`stats_history/` keep every upload, so a bad one can be rolled back. A file that will not read (a hand edit with a
typo, a disk error) is moved aside to `<name>.unreadable-<time>` and logged with `***` - never overwritten by the next
save (`storage.set_aside`).

### The package

| module | what |
|---|---|
| `app` | start-up: the listeners and their threads |
| `transport` | `PortServer`: one UDP port's PRUDP connections, reliability, fragments, RMC dispatch |
| `connections` | what happens to the game state when a player's connection ends |
| `dispatch` | which handler answers which (protocol, method) |
| `protocol/` | the wire: `prudp` packets, `rmc` envelopes, `datatypes`, `crypto` |
| `services/` | one module per RMC protocol the game calls |
| `gamesession/` | matchmaking (protocol 0x2A): `state`, `sessions`, `search`, `groups`, `invitations` |
| `players/` | `identity` (who is on each connection), `directory` (everyone seen), `friendships` |
| `notifications` | messages the server pushes without being asked |
| `status` | the 127.0.0.1 `/status` listener |
| `gamesettings` | the game settings container (CXB) we serve, built at start-up |
| `storage`, `logs`, `redaction`, `runtime` | files, the console log, keeping credentials out of it, process facts |

Lower layers never import higher ones: `protocol` knows nothing of services, `transport` nothing of game state
(it is given `on_disconnect` and `on_tick` hooks), `players` nothing of sessions.

## 1. The wire: PRUDP and RMC

*`protocol/prudp.py`, `protocol/rmc.py`, `protocol/datatypes.py`, `protocol/crypto.py`, `transport.py`*

**PRUDP** is a reliable protocol over UDP. Every packet has a type (SYN, CONNECT, DATA, DISCONNECT, PING), flags
(ACK, RELIABLE, NEED_ACK), a sequence id and a one-byte checksum. Payloads are RC4'd with a constant public key.
None of this is security: it is a format the client insists on.

A connection, in `transport.PortServer.handle_packet`:

1. **SYN** - the client asks for a connection signature; we hand out the next one.
2. **CONNECT** - opens the connection and resets anything left over on that address and port. On the secure port it
   carries an authenticator encrypted with the session key from the login ticket; we decrypt it and answer with the
   challenge + 1.
3. **DATA** - the real traffic. Reliable DATA is ACKed, de-duplicated (`SEEN_SEQ_WINDOW`), put back in sequence
   order (`IN_ORDER_*`), reassembled from parts, and handed to its handler.
4. **PING** every ~20 s. Silence for `connection_timeout` (120 s) counts as a disconnect, and so does DISCONNECT.

What we send is paced: at most `RELIABLE_WINDOW` (4) packets in flight per connection, each resent every 0.25 s until
ACKed. The game drops most of a burst, so without this the game settings download never completes. The comments in
`transport.py` give the reason for each rule.

**RMC** (remote method call) is what a complete DATA message carries: a protocol id, a method id, a call id and the
parameters. The secure server's `dispatch.SECURE_DISPATCH` maps each (protocol, method) to a handler
`handler(call_id, params, addr) -> (label, reply)`. A call with no handler is logged as "No handler" and never
answered - in the game that is a 15-20 second stall - so every call the game has been seen to make is answered, even
if only with an empty list.

The server also *sends* requests: **notifications** (`notifications.py`, protocol 0x0E), for things like "your
friend came online" or "a member joined your group". The game only ACKs them.

## 2. Logging in

*`services/authentication.py`, `services/secure.py`, `players/identity.py`, `players/directory.py`, `redaction.py`*

```
game                                   auth :21006                         secure :21007
 │ SYN, CONNECT                           │
 │ LoginEx(name, online key, password) ──▶│ identity: name + pid; friends told "online"
 │◀── pid, ticket, "go to :21007" ────────│
 │ RequestTicket ────────────────────────▶│
 │ SYN, CONNECT(authenticator) ──────────────────────────────────────────────▶│ challenge + 1
 │ RegisterEx(urls, the same login data) ────────────────────────────────────▶│
 │◀───────────────────────────── connection id (RVCID), your public address ──│
 │ ... every game service ...                                                 │
```

**There are no accounts.** A player is the Ubisoft name their game logs in with. That is the model of acb-rdv and
of a server for people who know each other: anyone can claim any name, and nothing about the game's own Ubisoft
sign-in is touched, bypassed or forged.

**The password problem.** A stock client sends the player's real Ubisoft password in the login data. The server
never needs it - the ticket is built from Quazal's constant dummy password - so `redaction.py` replaces it the moment
it is parsed and keeps any packet carrying it out of the hex dumps. `patcher/ac3mp_patch.py` stops the client
sending it at all: every player should use it.

**Identity** (`players/identity.py`). A login resolves to `{account, name, pid}`, held per connection address:

- The pid is derived from the name, so it is stable without storage; `players` in the config can pin a pid and give
  a display name.
- The same name again from the same IP address is the same game reconnecting (after a sleep, or limited mode): it
  takes the identity over.
- The same name from another address becomes `name#2`, with its own pid and its own files.
- Pids start at 1000, because the game refuses to talk to its Overlord services for a pid below that.
- Every per-player file is named after the account, so a display name can change without orphaning a career.

**The directory** (`players/directory.py`) remembers everyone who ever logged in (`players.json`), so friends lists
can name people who are offline.

**The secure connection** answers RegisterEx with two things that matter for internet play: a per-connection id (the
RVCID), and the client's own address *as the server sees it*. The game's PRUDP traffic comes from the same UDP port
it plays on, so that address is the player's public NAT mapping; the game publishes it, and other players connect to
it.

**Leaving** (`connections.py`). When the secure connection ends:

- the player is taken out of every session (a session ends with its last participant);
- their friends are told they went offline;
- their identity is released.

## 3. Matchmaking, groups and invitations

*`gamesession/state.py`, `sessions.py`, `search.py`, `groups.py`, `invitations.py`, `notifications.py`*

Everything here is GameSession, protocol 0x2A. A **session** is anything a game hosts, identified by a key
(type id, session id). There are three kinds:

| kind | key type | how it comes to exist | offered to searchers? |
|---|---|---|---|
| match | 1 | the host searched, found nothing, and created one within 3 s | yes |
| lobby | 1 | created cold (a group's lobby, a private match's menu), or with no game mode chosen | no |
| group | 2 | CREATE_GROUP: players who search and play together | no |

The server only introduces players. It keeps the session table (in memory) and hands out each host's **station
URLs** (its public and LAN addresses); the games then connect to each other directly.

**Public match.** The host's game calls `SearchSessions`, gets nothing back, then `CreateSession`,
`AddParticipants` (itself) and `RegisterURLs`. A second player's search now returns that session, and their game
calls `GetSession`, `JoinSession`, and `GetParticipants` to learn everyone's URLs. The server adds three attributes
the game never sends but requires in every search result (`SERVER_OWNED_ATTRS`, the current slot counts); without
them the joining game crashes.

**Private match.** The same, joined by invitation: `SendInvitation` -> the server *pushes* notification 7005 to
the recipient (their game never asks) -> they accept -> `GetSession`, `JoinSession`. No Uplay service is involved.

**Groups** are the subtle part. A group is a type-2 session, and the game keeps its own member list for it:

- **The leader learns its members only from pushes** (notification major 7: joined / left / leader changed,
  `groups.notify_group_members`). A leader that believes it has fewer than two members dissolves the group as soon
  as it creates a match.
- **The leader steers the members by broadcasting** (`SendTextMessage`), for example "here is the session I
  created, join it" or "return to the lobby". The server relays each broadcast verbatim as notification 7012 to
  every member, *including the sender*: the leader's own game acts on "return to the lobby" only when it receives
  it back.
- **After "return to the lobby" the members' leaves are not reported for 30 s.** The members leave on purpose, and
  telling the leader made it dissolve the group.

**Rules the server keeps** (the docstrings say why):

- A player is in at most one session of each type at a time.
- A session ends with its *last* participant, not its host; a departing host's session migrates to whoever is left.
- A login clears the player out of every session they are still listed in.

**Friends' "join game".** `SearchSessionsWithParticipants` (m24) answers "which session is this friend in". The
friends list only offers to join *matches*: groups and lobbies are deliberately reported as no session
(`state.match_session_of`).

**Known unknowns.**

- Which minor of the participation notification (major 3) means "joined" and which "left". Pushing it is off
  (`notify_participants`), and nothing has needed it so far.
- `strict_session_matching` (filtering a search by game mode, session type and playlist) is off by default: a
  filter that is too strict looks exactly like a broken server.

## 4. Friends, presence and contacts

*`players/friendships.py`, `services/friends.py`, `services/contacts.py`, `services/presence.py`*

The game's friends screen is assembled from **three** services, and each decides something different:

| service | call | decides |
|---|---|---|
| Friends 0x14 | m12 GetDetailedList | who is on the list |
| RichPresence 0x6D | m2 GetPresence | whether each friend reads **online** (no presence record = Offline, whatever else says) |
| ContactsExtensions 0x72 | m3 / m4 | the contacts behind the list; a flag per contact = "in a match", which turns on **Join** (the game then asks GameSession m24 which match) |

All of it is answered from this server's own data. Uplay is not asked.

**Who is friends with whom** is the `friends` setting:

- `"everyone"` (default): every player this server has seen is a friend of every other, so anyone can be invited to
  a private match.
- `"requests"`: `friends.json`, mutual - the player's Ubisoft Connect friends who play here (the game asks about them
  in ContactsExtensions m3), and the game's own AddFriend (Friends m3 / m4). The game has no "accept friend request"
  call to send, so a request *is* a friendship.
- `"explicit"`: only the `friendships` pairs in the config.

**Online status** is RichPresence: each game sets its own presence blob (SetPresence) and the server hands it back to
whoever asks, for the players who are online. One detail matters: the game ignores an invitation from a player
whose presence carries no game mode, so the server adds one where it is missing (`PRESENCE_FILL_MODE`).

**Reconnects.** A game that logs in again (after the machine slept, or from limited mode) may never restart its
online menus - no SetPresence, no polls, invitations ignored - so an old presence must not be handed out for it. A
RegisterEx clears the player's presence (`presence_reset`) and the last connection ending forgets it
(`presence_forget`). Test: `tests/reconnect_selftest.py`.

**Notifications** keep open friends screens current: a friend request (1001), and a friend going online or
offline (5001), pushed at login and at disconnect.

## 5. A player's progress: profile, career, money

*`services/onlinesave.py`, `services/statistics.py`, `services/shop.py`, `services/ladder.py`*

Three stores, all per player and named after the account, all in `prudp/data/`:

| what | protocol | file | how it changes |
|---|---|---|---|
| **profile blob**: accolades, unlocks, loadouts, emblem choices | OnlineSave 0x77 | `onlinesave_<account>.bin` | the game uploads the whole blob; we store it unread and hand it back at login |
| **career**: XP and every match statistic | PlayerStatistics 0x6C | `stats_<account>.json` | the game reports one match's numbers (m2); we fold them in by each stat's own rule (add / replace / keep the larger) and serve the totals at login (m3) |
| **money and purchases** | Shop 0x1B5B | `currency_<account>.json`, `purchases_<account>.json` | the game reports what it earned (m3) and what it bought (m5); the balance is the server's |

What the player sees comes from these as follows:

- **XP** is group 1 info 1 of the career, and the game works the **level** out from it itself. The game reads its
  career only at login, so an edit of `stats_<account>.json` shows at the next login.
- **Prices** are the catalogue we send (`shop_catalogue.json` in the data folder, scaled by `price_multiplier`);
  without one the store is empty, which the game copes with.
- **UnlockItem** answers "owned" for every item.
- **The Abstergo Ladder** (0x7A): the host's game registers each match and reports its scores at the end; the server
  works out each player's ladder points and rating (`ladder.json`).

Every profile upload and every raw statistics report is also kept (`history/`, `stats_history/`, capped by
`history_keep`), so a bad one can be rolled back.

**The profile's shrink guard** (`onlinesave_shrink_guard`, default 0.5; 0 = off): while a well-formed save is stored,
an upload under that fraction of its size, with under that fraction of its persona loadout entries (record
0xAB0369C6: a count byte + 104 bytes per persona), or not well-formed itself is refused - the stored save stays, the
upload goes to `history/<account>_<time>_refused.bin` (never pruned), the log says `*** ... REFUSED`, and the game gets
the normal empty reply (as if saved; its next login reads the stored save). A game resumed from sleep can upload a
save with its loadout record empty, which would otherwise wipe the player's loadouts. Test:
`tests/onlinesave_selftest.py`.

## 6. The game settings, and the rest of the start-up

*`gamesettings.py`, `services/gameinfo.py`, `services/overlord.py`, `services/clientlogin.py`, `services/misc.py`*

After login the game runs a fixed list of start-up steps, each one an RMC call that must be answered. One
unanswered call leaves the game on "Updating Animus parameters", or drops it into limited mode. The steps, roughly
in order:

- **the game settings** (GameInfo GetFileList, then PersistentStore GetItem);
- **ClientLogin** - the "aconlineservices" login, whose failure the game shows as "you are not running the most
  recent version";
- **Overlord** - Ubisoft's middleware;
- **the shop and the balance**, **the profile blob**, **the career**;
- **the small services** - messaging, live events, telemetry.

**The game settings** are one CXB container of 21 XML segments: playlists, every mode's rules, group sizes, NAT
tables. The game verifies its CRC32 and will not matchmake without it. The server builds it once at start-up:

1. **The base** is the game's *own* built-in default, as embedded in `AC3MP.exe` v1.00, shipped in `cxb/embedded/`.
   Without it the server refuses to start.
2. **Each setting edits only the segments it needs**, copies the others byte for byte, and recomputes the
   checksum:
   - `extra_maps` - the DLC maps;
   - `playlist_order`;
   - `group_size` / `private_min_players`;
   - `free_ability_sets`.

A map listed here must also be in the client's data, or the game silently drops it.

**Overlord's** settings reply carries flags (`SKIP_UPLAY_LOGIN` and others) that tell the game to skip Ubisoft
social services that no longer exist. Without them the start-up waited on a dead web login and gave up.

**The small services** (`misc`, `clientlogin`, `uplaywin`) answer with Ubisoft's own replies, or with
empty lists where those carried data that is not ours to repeat.

## 7. Internet play: addresses, NAT and the hole punch

*`server_config.py`, `services/secure.py`, `services/nattraversal.py`, `natdetect.py`*

**The server's own address.** The game is told one address for everything (the OnlineConfig document's
`SandboxUrl`, LoginEx's secure server URL). With `address` unset the server finds it at start-up: a public address on
one of the machine's interfaces (a VPS), else the address the internet sees (asked of a what-is-my-IP service: a
cloud host behind NAT, or a home server whose router forwards the ports), else the LAN address. The start-up banner
says which, and why.

Matches are peer to peer, so two players' games must reach each other through their home routers (NATs). The
server helps in three ways:

1. **It tells each game its public address.** The RegisterEx reply carries the address the server sees the game
   connecting from. The game's PRUDP traffic comes from the same UDP port it plays on (3074), so that address is
   the router's mapping. The game publishes it in its station URLs, and the others connect to it.
2. **It relays the hole punch** (NATTraversal). When a game asks to reach a peer, the server tells that peer to
   send first as well, so both routers open.
3. **NAT type detection** (`nat_detect`). At every start the game asks two "punch detect" servers what kind of NAT
   it is behind (Open / Moderate / Strict), and it uses the answer to filter matchmaking. By default those are
   Ubisoft's servers, which still answer. With `nat_detect: "own"` this server answers instead (`natdetect.py`, UDP
   11000-11002).

**Not yet proven:** play between players behind different, strict NATs.

## 8. The HTTP process

*`onlineconfig/onlineconfig_server.py`, served by waitress on TCP 80*

| endpoint | what it does |
|---|---|
| `GET /OnlineConfigService.svc/GetOnlineConfig` | The game's first request. Answered with Ubisoft's own document, with our address in place of the game servers' (and of the NAT detect servers', with `nat_detect: "own"`). The address is checked on every request: an unreachable one sends every game into limited mode with nothing in the game server's log, so it is warned about loudly. |
| `GET /status` | 200 when both processes answer, 503 otherwise - for a glance or an uptime monitor. |
| `GET /` | One line: what this is, and the address to patch in. |

Flask's debugger is never used.

## 9. Configuration and operation

*`server_config.py`, `server_config.example.json`, [RUNNING.md](RUNNING.md)*

`server_config.json` is optional and shared by both processes. **Every key is optional**, and the defaults suit a
hosted server. `server_config.example.json` explains each key, grouped as:

- **the game** - money, extra maps, groups, the Extra Content URL;
- **friends**;
- **logs and disk**;
- **NAT detection** and the status port;
- **the ladder**;
- **advanced** switches that are right at their defaults;
- **experiments** - off, because their effect in the game is not yet established.

The start-up banner states every setting and where it came from, and names any key that is not a setting.

**State and disk.** Everything that cannot be rebuilt is in `prudp/data/`: back it up. By
default the logs keep the RMC calls only (`log_packets` adds every packet), roll over at 100 MB and keep the newest
30 files; profile and statistics snapshots keep the newest 200 per player.

**Capacity** (measured with `tests/load_test.py`, which simulates players speaking real PRUDP against a throwaway
server; it needs `pip install psutil`). A login costs the game server about 35 ms of CPU, most of it the ~150 KB game
settings download; a player in menus or a match costs almost nothing (a keep-alive every 20 s). 300 players logging in
at the same instant were all in within 11 s; idle, 300 players cost under 1% of a core. The ceiling is the single game
thread, somewhere in the low thousands of players.

**Tests:** `python tests/run_all.py` runs the offline self-tests (the transport, the session layer, NAT detection,
the shop, the ladder, storage) and a smoke test that starts both processes.
