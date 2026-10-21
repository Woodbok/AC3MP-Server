"""Add map ids to the playlists we serve - for maps the stock config never lists.

The map list is a server-side document: a playlist offers exactly the
`<map id>`s its `playlistconfig` entry declares. Each id is the uid of an
`MpWorld` object in the client's Game Bootstrap Settings. With the map's
archive in the client's `multi/`, listing that id is all a playlist needs -
which is how the DLC maps reach the DLC playlists (9 and 10).

Config: `"extra_maps": {"10": [2569229107]}` - playlist id -> map ids.
Everything else in the playlist is left exactly as it was.
"""
import xml.etree.ElementTree as ET


def add_maps(xml_text, wanted):
    """-> (patched playlistconfig XML, [(playlist id, added ids)]).

    `wanted` is {playlist id: [map ids]}; ids already listed are skipped,
    and a playlist id the config does not have is reported, not created.
    """
    wanted = {str(k): [str(i) for i in v] for k, v in (wanted or {}).items()}
    root = ET.fromstring(xml_text)
    changes = []
    seen = set()
    for playlist in root:
        pid = playlist.get("id")
        if pid not in wanted:
            continue
        seen.add(pid)
        maps = playlist.find("maps")
        if maps is None:
            continue
        have = {m.get("id") for m in maps}
        added = []
        for map_id in wanted[pid]:
            if map_id not in have:
                ET.SubElement(maps, "map", {"id": map_id})
                have.add(map_id)
                added.append(map_id)
        if added:
            changes.append((pid, added))
    missing = sorted(set(wanted) - seen)
    if missing:
        changes.append(("missing playlists", missing))
    if not any(pid != "missing playlists" for pid, _ in changes):
        return xml_text, changes
    return ET.tostring(root, encoding="unicode"), changes


def reorder_maps(xml_text, order, rng=None):
    """-> (playlistconfig XML with every playlist's maps reordered,
    {playlist id: [map ids in the new order]}).

    `order` is "reverse" or "shuffle" (`rng` a random.Random, for tests);
    anything else returns the text untouched and an empty dict. Only the
    order changes - no map is added or dropped.
    """
    if order not in ("reverse", "shuffle"):
        return xml_text, {}
    if rng is None:
        import random
        rng = random.Random()
    root = ET.fromstring(xml_text)
    result = {}
    for playlist in root:
        maps = playlist.find("maps")
        if maps is None or len(maps) < 2:
            continue
        entries = list(maps)
        if order == "reverse":
            entries.reverse()
        else:
            rng.shuffle(entries)
        for m in list(maps):
            maps.remove(m)
        maps.extend(entries)
        result[playlist.get("id")] = [m.get("id") for m in entries]
    return ET.tostring(root, encoding="unicode"), result


__all__ = ["add_maps", "reorder_maps"]
