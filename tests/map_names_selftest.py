"""Map names (cxb/map_names.json, used for the log's match labels): every map id the served playlists can hold has
the name the game shows - AC3's own and the DLC maps. Offline, a second.

    python tests/map_names_selftest.py

Writes only in a temporary folder (AC3_DATA_DIR is set before the server loads).
"""
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "cxb"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

TMP = tempfile.mkdtemp(prefix="ac3_map_names_selftest_")
os.environ["AC3_DATA_DIR"] = TMP

import cxb_reader  # noqa: E402
import server_modules  # noqa: E402

FAILED = []

# the DLC maps (server_config.example.json extra_maps): Charlestown and Fort St-Mathieu, day / night / weather
DLC_MAPS = {2569229107: "Charlestown", 1453016576: "Charlestown", 1453016579: "Charlestown",
            2569229101: "Fort St-Mathieu", 1453016564: "Fort St-Mathieu", 1453016567: "Fort St-Mathieu"}


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def playlist_maps(container):
    root = ET.fromstring(cxb_reader.parse_cxb(container)["playlistconfig"])
    out = {}
    for playlist in root:
        maps = playlist.find("maps")
        out[playlist.get("id")] = [int(m.get("id")) for m in maps] if maps is not None else []
    return out


def main():
    ps = server_modules.load()
    check("AC3_DATA_DIR is the temporary one", os.path.abspath(ps.DATA_DIR).startswith(os.path.abspath(TMP)), ps.DATA_DIR)
    with open(os.path.join(ROOT, "cxb", "map_names.json"), encoding="utf-8") as fh:
        names = json.load(fh)

    print("\nthe file")
    check("every entry has a name and a variant",
          all(v.get("name") and v.get("variant") in ("Day", "Night", "Evening", "Weather") for v in names.values()))

    def named(i):
        n = names.get(str(i), {}).get("name", "")
        return bool(n) and not n.lower().startswith("mpworld")

    print("\nevery map id the served playlists can hold")
    stock = ps.load_gamesettings_cxb()
    stock_ids = sorted({i for ids in playlist_maps(stock).values() for i in ids})
    check(f"the stock playlists' {len(stock_ids)} map ids", all(named(i) for i in stock_ids),
          str([i for i in stock_ids if not named(i)]))
    check("the DLC maps, by name", all(names.get(str(i), {}).get("name") == n for i, n in DLC_MAPS.items()),
          str({i: names.get(str(i)) for i in DLC_MAPS}))
    check("the log's label for one", ps.map_label(2569229107).startswith("Charlestown"), ps.map_label(2569229107))

    print("\nall checks passed" if not FAILED else f"\n{len(FAILED)} FAILED: {', '.join(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
