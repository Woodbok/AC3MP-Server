"""The shop and the player's money: Shop (0x1B5B), Wallet (0x1A0A) and FriendsLeague (0x1C20), Ubisoft's
"aconlineservices" protocols with 16-bit ids.

    Shop m1  RequestItems        the catalogue: [list] { [u32 item][u32][u32][u32 price] }
    Shop m3  AddCurrency         [i32 a][i32 b] earned (+) or spent (-)    -> [u32 0][u32 0], as Ubisoft replied
    Shop m4  GetCurrency         the balance                               -> [u32 a][u32 b]
    Shop m5  BuyItem             [u32 item][u32][u32 price]                -> [bool][u32 a][u32 b][bought item]
    Shop m6  UnlockItem          [u32 item]                                -> [bool true][bought item]
    Shop m7  RequestBoughtItems  the player's purchases                    -> [list] { bought item }
    Wallet m1                    the Extra Content button's URL            -> [string] `extra_content_url`
    FriendsLeague m1, m2         goals / progress                          -> empty

    a bought item (PlayerBoughtShopItem) = [u32 item][u64 DateTime][u64 0]

The money is the SERVER's: the game asks for its balance and reports what it earned, and the balance is kept per
player in DATA_DIR/currency_<account>.json, starting at `currency`. Match rewards go to the second field, and a
purchase is charged to it; a purchase the balance cannot cover is granted without charging, rather than refused or
left hanging. Purchases persist in purchases_<account>.json. UnlockItem answers "owned" for every item.

Prices are server-side too: the catalogue we send is what the store charges, scaled by `price_multiplier` (0 makes
everything free). It is DATA_DIR/shop_catalogue.json, a list of {"item": uid, "price": n} records (optionally "f2",
"f3"); without the file the store is empty, which the game copes with.

The task names come from the game's AC_Online_Services::OnlineTask* classes, each resolved through its vtable to the
stub it calls; layouts from the response readers (Shop dispatcher FUN_00E9A800) and, where present, Ubisoft's replies.
"""

import json
import os
import struct

import server_config as config
from ac3server import storage

from ac3server.players.identity import onlinesave_user
from ac3server.protocol.datatypes import q_datetime_now, read_string, read_u32, write_string
from ac3server.protocol.rmc import build_rmc_response
from ac3server.storage import atomic_write, set_aside


SHOP_PROTO = 0x1B5B
FRIENDSLEAGUE_PROTO = 0x1C20
WALLET_PROTO = 0x1A0A


def handle_wallet_get_url(call_id, params, addr):            # Wallet m1
    """The "Extra Content" button opens whatever URL this returns (OpenWalletUrlSequence fetches it with this call).
    Request: [string "extracontent"][u32 0][u32 0][string language][string locale][string region]. The reply is one
    string (the Wallet dispatcher FUN_00E9B0C0 reads method 1 with the string reader); empty still completes the
    step, while no answer stalls it for 15 s."""
    key, language, locale, region = "?", "", "", ""
    try:
        key, pos = read_string(params, 0)
        _a, pos = read_u32(params, pos)
        _b, pos = read_u32(params, pos)
        language, pos = read_string(params, pos)
        locale, pos = read_string(params, pos)
        region, pos = read_string(params, pos)
        if pos != len(params):
            print(f"[{addr}]   Wallet.GetUrl: {len(params) - pos} trailing "
                  f"byte(s) - layout may be incomplete")
    except Exception as e:
        print(f"[{addr}]   Wallet.GetUrl: could not parse the {len(params)}-byte "
              f"request ({e})")
    resp = bytearray()
    write_string(resp, config.EXTRA_CONTENT_URL)
    print(f"[{addr}]   Wallet.GetUrl for {key!r} ({language}/{locale}/{region}) "
          f"-> {config.EXTRA_CONTENT_URL!r}"
          + ("" if config.EXTRA_CONTENT_URL else
             " (empty - set extra_content_url to send the button somewhere)"))
    return "Wallet.GetUrl", build_rmc_response(WALLET_PROTO, call_id, 1,
                                               bytes(resp))


def _shop_bought_item(item_id: int) -> bytes:
    return struct.pack('<IQQ', item_id, q_datetime_now(), 0)


PURCHASES_MEM = {}


def _purchases_path(user):
    return os.path.join(storage.DATA_DIR, f"purchases_{user}.json")


def _purchases_get(addr):
    user = onlinesave_user(addr)
    if user in PURCHASES_MEM:
        return PURCHASES_MEM[user]
    items = []
    path = _purchases_path(user)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, list):
                items = [(int(i["item"]), int(i["date"])) for i in stored]
        except Exception as exc:
            set_aside(path, exc)
    PURCHASES_MEM[user] = items
    return items


def _purchases_add(addr, item_id, when):
    items = _purchases_get(addr)
    if any(i == item_id for i, _ in items):
        return items
    items.append((item_id, when))
    user = onlinesave_user(addr)
    PURCHASES_MEM[user] = items
    try:
        os.makedirs(storage.DATA_DIR, exist_ok=True)
        atomic_write(_purchases_path(user), json.dumps([{"item": i, "date": d} for i, d in items]))
    except OSError as exc:
        print(f"[{addr}]   could not persist purchases ({exc}) - kept in memory only")
    return items


def handle_shop_bought_items(call_id, params, addr):          # m7
    items = list(_purchases_get(addr))
    resp = bytearray(struct.pack('<I', len(items)))
    for item_id, when in items:
        resp += struct.pack('<IQQ', item_id, when, 0)
    print(f"[{addr}]   Shop.RequestBoughtItems -> {len(items)} item(s) {[hex(i) for i, _ in items]}")
    return "Shop.RequestBoughtItems", build_rmc_response(SHOP_PROTO, call_id, 7, bytes(resp))


SHOP_CATALOGUE_PATH = os.path.join(storage.DATA_DIR, "shop_catalogue.json")


def _load_shop_catalogue():
    if not os.path.isfile(SHOP_CATALOGUE_PATH):
        print(f"No shop catalogue at {SHOP_CATALOGUE_PATH} - serving an empty store.")
        return []
    try:
        with open(SHOP_CATALOGUE_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as exc:
        print(f"Shop catalogue unreadable ({exc}) - serving an empty store")
        return []
    items = []
    for entry in raw:
        price = int(round(int(entry["price"]) * config.PRICE_MULTIPLIER))
        items.append((int(entry["item"]), int(entry.get("f2", 1)),
                      int(entry.get("f3", 0)), max(0, price)))
    scaled = "" if config.PRICE_MULTIPLIER == 1.0 else f", prices scaled by {config.PRICE_MULTIPLIER:g}"
    print(f"Loaded {len(items)} shop item(s) from {SHOP_CATALOGUE_PATH}{scaled}")
    return items


SHOP_CATALOGUE = _load_shop_catalogue()


def handle_shop_catalog(call_id, params, addr):               # m1
    resp = bytearray(struct.pack('<I', len(SHOP_CATALOGUE)))
    for item_id, f2, f3, price in SHOP_CATALOGUE:
        resp += struct.pack('<IIII', item_id, f2, f3, price)
    print(f"[{addr}]   Shop.RequestItems -> {len(SHOP_CATALOGUE)} item(s)")
    return "Shop.RequestItems", build_rmc_response(SHOP_PROTO, call_id, 1, bytes(resp))


CURRENCY_MEM = {}


def _currency_path(user):
    return os.path.join(storage.DATA_DIR, f"currency_{user}.json")


def _currency_get(addr):
    user = onlinesave_user(addr)
    if user in CURRENCY_MEM:
        return CURRENCY_MEM[user]
    balance = list(config.STARTING_CURRENCY)
    path = _currency_path(user)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, list) and len(stored) == 2:
                # masked to u32, as the game sees it
                balance = [int(stored[0]) & 0xFFFFFFFF, int(stored[1]) & 0xFFFFFFFF]
        except Exception as exc:
            set_aside(path, exc)
    CURRENCY_MEM[user] = balance
    return balance


def _currency_save(addr, balance):
    user = onlinesave_user(addr)
    CURRENCY_MEM[user] = balance
    try:
        os.makedirs(storage.DATA_DIR, exist_ok=True)
        atomic_write(_currency_path(user), json.dumps(balance))
    except OSError as exc:
        print(f"[{addr}]   could not persist currency ({exc}) - kept in memory only")


def handle_shop_get_currency(call_id, params, addr):          # m4
    balance = _currency_get(addr)
    print(f"[{addr}]   Shop.GetCurrency -> {balance[0]}, {balance[1]}")
    return "Shop.GetCurrency", build_rmc_response(
        SHOP_PROTO, call_id, 4, struct.pack('<II', balance[0] & 0xFFFFFFFF,
                                            balance[1] & 0xFFFFFFFF))


def handle_shop_add_currency(call_id, params, addr):          # m3
    """The deltas are SIGNED: the game spends through AddCurrency too - Spend (0x008DA346) sends
    AddCurrency(0, -price) (a craft, a paid ability-set switch), a match reward AddCurrency(0, +earned). Read as
    unsigned, a spend of p would add 2^32 - p. A delta that would take a field below 0 stops it at 0 (the game checks
    the balance first, so it should never happen)."""
    balance = _currency_get(addr)
    if len(params) >= 8:
        a, b = struct.unpack_from('<ii', params, 0)
        want = [balance[0] + a, balance[1] + b]
        balance = [min(max(v, 0), 0xFFFFFFFF) for v in want]
        _currency_save(addr, balance)
        print(f"[{addr}]   Shop.AddCurrency {a:+d}, {b:+d} -> {balance[0]}, {balance[1]}"
              + ("" if balance == want else f" *** clamped (would be {want[0]}, {want[1]})"))
    else:
        print(f"[{addr}]   Shop.AddCurrency: {len(params)}-byte request, no deltas - ignored")
    # Ubisoft replied with eight zero bytes, not the new balance
    return "Shop.AddCurrency", build_rmc_response(SHOP_PROTO, call_id, 3,
                                                  struct.pack('<II', 0, 0))


def handle_shop_buy_item(call_id, params, addr):              # m5
    """Request from the stub's serializer (0x00E9ACF0), reply from its reader (FUN_00E9A340). The two u32s are taken
    to be the new balance; that is inferred, not confirmed."""
    item_id = price = other = 0
    if len(params) >= 12:
        item_id, other, price = struct.unpack_from('<III', params, 0)
    else:
        print(f"[{addr}]   Shop.BuyItem: {len(params)}-byte request, expected 12 - "
              f"answering success with the balance unchanged")
    balance = _currency_get(addr)
    if price and balance[1] >= price:
        balance = [balance[0], balance[1] - price]
        _currency_save(addr, balance)
        note = f"charged {price} -> {balance[0]}, {balance[1]}"
    elif price:
        note = (f"price {price} exceeds the balance {balance[1]} - granting it "
                f"anyway and leaving the balance alone")
    else:
        note = "no price in the request - balance unchanged"
    print(f"[{addr}]   Shop.BuyItem item {item_id:#010x} (field2={other}): {note}")
    when = q_datetime_now()
    _purchases_add(addr, item_id, when)
    resp = bytearray()
    resp.append(1)                                              # success
    resp += struct.pack('<II', balance[0] & 0xFFFFFFFF, balance[1] & 0xFFFFFFFF)
    resp += struct.pack('<IQQ', item_id, when, 0)
    return "Shop.BuyItem", build_rmc_response(SHOP_PROTO, call_id, 5, bytes(resp))


def handle_shop_unlock_item(call_id, params, addr):
    item_id = struct.unpack_from('<I', params, 0)[0] if len(params) >= 4 else 0
    print(f"[{addr}]   Shop.6 item id {item_id:#010x} -> true + item with a live DateTime, as Ubisoft did")
    return "Shop.6", build_rmc_response(SHOP_PROTO, call_id, 6, bytes([1]) + _shop_bought_item(item_id))


def handle_friendsleague_goals(call_id, params, addr):       # m1
    resp = struct.pack('<I', 0) + struct.pack('<Q', 0) + struct.pack('<Q', q_datetime_now())
    return "FriendsLeague.1", build_rmc_response(FRIENDSLEAGUE_PROTO, call_id, 1, resp)


def handle_friendsleague_save_progress(call_id, params, addr):
    return "FriendsLeague.2", build_rmc_response(FRIENDSLEAGUE_PROTO, call_id, 2, b"")
