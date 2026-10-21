"""OnlineSave (protocol 0x77): each player's profile blob - accolades, unlocks, loadouts, the emblem screen's choices.

    m1 Set  [u16 len][blob]   -> nothing         the game uploads its profile (after a match, after a change)
    m2 Get  nothing           -> [bool exists][u16 len][blob]      at login; exists = false for a new player

The blob is the game's own Hermes::Serializer image (big-endian length, CRC32("onlinesave_v010") as its tag, a
checksum, nested records). The server never interprets it: it stores what the game uploads, in
DATA_DIR/onlinesave_<account>.bin, and hands it back at the next login. Every upload is also kept in
DATA_DIR/history/ (`history_keep` caps how many), so a bad one can be rolled back and two can be compared to locate
a field.

The shrink guard (server_config `onlinesave_shrink_guard`): a game can upload a much smaller save whose
persona-loadout record (tag 0xAB0369C6: a count byte, then one 104-byte entry per persona) is EMPTY, and its next
start then rebuilds the loadouts from defaults, losing the player's choices. So while a well-formed save is stored, an
upload is refused when it is under `onlinesave_shrink_guard` (default 0.5) of the stored size, holds under that
fraction of its persona loadout entries, or is not a well-formed onlinesave_v010 blob itself. The stored save stays,
the refused one is kept as history/<account>_<time>_refused.bin (never pruned), the log says so with ***, and the
game gets the usual empty success reply (an error would read as ERROR_SAVEONLINE; this way its next login simply
reads the stored save). 0 turns the guard off.

Layout from the game's stubs (m2 FUN_00dd7390, m1 FUN_00dd72df), which Ubisoft's server matched to the byte; the
game gives up on Get after 15 s and reports error 8 (ERROR_SAVEONLINE). XP and level are NOT here: they are
statistics (services/statistics.py).
"""

import os
import struct
import time
import zlib

import server_config as config
from ac3server import storage

from ac3server.players.identity import onlinesave_user
from ac3server.protocol.rmc import build_rmc_response
from ac3server.storage import atomic_write, prune_history


ONLINESAVE_TAG_CRC = 0xa0eb491f  # zlib.crc32(b"onlinesave_v010")
ONLINESAVE_MEM = {}  # username -> blob, mirror of the on-disk file for this run
LOADOUT_TAG = 0xAB0369C6         # the persona-loadout record: [u8 count] + count x 104-byte entries
LOADOUT_ENTRY = 104
RECORDS_START = 0x24             # after the outer header (length, tag, crc, inner length x2, tag, crc, 0, revision)


def _onlinesave_path(user):
    return os.path.join(storage.DATA_DIR, f"onlinesave_{user}.bin")


def _onlinesave_load(user):
    if user in ONLINESAVE_MEM:
        return ONLINESAVE_MEM[user]
    path = _onlinesave_path(user)
    if os.path.isfile(path):
        with open(path, "rb") as f:
            ONLINESAVE_MEM[user] = f.read()
        return ONLINESAVE_MEM[user]
    return None


def _onlinesave_describe(blob):
    if len(blob) < 12:
        return "too short for a serializer header"
    be_len = struct.unpack_from(">I", blob, 0)[0]
    tag = struct.unpack_from(">I", blob, 4)[0]  # Hermes::Serializer fields are big-endian
    tag_ok = "onlinesave_v010" if tag == ONLINESAVE_TAG_CRC else f"unknown tag {tag:#010x}"
    return f"{len(blob)} bytes, header length {be_len}, {tag_ok}"


def onlinesave_framed(blob):
    """A well-formed onlinesave_v010 blob: its total length, its tag and its outer CRC32 (at 0x08, over 0x0C..)
    agree."""
    if not blob or len(blob) < RECORDS_START:
        return False
    total, tag, crc = struct.unpack_from(">III", blob, 0)
    return total == len(blob) and tag == ONLINESAVE_TAG_CRC and crc == zlib.crc32(blob[0x0C:]) & 0xFFFFFFFF


def onlinesave_loadouts(blob):
    """How many persona-loadout entries the blob holds, or None (no such record, or a layout not understood).

    Walks the top-level records - [u32 length][u32 length][u32 tag][u32 crc32][payload: length - 12], big-endian,
    the next at +4 + length - to the loadout record (LOADOUT_TAG)."""
    pos = RECORDS_START
    while pos + 16 <= len(blob):
        length, length2, tag = struct.unpack_from(">III", blob, pos)
        if length != length2 or length < 12 or pos + 4 + length > len(blob):
            return None
        if tag == LOADOUT_TAG:
            payload = blob[pos + 16:pos + 4 + length]
            if payload and len(payload) == 1 + LOADOUT_ENTRY * payload[0]:
                return payload[0]
            return None
        pos += 4 + length
    return None


def onlinesave_shrink_check(stored, blob, fraction=None):
    """-> None (store the upload) or why it must not replace the stored save (server_config onlinesave_shrink_guard).
    Only a well-formed stored save is protected: a damaged one is no better than whatever comes."""
    fraction = config.ONLINESAVE_SHRINK_GUARD if fraction is None else fraction
    if fraction <= 0 or not onlinesave_framed(stored):
        return None
    if not onlinesave_framed(blob):
        return f"not a well-formed onlinesave_v010 blob ({_onlinesave_describe(blob)})"
    if len(blob) < fraction * len(stored):
        return f"{len(blob)} bytes, under {fraction:g} of the stored {len(stored)}"
    had, has = onlinesave_loadouts(stored), onlinesave_loadouts(blob)
    if had and (has or 0) < fraction * had:
        return (f"{has if has is not None else 'no'} persona loadout(s), under {fraction:g} of the stored "
                f"save's {had}")
    return None


def _keep_refused(user, blob):
    """The refused upload, kept as history/<user>_<time>_refused.bin (prune_history's pattern never matches it)."""
    hist = os.path.join(storage.DATA_DIR, "history")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path, n = os.path.join(hist, f"{user}_{stamp}_refused.bin"), 1
    try:
        os.makedirs(hist, exist_ok=True)
        while os.path.exists(path):
            n += 1
            path = os.path.join(hist, f"{user}_{stamp}_refused-{n}.bin")
        with open(path, "wb") as f:
            f.write(blob)
        return path
    except OSError as e:
        return f"nowhere ({e})"


def handle_onlinesave_get(call_id, params, addr):     # m2
    user = onlinesave_user(addr)
    blob = _onlinesave_load(user)
    if blob is None or len(blob) > 0xFFFF:
        print(f"[{addr}]   OnlineSave.Get for '{user}': no stored save -> exists=false, empty buffer")
        resp = b"\x00" + struct.pack("<H", 0)
    else:
        print(f"[{addr}]   OnlineSave.Get for '{user}': returning stored save ({_onlinesave_describe(blob)})")
        resp = b"\x01" + struct.pack("<H", len(blob)) + blob
    return "OnlineSave.Get", build_rmc_response(0x77, call_id, 2, resp)


def handle_onlinesave_set(call_id, params, addr):     # m1
    user = onlinesave_user(addr)
    if len(params) >= 2:
        n = struct.unpack_from("<H", params, 0)[0]
        blob = params[2:2 + n]
        refused = onlinesave_shrink_check(_onlinesave_load(user), blob) if len(blob) == n and n > 0 else None
        if refused:
            kept = _keep_refused(user, blob)
            print(f"[{addr}]   *** OnlineSave.Set for '{user}' REFUSED: {refused}. The stored save "
                  f"({_onlinesave_describe(_onlinesave_load(user))}) stays; the upload is kept {kept}; the game is "
                  f"answered as if saved (onlinesave_shrink_guard {config.ONLINESAVE_SHRINK_GUARD:g})")
        elif len(blob) == n and n > 0:
            ONLINESAVE_MEM[user] = blob
            try:
                os.makedirs(storage.DATA_DIR, exist_ok=True)
                atomic_write(_onlinesave_path(user), blob)
                where = "stored on disk"
                # every version kept in history/ (pruned to history_keep): the only way to find a field in
                # an opaque blob is to compare snapshots either side of a change
                try:
                    hist = os.path.join(storage.DATA_DIR, "history")
                    os.makedirs(hist, exist_ok=True)
                    stamp = time.strftime("%Y%m%d_%H%M%S")
                    with open(os.path.join(hist, f"{user}_{stamp}.bin"), "wb") as f:
                        f.write(blob)
                    prune_history(hist, user, ".bin")
                except OSError:
                    pass
            except OSError as e:
                where = f"kept in memory only ({e})"
            print(f"[{addr}]   OnlineSave.Set for '{user}': {_onlinesave_describe(blob)}, {where}")
        else:
            print(f"[{addr}]   OnlineSave.Set for '{user}': length field {n} but {len(params) - 2} payload bytes - ignored")
    else:
        print(f"[{addr}]   OnlineSave.Set for '{user}': {len(params)}-byte params, no buffer - ignored")
    return "OnlineSave.Set", build_rmc_response(0x77, call_id, 1, b"")
