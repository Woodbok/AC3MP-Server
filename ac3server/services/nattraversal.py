"""NATTraversal (protocol 0x03): the server's part in the hole punch between two players' games.

    m1 RequestProbeInitiation  [u32 count] { [u16 len][station url] }   a game asks to reach these peers -> nothing
    m2 InitiateProbe           [u16 len][station url]                  PUSHED by us to each of those peers

The server answers m1 empty, then pushes m2 to each target carrying the REQUESTER's public endpoint, so both games
send first and both NATs open; Ubisoft's server pushed it in that direction too. The pushed URL is
address;port;RVCID;sid, in that order and nothing more (services/secure.public_station_url). A target is found by
matching the URL's address and port against the connections we see; one nobody here matches is logged and skipped.
"""

from ac3server.notifications import SECURE_SERVER, next_notification_call_id
from ac3server.protocol.datatypes import read_string, read_u32, write_string
from ac3server.protocol.rmc import build_rmc_request, build_rmc_response
from ac3server.services.secure import CONNECTION_IDS, public_station_url


NAT_PROTO = 0x03


def _endpoint_of_url(url):
    """-> (address, port) from a station URL, or None."""
    fields = {}
    for part in url.split(":/", 1)[-1].split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key.strip().lower()] = value.strip()
    try:
        return fields["address"], int(fields["port"])
    except (KeyError, ValueError):
        return None


def _connection_for_endpoint(endpoint):
    """The connected address a station URL refers to, if any.

    An exact endpoint match first. Failing that, the same address on
    another port: a client's game port and the port its PRUDP connection
    reaches us from are normally the same, but a NAT that remaps per
    destination breaks that, and the address is still the useful half.
    """
    if endpoint is None:
        return None
    if endpoint in CONNECTION_IDS:
        return endpoint
    for known in list(CONNECTION_IDS):
        if known[0] == endpoint[0]:
            return known
    return None


def handle_nat_request_probe_initiation(call_id, params, addr):
    urls = []
    try:
        count, pos = read_u32(params, 0)
        for _ in range(count):
            url, pos = read_string(params, pos)
            urls.append(url)
    except Exception as e:
        print(f"[{addr}]   NATTraversal.RequestProbeInitiation: could not parse ({e})")
    reply = build_rmc_response(0x03, call_id, 1, b"")
    mine = public_station_url(addr, probe=True)
    sent, skipped = 0, []
    server = SECURE_SERVER[0]
    for url in urls:
        target = _connection_for_endpoint(_endpoint_of_url(url))
        if target is None or target == addr:
            skipped.append(url)
            continue
        payload = bytearray()
        write_string(payload, mine)
        request = build_rmc_request(NAT_PROTO,
                                    next_notification_call_id(target), 2,
                                    bytes(payload))
        print(f"[{target}]   -> InitiateProbe: send to {mine}")
        if server is not None and server.send_rmc_request(
                target, request, "NATTraversal.InitiateProbe"):
            sent += 1
    print(f"[{addr}]   NATTraversal.RequestProbeInitiation: {len(urls)} target(s), "
          f"{sent} probe(s) initiated"
          + (f"; no connection here matches {skipped}" if skipped else ""))
    return "NATTraversal.RequestProbeInitiation", reply
