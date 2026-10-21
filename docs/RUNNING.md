# Running a server

The server is two Python processes. Run them on any machine players can reach - a VPS, a home server with its ports
forwarded, or a PC on the LAN - under whatever keeps processes running on your system (a terminal, systemd, a
Windows service, a container you build yourself).

## What runs

| process | listens on | what for |
|---|---|---|
| `python onlineconfig/onlineconfig_server.py` | **TCP 80** | the game's OnlineConfig request, `GET /status` |
| `python -m ac3server` | **UDP 21006-21007** | login, matchmaking, friends, shop, careers |
| (the same process) | **UDP 11000-11002** | the game's NAT type detection, only when `nat_detect` is `"own"` |
| (the same process) | TCP 21080 on 127.0.0.1 only | `/status` for the HTTP process (`status_port`); never open it in a firewall |

Matches themselves are peer to peer between players (UDP 3074 on their side), so the server needs very little: one CPU
core and 1 GB of RAM are plenty.

## Start it

Python 3.13, from the repository's root:

```
pip install -r requirements.txt
python onlineconfig/onlineconfig_server.py
python -m ac3server
```

The HTTP process binds port 80, which needs an Administrator prompt on Windows and root or `CAP_NET_BIND_SERVICE` on
Linux. `AC3_HTTP_PORT` moves it for testing, but the game always asks port 80.

Open the ports in the firewall: TCP 80 and UDP 21006-21007 (and UDP 11000-11002 with `nat_detect: "own"`). Behind a
router, forward them to this machine. Then check:

```
curl http://127.0.0.1/status
```

`{"ok": true, ..., "prudp": {"ok": true, "players_online": 0, ...}}` means both processes are up.

## Configuration

None is needed. With no `server_config.json` the defaults apply, and the server finds its own address: a public
address on one of the machine's interfaces, else the address the internet sees it at, else its LAN address. The
start-up banner states the address it advertises and how it found it - check it first if players cannot connect.

To change a setting, copy `server_config.example.json` to `server_config.json` and edit it; both processes read it at
start-up. Every key is optional and documented in the example. The ones a host may want:

* `address` - when the banner's guess is wrong: a LAN-only evening wants this machine's LAN address, a machine with
  several public addresses the right one;
* `friends` - `"everyone"` (default) suits a community of any normal size;
* `extra_maps` - the DLC maps, for communities whose players own the DLC;
* `nat_detect: "own"` - answer the game's NAT detection here instead of at Ubisoft's servers.

Environment variables override a few settings: `AC3_CONFIG` (the file's path), `AC3_SERVER_ADDRESS`, `AC3_DATA_DIR`,
`AC3_LOG_DIR`, `AC3_LOG_PACKETS`, `AC3_HTTP_PORT`.

## Pointing players at it

A stock game asks `onlineconfigservice.ubi.com`. Each player points it at this server once, either way:

* **The patcher (recommended):** with Python installed, in an Administrator PowerShell -

  ```
  python patcher\ac3mp_patch.py "C:\path\to\Assassin's Creed III\AC3MP.exe" --host <server name or address>
  ```

  It also stops the game sending the player's real Ubisoft password to the server. It changes only a file it
  recognises byte for byte (AC3MP.exe v1.00), keeps the original as `AC3MP.exe.orig`, and `--restore` puts it back.
  A server name must be 27 characters at most.
* **A hosts-file line:** `<server address> onlineconfigservice.ubi.com` in `C:\Windows\System32\drivers\etc\hosts`
  (as Administrator). Without the patcher the game still sends the Ubisoft password; the server never stores or logs
  it, but the patcher is the safer choice.

The game is still started and owned through Ubisoft Connect as always. Players should have UPnP on or UDP 3074
forwarded, and no VPN running: matches are peer to peer.

## State, logs and backups

* **State** is in `prudp/data/` (`AC3_DATA_DIR`): every player's profile, career, balance and purchases, friendships
  and the ladder, as plain JSON and small binary files. **It is the only thing that cannot be rebuilt** - back the
  folder up (copying it while the server runs is safe: every file is replaced whole by a rename).
* **Logs** are in `prudp/logs/` (`AC3_LOG_DIR`): the game server's console, every RMC call by default; `log_packets`
  adds every packet. Files roll over at `log_max_mb` and the newest `log_keep` are kept.
* A restart drops every connected player; their games log in again.

## When something is wrong

* **The game drops to limited mode, nothing in the game server's log:** the client never reached the server - check
  the advertised address in the start-up banner, the firewall and port forwards, and that the player's hosts entry or
  patched exe names this server.
* **`/status` says 503:** the game server is down or hung; its console says why. A handler error is logged with its
  traceback and the server keeps serving.
* **A career looks reset:** per-player files are named after the Ubisoft name the game logs in with; a player who
  logs in under another name is someone else to the server.
* **Nobody can join anybody:** NAT between the players - their routers must let UDP 3074 through (UPnP, or a port
  forward), and VPNs must be off.
* **A `*** ... could not be read ... moved aside` line, or a `<file>.unreadable-<time>` in the data folder:** a state
  file was damaged (usually a hand edit). Nothing in it is lost and the server carried on (from its memory, or from
  empty for that file). Edit state files only while the server is stopped.
