"""Replace ONE segment of a CXB container, leaving every other byte alone.

Rebuilding a whole CXB through `cxb_builder.convert_to_cxb` works, but it
re-emits all 21 segments uncompressed and minified: 59,692 bytes become
198,760, and not one segment is Ubisoft's own bytes any more. The container
is delivered in 963-byte fragments over the reliable stream, and the
embedded default is the ground truth for this build, so the edit is kept
surgical.

The container is a flat table followed by the segment blobs:

    { [name, `name_field_size` bytes, NUL-padded][decimal size, 8 bytes] } *
    [trailer row, the same width]
    segment bytes, concatenated in table order

In the real embedded container the trailer row is **not** CXBTool's `0x30`
marker: it is the decimal string `3652845281`, NUL-padded to the full row
width, and it is a **checksum the client verifies**:

    crc32(the whole container, with the trailer's own name field zeroed)

A stale checksum makes the client reject the container and drop into
limited mode, so it is recomputed here.

One segment can then be re-emitted - uncompressed, which the format's own
reader supports through its per-block `is_compressed` flag - with its size
corrected in the table, the checksum recomputed, and every other segment
copied byte for byte.
"""
import zlib

import cxb_builder


def read_table(data: bytes, name_field_size: int = 40):
    """-> ([(name, size)], trailer row bytes, offset of the segment bytes)."""
    entries = []
    pos = 0
    stride = name_field_size + 8
    while pos + stride <= len(data):
        name = data[pos:pos + name_field_size].split(b"\x00")[0]
        size_raw = data[pos + name_field_size:pos + stride].split(b"\x00")[0]
        if name and size_raw.isdigit():
            entries.append((name.decode("ascii", "replace"), int(size_raw)))
            pos += stride
            continue
        trailer = data[pos:pos + stride]
        body = pos + stride
        accounted = sum(s for _n, s in entries)
        if not entries or accounted != len(data) - body:
            raise ValueError("CXB table sizes (%d) do not account for the body "
                             "(%d bytes from offset %d)"
                             % (accounted, len(data) - body, body))
        return entries, trailer, body
    raise ValueError("no CXB table trailer found")


def replace_segment(data: bytes, target: str, new_xml: str,
                    name_field_size: int = 40) -> bytes:
    """Return `data` with segment `target`'s content replaced by `new_xml`."""
    entries, trailer, body_start = read_table(data, name_field_size)
    if target not in [n for n, _s in entries]:
        raise KeyError("%r is not a segment of this container (%s)"
                       % (target, [n for n, _s in entries]))

    rebuilt = cxb_builder.build_segment(cxb_builder.minify_xml(new_xml))

    table = bytearray()
    body = bytearray()
    offset = body_start
    for name, size in entries:
        if name == target:
            blob = rebuilt
        else:
            blob = data[offset:offset + size]
            if len(blob) != size:
                raise ValueError("segment %r is truncated in the source" % name)
        offset += size
        table += name.encode("ascii")[:name_field_size].ljust(name_field_size, b"\x00")
        table += str(len(blob)).encode("ascii")[:8].ljust(8, b"\x00")
        body += blob
    table += b"\x00" * name_field_size + trailer[name_field_size:]
    return stamp_checksum(bytes(table) + bytes(body), len(table) - len(trailer),
                          name_field_size)


def add_segment(data: bytes, name: str, new_xml: str, name_field_size: int = 40) -> bytes:
    """Return `data` with one more segment, `name`, holding `new_xml`.

    For a game mode of our own (`gamemode_<engine name>`: the client looks a
    mode's XML up by that name, scanning the table row by row, so the order
    does not matter - it goes in alphabetical place like Ubisoft's own rows).
    Every existing segment is copied byte for byte; the table gains a row and
    the checksum is recomputed, exactly as replace_segment does.
    """
    entries, trailer, body_start = read_table(data, name_field_size)
    if name in [n for n, _s in entries]:
        raise KeyError("%r is already a segment of this container" % name)
    if len(name.encode("ascii")) >= name_field_size:
        raise ValueError("segment name %r is too long" % name)
    blobs, offset = {}, body_start
    for n, size in entries:
        blobs[n] = data[offset:offset + size]
        if len(blobs[n]) != size:
            raise ValueError("segment %r is truncated in the source" % n)
        offset += size
    blobs[name] = cxb_builder.build_segment(cxb_builder.minify_xml(new_xml))
    order = [n for n, _s in entries]
    order.insert(next((i for i, n in enumerate(order) if n > name), len(order)), name)
    table = bytearray()
    for n in order:
        table += n.encode("ascii").ljust(name_field_size, b"\x00")
        table += str(len(blobs[n])).encode("ascii")[:8].ljust(8, b"\x00")
    table += b"\x00" * name_field_size + trailer[name_field_size:]
    body = b"".join(blobs[n] for n in order)
    return stamp_checksum(bytes(table) + body, len(table) - len(trailer), name_field_size)


def container_checksum(data: bytes, trailer_offset: int, name_field_size: int = 40):
    """crc32 of the container with the trailer's own name field zeroed."""
    blanked = (data[:trailer_offset]
               + b"\x00" * name_field_size
               + data[trailer_offset + name_field_size:])
    return zlib.crc32(blanked)


def stamp_checksum(data: bytes, trailer_offset: int, name_field_size: int = 40):
    """Write the checksum into the trailer's name field, as decimal ASCII."""
    value = str(container_checksum(data, trailer_offset, name_field_size))
    field = value.encode("ascii")[:name_field_size].ljust(name_field_size, b"\x00")
    return data[:trailer_offset] + field + data[trailer_offset + name_field_size:]


def verify_checksum(data: bytes, name_field_size: int = 40):
    """-> (stored value, recomputed value). They must be equal."""
    entries, trailer, body = read_table(data, name_field_size)
    trailer_offset = body - len(trailer)
    stored = trailer[:name_field_size].split(b"\x00")[0]
    return (int(stored) if stored.isdigit() else None,
            container_checksum(data, trailer_offset, name_field_size))


def untouched_segments(original: bytes, patched: bytes, name_field_size: int = 40):
    """Names whose bytes are byte-identical between the two containers."""
    a_entries, _at, a_start = read_table(original, name_field_size)
    b_entries, _bt, b_start = read_table(patched, name_field_size)
    a_blobs, off = {}, a_start
    for name, size in a_entries:
        a_blobs[name] = original[off:off + size]
        off += size
    same, off = [], b_start
    for name, size in b_entries:
        if a_blobs.get(name) == patched[off:off + size]:
            same.append(name)
        off += size
    return same


__all__ = ["read_table", "replace_segment", "add_segment", "untouched_segments"]
