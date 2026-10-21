"""The shop: free ability sets (the game settings' IsMoneySinkFree, free_ability_sets), the money (AddCurrency's
signed deltas) and the settings' documentation. Offline, a few seconds.

    python tests/shop_selftest.py

Writes only in temporary folders (AC3_DATA_DIR is set before the server loads).
"""
import json
import os
import struct
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "cxb"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

TMP = tempfile.mkdtemp(prefix="ac3_shop_selftest_")
os.environ["AC3_DATA_DIR"] = TMP

import cxb_builder  # noqa: E402
import cxb_reader  # noqa: E402
import server_modules  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def minified(xml):
    m = cxb_builder.minify_xml(xml)
    return m.decode("utf-8") if isinstance(m, bytes) else m


def params_of(items):
    out = struct.pack("<I", len(items))
    for item, when in items:
        out += struct.pack("<IQQ", item, when, 0)
    return out


def free_sets(ps):
    print("\nfree_ability_sets: IsMoneySinkFree in every mode")
    new, n = ps.free_money_sink('<GBrick_MoneySink Start="XML_InGame" IsMoneySinkFree="false" />')
    check("a false becomes true", n == 1 and new == '<GBrick_MoneySink Start="XML_InGame" IsMoneySinkFree="true" />', new)
    new, n = ps.free_money_sink('<GBrick_MoneySink Start="XML_InGame" />')
    check("a tag without the attribute gets it", n == 1 and 'IsMoneySinkFree="true"' in new and new.endswith("/>"), new)
    new, n = ps.free_money_sink('<GBrick_MoneySink IsMoneySinkFree="true" ForceMoneySinkAvailable="true" />')
    check("an already free tag is left as it is", n == 0)

    stock = ps.load_gamesettings_cxb()
    segs0 = cxb_reader.parse_cxb(stock)
    paid = [n for n in segs0 if 'IsMoneySinkFree="false"' in segs0[n]]
    check("the stock competitive modes ship a paid switch (the case to fix)", len(paid) >= 10, str(paid))

    ps.FREE_ABILITY_SETS = False
    check("free_ability_sets false: the container as is", ps.free_ability_sets(stock) == stock)
    ps.FREE_ABILITY_SETS = True
    out = ps.free_ability_sets(stock)
    segs = cxb_reader.parse_cxb(out)
    sinks = [n for n, t in segs.items() if "GBrick_MoneySink" in t]
    check(f"every mode with a GBrick_MoneySink is free ({len(sinks)} modes)",
          len(sinks) >= 15
          and all('IsMoneySinkFree="false"' not in segs[n] and 'IsMoneySinkFree="true"' in segs[n] for n in sinks))
    check("nothing else changes: the paid modes differ only by the attribute (rebuilt minified, as every patch "
          "does), every other segment is byte for byte",
          all(segs[n] == minified(segs0[n].replace('IsMoneySinkFree="false"', 'IsMoneySinkFree="true"')) for n in paid)
          and all(segs[n] == segs0[n] for n in segs0 if n not in paid))
    check("ForceMoneySinkAvailable left alone",
          sum(t.count("ForceMoneySinkAvailable") for t in segs.values())
          == sum(t.count("ForceMoneySinkAvailable") for t in segs0.values()))
    check("the same segments in the same order", list(segs) == list(segs0))
    check("a second pass changes nothing", ps.free_ability_sets(out) == out)


def currency(ps):
    print("\nAddCurrency: signed deltas")
    ps.onlinesave_user = lambda addr: "spender"
    ps.CURRENCY_MEM.clear()
    addr = ("127.0.0.1", 3075)
    path = os.path.join(TMP, "currency_spender.json")

    def add(a, b):
        name, resp = ps.handle_shop_add_currency(3, struct.pack("<ii", a, b), addr)
        return name == "Shop.AddCurrency" and resp.endswith(struct.pack("<II", 0, 0))

    def stored():
        return json.load(open(path, encoding="utf-8"))

    def served():
        _name, resp = ps.handle_shop_get_currency(4, b"", addr)
        return list(struct.unpack("<II", resp[-8:]))

    ps._currency_save(addr, [0, 5000])
    check("a match reward adds", add(0, 500) and stored() == [0, 5500])
    check("a spend (Spend -> AddCurrency(0, -price), as the game sends it: u32 2^32 - 60) takes it off",
          add(0, -60) and stored() == [0, 5440] and served() == [0, 5440], str(stored()))
    check("a free craft (AddCurrency(0, 0)) changes nothing and still answers", add(0, 0) and stored() == [0, 5440])
    check("a spend past the balance stops at 0, never wraps", add(0, -6000) and stored() == [0, 0] and served() == [0, 0])

    with open(path, "w", encoding="utf-8") as fh:
        json.dump([0, 4294972236], fh)               # 5,000 - 60 added unsigned, as an older server stored it
    ps.CURRENCY_MEM.clear()
    check("a balance stored 2^32 high (an unsigned add) is read as the game saw it", served() == [0, 4940])
    _name, resp = ps.handle_shop_buy_item(5, struct.pack("<III", 0x1234, 0, 5000), addr)
    check("and BuyItem's afford check uses the real balance: 5,000 over 4,940 is granted, not charged into a wrap",
          served() == [0, 4940] and resp[-28:-20] == struct.pack("<II", 0, 4940), str(served()))


def config():
    print("\nthe settings")
    import server_config
    with open(os.path.join(ROOT, "server_config.example.json"), encoding="utf-8") as fh:
        example = json.load(fh)
    check("free_ability_sets is a known key", "free_ability_sets" in server_config.KNOWN_KEYS)
    check("and documented in server_config.example.json with its default",
          example.get("free_ability_sets") is True and "_free_ability_sets" in example)
    documented = {k for k in example if not k.startswith("_")}
    check("every key of the example is a setting, and every setting is in the example",
          documented == set(server_config.KNOWN_KEYS), str(documented ^ set(server_config.KNOWN_KEYS)))


def main():
    ps = server_modules.load()
    ps.DATA_DIR = TMP
    free_sets(ps)
    currency(ps)
    config()
    if FAILED:
        print(f"\n{len(FAILED)} check(s) failed: {', '.join(FAILED)}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
