# ac3-server

A server for **Assassin's Creed III multiplayer** (PC, v1.00): a reimplementation of the Ubisoft / Quazal
Rendez-Vous backend the game talks to. It works with the game as it ships.

## Run a server

Python 3.13, any OS, with TCP 80 and UDP 21006-21007 reachable by the players:

```
pip install -r requirements.txt
python onlineconfig/onlineconfig_server.py      (binds TCP 80: Administrator / root)
python -m ac3server
```

No configuration is needed: the server finds its own public address (the start-up banner says which, and how). Every
setting is optional and documented in [`server_config.example.json`](server_config.example.json); copy it to
`server_config.json` to change one. For a LAN evening, set `"address"` to the server PC's LAN address. Ports, state,
backups and troubleshooting: **[docs/RUNNING.md](docs/RUNNING.md)**.

## Play on a server

Point it at the server - with Python installed, in an Admin PowerShell:

```
python patcher\ac3mp_patch.py "C:\path\to\Assassin's Creed III\AC3MP.exe" --host <server name or address>
```

The patcher also stops the game sending your real Ubisoft password to whatever server it connects to (the stock game
does). It only edits a v1.00 `AC3MP.exe` it recognises byte for byte, keeps the original as `AC3MP.exe.orig`, and
`--restore` puts it back. (The alternative is a hosts-file line, `<server address> onlineconfigservice.ubi.com`, but
then the game still sends the password.)

On the server you are the Ubisoft name you play under; there are no separate accounts. For matches, have UPnP on or
UDP 3074 forwarded on your router, and no VPN running: matches are peer to peer.

## How it works

Two Python processes:

| process | port | role |
|---|---|---|
| `onlineconfig/onlineconfig_server.py` | TCP 80 | the game's first request (`OnlineConfigService`), which tells it where the game servers are; `GET /status` |
| `python -m ac3server` | UDP 21006-21007 | the PRUDP / RMC game services: authentication, secure connection, matchmaking (GameSession), friends, shop, statistics, profiles, the ladder |

Matches are peer to peer between the players; the server handles everything around them. State is plain JSON and
small binary files in `prudp/data/`. How it all fits together: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Repository layout

| path | what |
|---|---|
| `ac3server/` | the game server (PRUDP transport, RMC services) - a map of its modules is in its `__init__.py` |
| `onlineconfig/onlineconfig_server.py` | the HTTP service: OnlineConfig and `/status` |
| `server_config.py`, `server_config.example.json` | settings and their documentation |
| `cxb/` | the game settings container (CXB): reader, patcher, the game's embedded default, map edits |
| `patcher/ac3mp_patch.py` | patches a player's own `AC3MP.exe`: never send the Ubisoft password; optional `--host` |
| `tests/` | offline self-tests - `python tests/run_all.py` |
| `docs/` | [ARCHITECTURE.md](docs/ARCHITECTURE.md), [RUNNING.md](docs/RUNNING.md), [CREDITS.md](docs/CREDITS.md) |


## Licence

GPLv3 - see [LICENSE](LICENSE). The CXB codec is a port of CXBTool (GPLv3); see
[docs/CREDITS.md](docs/CREDITS.md#credits--prior-art) for the credits.
