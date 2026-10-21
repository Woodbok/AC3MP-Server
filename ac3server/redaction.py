"""Keeping credentials out of the log.

LoginEx and RegisterEx carry the player's Ubisoft name, online key and - from a stock client - their real Ubisoft
password, in the clear once the fixed-key RC4 is undone. The server never needs the password (the ticket uses a
constant, protocol/crypto.py), so the secret fields are replaced the moment they are parsed and a packet carrying
them is never hex-dumped. Logs must be safe to share, and on a public server they would otherwise hold every
player's password. patcher/ac3mp_patch.py stops the client sending it at all.
"""

from ac3server.protocol.datatypes import read_string


LOGIN_CUSTOM_DATA_CLASS = "UbiAuthenticationLoginCustomData"
REDACTED = "<redacted>"


def _redacted(value):
    """Record only whether a secret was sent, never its content."""
    return REDACTED if value else value


def login_secrets(username, online_key, password):
    """The two secret fields as they may be logged.

    The client patch (patcher/ac3mp_patch.py) makes the serializer send the
    username in all three slots. A stock client's onlineKey is `3XP-...` and never equals the
    username, so three identical fields mean a patched client, and saying so
    reveals nothing that the username field does not already show."""
    if username and online_key == username and password == username:
        return {"onlineKey": "<username - patched client>",
                "password": "<username - patched client>"}
    return {"onlineKey": _redacted(online_key), "password": _redacted(password)}


def read_login_custom_data(params, pos):
    """(class name, username, onlineKey, password) of an AnyObjectHolder
    carrying `UbiAuthenticationLoginCustomData`; the three strings are None
    for any other class. Raw: never to print."""
    class_name, pos = read_string(params, pos)
    pos += 8  # two discarded u32 fields, per acb-rdv's ProcessData
    username = online_key = password = None
    if class_name == LOGIN_CUSTOM_DATA_CLASS:
        username, pos = read_string(params, pos)
        online_key, pos = read_string(params, pos)
        password, pos = read_string(params, pos)
    return class_name, username, online_key, password


def loggable_payload(payload: bytes) -> str:
    """A payload as hex for the log, unless it carries login credentials."""
    if LOGIN_CUSTOM_DATA_CLASS.encode() in payload:
        return f"<{len(payload)} bytes carrying login credentials - not logged>"
    return payload.hex()
