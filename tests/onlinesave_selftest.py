"""The profile's shrink guard (server_config onlinesave_shrink_guard, ac3server/services/onlinesave.py): a much smaller
OnlineSave upload never replaces a full one: a game woken from sleep can upload a save with its persona loadout record
empty (1,706 bytes) over a full one (10,234 bytes). Offline, a second.

    python tests/onlinesave_selftest.py

The saves are built here to the game's layout (big-endian, CRC32-framed records; the loadout record 0xAB0369C6 = a
count byte + 104 bytes per persona), with the sizes of real files: 82 loadouts = 10,234 bytes, none = 1,706, 24 =
3,202. Writes only in a temporary folder (AC3_DATA_DIR is set
before the server loads).
"""
import contextlib
import io
import os
import struct
import sys
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

TMP = tempfile.mkdtemp(prefix="ac3_onlinesave_selftest_")
os.environ["AC3_DATA_DIR"] = TMP

import server_modules  # noqa: E402

FAILED = []
TAG = 0xA0EB491F
# the top-level records of a real save, (tag, payload length) - the loadout record's payload is built per save
RECORDS = [(0x97DDF0C0, 501), (0x161A9278, 217), (0xAB0369C6, None), (0xB2306F11, 9), (0xB3F70A80, 36),
           (0x4A0508E7, 44), (0x574767AA, 544), (0x2201F246, 25), (0x7FA71ABA, 8), (0x9AE1B6C9, 4), (0x3030ACD1, 4),
           (0x8B870EDE, 4), (0x0709AF8B, 5), (0x58314246, 24), (0x37BDBC25, 4)]


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def record(tag, payload):
    n = 12 + len(payload)
    return struct.pack(">IIII", n, n, tag, zlib.crc32(payload) & 0xFFFFFFFF) + payload


def make_save(loadouts, revision=105, fill=0x11, pad=0):
    """A save shaped like the game's: `loadouts` persona entries; `pad` extra bytes in the per-persona progress record
    (to keep a save's size while its loadouts drop)."""
    body = b""
    for tag, size in RECORDS:
        if size is None:
            payload = bytes([loadouts]) + bytes([fill]) * (104 * loadouts)
        else:
            payload = bytes([(tag + i) & 0xFF for i in range(size + (pad if tag == 0x574767AA else 0))])
        body += record(tag, payload)
    inner = struct.pack(">II", 0, revision) + body
    inner = struct.pack(">I", TAG) + struct.pack(">I", zlib.crc32(inner) & 0xFFFFFFFF) + inner
    inner = struct.pack(">II", len(inner) + 4, len(inner) + 4) + inner
    blob = struct.pack(">I", TAG) + struct.pack(">I", zlib.crc32(inner) & 0xFFFFFFFF) + inner
    return struct.pack(">I", len(blob) + 4) + blob


def main():
    ps = server_modules.load()
    check("AC3_DATA_DIR is the temporary one", os.path.abspath(ps.DATA_DIR).startswith(os.path.abspath(TMP)), ps.DATA_DIR)
    ps.DATA_DIR = TMP
    addr = ("10.0.2.1", 3074)
    ps.PLAYER_IDENTITIES[addr] = {"account": "tester", "name": "tester", "pid": 4000}
    path = os.path.join(TMP, "onlinesave_tester.bin")
    hist = os.path.join(TMP, "history")

    def upload(blob):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _label, resp = ps.handle_onlinesave_set(5, struct.pack("<H", len(blob)) + blob, addr)
        ok, _err, proto, _call, method, params = ps.parse_rmc_response(resp)
        return (ok and proto == 0x77 and method == 1 and params == b""), out.getvalue()

    def stored():
        with open(path, "rb") as f:
            disk = f.read()
        return disk if ps.ONLINESAVE_MEM.get("tester") == disk else None

    def refused_files():
        return sorted(n for n in os.listdir(hist) if "_refused" in n) if os.path.isdir(hist) else []

    print("\nthe synthetic saves")
    full, empty, old = make_save(82), make_save(0, revision=106), make_save(24)
    check("the full and the empty save: 10,234 / 1,706 bytes", (len(full), len(empty)) == (10234, 1706),
          f"{len(full)} / {len(empty)}")
    check("well-formed, loadouts read back (82 / 0 / 24)",
          all(ps.onlinesave_framed(b) for b in (full, empty, old))
          and [ps.onlinesave_loadouts(b) for b in (full, empty, old)] == [82, 0, 24])

    print("\nthe guard (default 0.5)")
    check("the default is 0.5", ps.ONLINESAVE_SHRINK_GUARD == 0.5, str(ps.ONLINESAVE_SHRINK_GUARD))
    ok, _log = upload(old)
    check("the first upload (nothing stored) is stored", ok and stored() == old)
    ok, _log = upload(full)
    check("a normal growth (24 -> 82 personas, 3.2 -> 10 KB) is accepted", ok and stored() == full)
    small_change = make_save(82, revision=106, fill=0x12)
    ok, _log = upload(small_change)
    check("a normal small change (the loadouts' bytes, the revision) is accepted", ok and stored() == small_change)
    ok, log = upload(empty)
    check("the 1.7 KB save over the 10 KB one is REFUSED, the stored save intact",
          ok and stored() == small_change, log)
    check("the log says so loudly (*** ... REFUSED, sizes named)",
          "*** OnlineSave.Set for 'tester' REFUSED" in log and "1706 bytes" in log, log)
    files = refused_files()
    check("the refused upload is kept in history/ as <account>_<time>_refused.bin",
          len(files) == 1 and files[0].startswith("tester_")
          and open(os.path.join(hist, files[0]), "rb").read() == empty, str(files))
    with contextlib.redirect_stdout(io.StringIO()):
        _label, resp = ps.handle_onlinesave_get(6, b"", addr)
    params = ps.parse_rmc_response(resp)[5]
    check("the next login gets the stored save", params[:1] == b"\x01" and params[3:] == small_change)
    few = make_save(30, pad=104 * 52)
    ok, log = upload(few)
    check("the same size with far fewer loadouts (82 -> 30) is refused", ok and stored() == small_change
          and "persona loadout" in log and len(few) >= len(small_change), log)
    bad = bytearray(full)
    bad[-1] ^= 0xFF
    ok, log = upload(bytes(bad))
    check("a damaged upload (outer CRC wrong) over a good save is refused", ok and stored() == small_change
          and "not a well-formed" in log, log)
    ok, _log = upload(make_save(82, revision=107))
    check("and the next good upload is stored", ok and stored() == make_save(82, revision=107))

    print("\nthe history")
    ps.HISTORY_KEEP = 2
    for rev in (108, 109, 110):
        upload(make_save(82, revision=rev))
    check("pruning the snapshots never removes a refused upload", len(refused_files()) == 3, str(refused_files()))
    ps.HISTORY_KEEP = 200

    print("\nonlinesave_shrink_guard 0: off")
    ps.ONLINESAVE_SHRINK_GUARD = 0.0
    ok, log = upload(empty)
    check("the 1.7 KB save is stored like any other", ok and stored() == empty and "REFUSED" not in log, log)
    ps.ONLINESAVE_SHRINK_GUARD = 0.5
    ok, _log = upload(full)
    check("on again: the full save goes back over the small one (growth)", ok and stored() == full)
    ps.ONLINESAVE_MEM["tester"] = full[:5000]
    ok, _log = upload(empty)
    check("a damaged stored save is not protected: any upload replaces it", ok and stored() == empty)

    print("\nthe settings")
    import server_config
    check("onlinesave_shrink_guard is a known key", "onlinesave_shrink_guard" in server_config.KNOWN_KEYS)
    check("the example config documents it",
          '"onlinesave_shrink_guard": 0.5' in open(os.path.join(ROOT, "server_config.example.json"), encoding="utf-8").read())
    check("the start-up banner names it", "profile shrink guard: ON" in server_config.describe())

    print("\nall checks passed" if not FAILED else f"\n{len(FAILED)} FAILED: {', '.join(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
