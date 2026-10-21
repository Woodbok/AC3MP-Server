"""Names for game modes and maps, for the logs.

Mode ids are idGameMode (the session's GameMode attribute, 0x75). Map ids are MpWorld uids (the session's WorldId
attribute, 0x65, and the playlists' map ids), named from cxb/map_names.json, written from the game's default set of
MpWorlds.
"""

import json
import os

from ac3server.storage import CXB_DIR

MODE_NAMES = {11: "Training", 4: "Wanted", 12: "Manhunt", 1: "Easy Deathmatch", 9: "Deathmatch",
              6: "Assassinate", 14: "Artifact Assault", 10: "Domination", 5: "Team Deathmatch", 7: "Escort",
              2: "Wolfpack", 15: "Tag", 16: "Corruption"}
MAP_NAMES_FILE = os.path.join(CXB_DIR, "map_names.json")
_MAP_NAMES = [None]


def map_names():
    """{"<uid>": {"name", "variant", "object"}} from cxb/map_names.json ({} when it is missing or unreadable)."""
    if _MAP_NAMES[0] is None:
        try:
            with open(MAP_NAMES_FILE, encoding="utf-8") as fh:
                _MAP_NAMES[0] = json.load(fh)
        except (OSError, ValueError):
            _MAP_NAMES[0] = {}
    return _MAP_NAMES[0]


def mode_label(mode):
    """"Domination", "Mode 42" for an id with no name, "mode unknown" for None."""
    if mode is None:
        return "mode unknown"
    return MODE_NAMES.get(int(mode), f"Mode {mode}")


def map_label(map_id):
    """"Galata (Day)", "Map 123" for an id with no name, "map unknown" for None or 0."""
    if not map_id:
        return "map unknown"
    entry = map_names().get(str(int(map_id)))
    if not entry:
        return f"Map {map_id}"
    return entry["name"] + (f" ({entry['variant']})" if entry.get("variant") else "")
