"""Patch your own copy of AC3MP.exe (Assassin's Creed III multiplayer, v1.00)
to use a community server safely.

Two edits:

  login   ALWAYS applied. The stock game sends your real Ubisoft account
          password (and an account key) to whatever server it connects to,
          under encryption anyone can undo. After this patch it sends your
          username in those two slots instead. Two bytes in the function that
          writes the login message; the launcher, Ubisoft sign-in and every
          ownership check are untouched.

  host    Optional (--host). Replaces the built-in address
          `onlineconfigservice.ubi.com` with a server of your choice, so no
          hosts-file edit is needed. 27 characters at most.

The patcher only writes to a file it recognises byte for byte. Anything else
is refused rather than guessed at. The first time it changes a stock file it
keeps the original beside it as AC3MP.exe.orig, and --restore puts that back.

No Ubisoft file is included in or distributed with this script: it edits the
copy you already have.

Usage (PowerShell; quote the path, as install paths contain spaces and an
apostrophe):

  python ac3mp_patch.py "C:\\Games\\AC3\\AC3MP.exe" --check
  python ac3mp_patch.py "C:\\Games\\AC3\\AC3MP.exe"
  python ac3mp_patch.py "C:\\Games\\AC3\\AC3MP.exe" --host 203.0.113.7
  python ac3mp_patch.py "C:\\Games\\AC3\\AC3MP.exe" --restore

A game installed under "Program Files" needs an Administrator PowerShell.
"""
import argparse
import array
import hashlib
import os
import re
import shutil
import struct
import sys
import tempfile

KNOWN_BUILD = "v1.00 (disc, October 2012)"
STOCK_SIZE = 21590016
STOCK_SHA256 = "2fdbd0037bc41f06d299d8b2f60286ca252220a5868968fee0edffe1e4d8aa78"

# --- login: the UbiAuthenticationLoginCustomData serializer ---------------
# FUN_01166790 streams this+4 (username), this+8 (onlineKey) and this+0xC
# (password). The patch points the second and third at +4. The whole run of
# three calls is checked, so a different build can never be half-matched.
LOGIN_OFFSET = 0xD65BA1
LOGIN_STOCK = bytes.fromhex(
    "8d460450 57e87588feff"      # lea eax,[esi+4]; push eax; push edi; call write
    "8d4e0851 57e86b88feff"      # lea ecx,[esi+8]  - onlineKey
    "83c60c56 57e86188feff"      # add esi,0xC      - password
    "83c420")
LOGIN_PATCHED = bytes.fromhex(
    "8d460450 57e87588feff"
    "8d4e0451 57e86b88feff"      # lea ecx,[esi+4]  - username again
    "83c60456 57e86188feff"      # add esi,4        - username again
    "83c420")

# --- host: the OnlineConfig address ----------------------------------------
# A NUL-terminated string with exactly one NUL after it, between "http://"
# and the next string. Its only reader builds "http://" + host + path.
HOST_OFFSET = 0x104AA44
HOST_FIELD = 28                  # 27 characters + the terminating NUL
HOST_BEFORE = b"http://\x00"
HOST_AFTER = b"prud"
STOCK_HOST = "onlineconfigservice.ubi.com"
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(:[0-9]{1,5})?$")


class Refused(Exception):
    pass


def login_state(data):
    region = bytes(data[LOGIN_OFFSET:LOGIN_OFFSET + len(LOGIN_STOCK)])
    if region == LOGIN_STOCK:
        return "stock"
    if region == LOGIN_PATCHED:
        return "patched"
    return None


def read_host(data):
    """The host currently in the exe, or None if the field is not recognised."""
    if data[HOST_OFFSET - len(HOST_BEFORE):HOST_OFFSET] != HOST_BEFORE:
        return None
    end = HOST_OFFSET + HOST_FIELD
    if data[end:end + len(HOST_AFTER)] != HOST_AFTER:
        return None
    field = bytes(data[HOST_OFFSET:end])
    text, nul, rest = field.partition(b"\x00")
    if not nul or rest.strip(b"\x00") or not text:
        return None
    try:
        host = text.decode("ascii")
    except UnicodeDecodeError:
        return None
    return host if HOST_RE.match(host) else None


def pe_checksum_offset(data):
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    return pe + 0x18 + 64


def pe_checksum(data):
    """The PE image checksum (what imagehlp's CheckSumMappedFile computes)."""
    off = pe_checksum_offset(data)
    buf = bytearray(data)
    buf[off:off + 4] = b"\x00\x00\x00\x00"
    if len(buf) % 2:
        buf += b"\x00"
    total = sum(array.array("H", bytes(buf)))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (total + len(data)) & 0xFFFFFFFF


def stored_checksum(data):
    return struct.unpack_from("<I", data, pe_checksum_offset(data))[0]


def describe(data):
    login = login_state(data)
    host = read_host(data)
    lines = [
        "  login:  " + {"stock": "NOT patched - this exe sends your Ubisoft password",
                        "patched": "patched - sends your username, never your password",
                        None: "unrecognised"}[login],
        "  host:   " + ("unrecognised" if host is None else
                        host + (" (stock)" if host == STOCK_HOST else "")),
    ]
    if len(data) == STOCK_SIZE and hashlib.sha256(data).hexdigest() == STOCK_SHA256:
        lines.append(f"  build:  unmodified {KNOWN_BUILD}")
    return login, host, "\n".join(lines)


def require_known(login, host):
    if login is None or host is None:
        raise Refused(
            f"This AC3MP.exe is not the {KNOWN_BUILD} build this patcher knows, "
            "or it was modified by something else. Nothing was written. "
            "If you patched it earlier, try --restore first.")


def write_atomically(path, data):
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".ac3mp_patch_", dir=folder)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def patch(path, new_host):
    data = bytearray(open(path, "rb").read())
    login, host, report = describe(data)
    print("Before:\n" + report)
    require_known(login, host)

    if new_host is not None:
        if len(new_host) > HOST_FIELD - 1:
            raise Refused(f"--host is {len(new_host)} characters; at most "
                          f"{HOST_FIELD - 1} fit.")
        if not HOST_RE.match(new_host):
            raise Refused("--host must be a hostname or IPv4 address, optionally "
                          "with :port (letters, digits, '.', '-').")
        if ":" in new_host:
            print("  note: a :port in the host has not been tested with the game.")

    # Only keep the PE checksum consistent if it was consistent to begin with.
    # The stock v1.00 exe ships with a stale one (stored 0x014A6761, actual
    # 0x0149A809 - MapFileAndCheckSumW agrees), so it is left exactly as
    # Ubisoft shipped it.
    checksum_was_valid = stored_checksum(data) == pe_checksum(data)
    was_stock = login == "stock" and host == STOCK_HOST

    data[LOGIN_OFFSET:LOGIN_OFFSET + len(LOGIN_PATCHED)] = LOGIN_PATCHED
    if new_host is not None:
        field = new_host.encode("ascii")
        data[HOST_OFFSET:HOST_OFFSET + HOST_FIELD] = field + b"\x00" * (HOST_FIELD - len(field))
    if checksum_was_valid:
        struct.pack_into("<I", data, pe_checksum_offset(data), pe_checksum(data))

    if bytes(data) == open(path, "rb").read():
        print("Nothing to do - already patched that way.")
        return

    backup = path + ".orig"
    if was_stock and not os.path.exists(backup):
        shutil.copy2(path, backup)
        print(f"Original kept as {backup}")
    write_atomically(path, bytes(data))
    print("After:\n" + describe(data)[2])


def restore(path):
    backup = path + ".orig"
    if not os.path.exists(backup):
        raise Refused(f"No backup at {backup}; nothing to restore.")
    original = open(backup, "rb").read()
    login, host, report = describe(original)
    if login != "stock" or host != STOCK_HOST:
        raise Refused(f"{backup} is not an unpatched exe; refusing to restore it.\n{report}")
    write_atomically(path, original)
    print(f"Restored the original from {backup}:\n{report}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Patch your own AC3MP.exe (v1.00) so it never sends your "
                    "Ubisoft password, and optionally point it at a server.")
    ap.add_argument("exe", help="path to AC3MP.exe")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report only, change nothing")
    mode.add_argument("--restore", action="store_true", help="put back AC3MP.exe.orig")
    ap.add_argument("--host", help="server address to use instead of "
                                   f"{STOCK_HOST} (27 characters max)")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.exe):
        print(f"Not found: {args.exe}")
        return 2
    try:
        if args.check:
            print(describe(open(args.exe, "rb").read())[2])
        elif args.restore:
            restore(args.exe)
        else:
            patch(args.exe, args.host)
    except Refused as exc:
        print(f"Refused: {exc}")
        return 1
    except PermissionError:
        print("Permission denied writing next to the exe. Run PowerShell as "
              "Administrator, or patch a copy of the game outside Program Files.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
