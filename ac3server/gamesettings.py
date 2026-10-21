"""The game settings we serve: one CXB container, built once at start-up.

The game downloads it right after login (GameInfo GetFileList, then PersistentStore GetItem; services/gameinfo.py)
and must accept it before matchmaking works at all: it verifies the container's CRC32 trailer, and a container it
rejects drops it into limited mode. It holds 21 segments of XML - the playlists, every game mode's rules, the
onlineconfig block (group size, NAT tables) and more.

The base is the game's OWN default: the CXB embedded in the v1.00 AC3MP.exe (RVA 0x01162070, 0xe92c bytes),
shipped in cxb/embedded/. The settings in server_config.json then edit it, each one replacing only the segments it
changes and copying the rest byte for byte (cxb/cxb_patch.py recomputes the checksum):

    extra_maps         more map ids in chosen playlists - the DLC maps (playlists 9 and 10)
    playlist_order     every playlist's maps reversed or shuffled
    group_size, private_min_players   groups of up to 8, private matches from one player
    free_ability_sets  the in-match ability set switch free in every mode (GBrick_MoneySink IsMoneySinkFree)

A map listed here must also exist in the client's data, or there is nothing to load: the game silently drops map ids
it cannot resolve. Every edit is logged at start-up; one that fails is skipped and logged, and the stock
container served.
"""

import os
import re

import cxb_patch
import cxb_reader
import extra_maps
import server_config as config

from ac3server.storage import CXB_DIR


EMBEDDED_CXB_PATH = os.path.join(CXB_DIR, "embedded", "gamesettings_c20559_embedded_default.bin")


def load_gamesettings_cxb() -> bytes:
    """The game's own default container, as extracted from AC3MP.exe. Without it there is nothing sound to serve."""
    if not os.path.isfile(EMBEDDED_CXB_PATH):
        raise SystemExit(f"The game settings default is missing: {EMBEDDED_CXB_PATH}. It is the CXB embedded in "
                         f"AC3MP.exe v1.00 (RVA 0x01162070, 0xe92c bytes) and ships with this repository; the server "
                         f"cannot start without it.")
    with open(EMBEDDED_CXB_PATH, "rb") as f:
        return f.read()


def add_extra_maps(container: bytes) -> bytes:
    """List extra map ids in chosen playlists (server_config "extra_maps").

    A playlist offers exactly the map ids its playlistconfig entry declares, each the uid of an MpWorld the client's
    Bootstrap registers. Only the playlistconfig segment changes. Empty config -> the container as is.
    """
    if not config.EXTRA_MAPS:
        return container
    try:
        ours = cxb_reader.parse_cxb(container)["playlistconfig"]
        patched, changes = extra_maps.add_maps(ours, config.EXTRA_MAPS)
        added = [(pid, ids) for pid, ids in changes if pid != "missing playlists"]
        for pid, ids in changes:
            if pid == "missing playlists":
                print(f"[config] extra_maps: no playlist with id {', '.join(ids)} - skipped")
        if not added:
            print("[config] extra_maps: nothing to add")
            return container
        out = cxb_patch.replace_segment(container, "playlistconfig", patched)
        kept = cxb_patch.untouched_segments(container, out)
        print("[config] extra maps ON - " + ", ".join(
            f"playlist {pid} + {', '.join(ids)}" for pid, ids in added)
            + f" ({len(kept)} of {len(kept) + 1} segments left untouched)")
        return out
    except Exception as e:
        print(f"[config] extra_maps: could not patch the gamesettings ({e}) "
              f"- serving it without them")
        return container


def team_lobby_settings(container: bytes) -> bytes:
    """Group size and the private matches' minimum (server_config group_size / private_min_players).

    Off by default: the stock 4 and 2. With group_size 8, a group of 8 can form
    (onlineconfig GroupSizeMax) and enter a private match together (the private
    playlists 8 and 10: groupSize; the team modes: JoinMaxGroupSize).
    MaxGroupBySession stays as it is. With private_min_players 1 a private match
    starts with one player (every mode's PrivateMinPlayers).
    """
    if config.GROUP_SIZE == 4 and config.PRIVATE_MIN_PLAYERS == 2:
        return container
    try:
        segs = cxb_reader.parse_cxb(container)
        changed, notes = {}, []
        if config.GROUP_SIZE != 4:
            n = segs["onlineconfig"].count('GroupSizeMax="4"')
            if n != 1:
                raise ValueError(f"GroupSizeMax=\"4\" found {n} times in onlineconfig")
            changed["onlineconfig"] = segs["onlineconfig"].replace('GroupSizeMax="4"', f'GroupSizeMax="{config.GROUP_SIZE}"')
            playlists, k = re.subn(r'(<Playlist id="(?:8|10)"[^>]*?groupSize=")4(")', rf'\g<1>{config.GROUP_SIZE}\2',
                                   segs["playlistconfig"])
            if k != 2:
                raise ValueError(f"private playlists' groupSize found {k} times, not 2")
            changed["playlistconfig"] = playlists
            modes = [name for name, text in segs.items() if 'JoinMaxGroupSize="4"' in text]
            for name in modes:
                changed[name] = segs[name].replace('JoinMaxGroupSize="4"', f'JoinMaxGroupSize="{config.GROUP_SIZE}"')
            notes.append(f"groups of {config.GROUP_SIZE} (GroupSizeMax, private playlists 8 and 10, JoinMaxGroupSize in "
                         f"{len(modes)} team modes)")
        if config.PRIVATE_MIN_PLAYERS != 2:
            modes = []
            for name, text in segs.items():
                text = changed.get(name, text)
                if 'PrivateMinPlayers="2"' in text:
                    changed[name] = text.replace('PrivateMinPlayers="2"', f'PrivateMinPlayers="{config.PRIVATE_MIN_PLAYERS}"')
                    modes.append(name)
            notes.append(f"private matches from {config.PRIVATE_MIN_PLAYERS} player(s) ({len(modes)} modes)")
        out = container
        for name, text in changed.items():
            out = cxb_patch.replace_segment(out, name, text)
        kept = cxb_patch.untouched_segments(container, out)
        print("[config] groups: " + "; ".join(notes) +
              f" ({len(kept)} of {len(kept) + len(changed)} segments left untouched)")
        return out
    except Exception as e:
        print(f"[config] group settings: could not patch the gamesettings ({e}) - serving them unchanged")
        return container


def reorder_playlists(container: bytes) -> bytes:
    """Reorder every playlist's map list (server_config "playlist_order").

    Same splice as add_extra_maps. Prints the Private playlist's new order.
    """
    if config.PLAYLIST_ORDER == "stock":
        return container
    try:
        ours = cxb_reader.parse_cxb(container)["playlistconfig"]
        patched, orders = extra_maps.reorder_maps(ours, config.PLAYLIST_ORDER)
        if not orders:
            print(f"[config] playlist_order {config.PLAYLIST_ORDER!r}: not reverse/shuffle "
                  f"or nothing to reorder - stock order")
            return container
        out = cxb_patch.replace_segment(container, "playlistconfig", patched)
        print(f"[config] playlist_order {config.PLAYLIST_ORDER} - {len(orders)} playlists reordered")
        for pid in ("8", "10"):
            if pid in orders:
                print(f"[config]   playlist {pid}: " + ", ".join(orders[pid]))
        return out
    except Exception as e:
        print(f"[config] playlist_order: could not patch the gamesettings ({e}) "
              f"- serving the stock order")
        return container


MONEYSINK_TAG = re.compile(r"<GBrick_MoneySink\b[^>]*>")


def free_money_sink(text: str):
    """-> (text with IsMoneySinkFree="true" in every GBrick_MoneySink tag, tags changed). A tag without the
    attribute gets it before its end."""
    def fix(m):
        tag = m.group(0)
        if 'IsMoneySinkFree="' in tag:
            return re.sub(r'IsMoneySinkFree="[^"]*"', 'IsMoneySinkFree="true"', tag)
        end = len(tag) - (2 if tag.endswith("/>") else 1)
        return tag[:end].rstrip() + ' IsMoneySinkFree="true" ' + tag[end:]
    count = 0

    def counted(m):
        nonlocal count
        new = fix(m)
        count += new != m.group(0)
        return new
    return MONEYSINK_TAG.sub(counted, text), count


def free_ability_sets(container: bytes) -> bytes:
    """The in-match ability set switch for free (server_config "free_ability_sets", on by default).

    Every game mode segment has one GBrick_MoneySink; the competitive modes ship IsMoneySinkFree="false" (the switch
    costs credits, ChangeAbilitySetPriceIncreaseFactor more each time), the tutorials and training "true". This sets
    "true" everywhere. ForceMoneySinkAvailable (training
    only) is left alone: the tutorials are free without it, and the switch is already available in every mode
    (IsChangeAbilitySetEnabled, playlistconfig isMoneySinkEnabled). Only the segments that change are replaced."""
    if not config.FREE_ABILITY_SETS:
        return container
    try:
        segs = cxb_reader.parse_cxb(container)
        out, modes = container, []
        for name, text in segs.items():
            new, n = free_money_sink(text)
            if n:
                out = cxb_patch.replace_segment(out, name, new)
                modes.append(name.replace("gamemode_", ""))
        kept = cxb_patch.untouched_segments(container, out)
        print(f"[config] free ability sets ON - IsMoneySinkFree in {len(modes)} modes ({', '.join(modes) or 'none'}; "
              f"{len(kept)} segments left untouched)")
        return out
    except Exception as e:
        print(f"[config] free_ability_sets: could not patch the gamesettings ({e}) - the switch keeps its price")
        return container


GAMESETTINGS_CXB = free_ability_sets(team_lobby_settings(reorder_playlists(add_extra_maps(load_gamesettings_cxb()))))
print(f"Built gamesettings CXB: {len(GAMESETTINGS_CXB)} bytes")
