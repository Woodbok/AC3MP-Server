"""
Minimal CXB reader, ported from CXBTool's ExtractCXB, for round-trip
verification of cxb_builder.py's output and for decoding Ubisoft's served
CXB files. Supports both uncompressed (is_compressed=0) blocks, the only
kind cxb_builder.py writes, and LZO2A-compressed (is_compressed=1) blocks
via lzo2a.py.
"""
import re
import struct
import zlib

from lzo2a import lzo2a_decompress

MAGIC = 0x1004FA9957FBAA33
MAGIC_BYTES = struct.pack("<Q", MAGIC)


def _guess_segment_names(data: bytes, first_index: int, count: int):
    """Best-effort segment naming from the preamble's plaintext (name, size)
    token list. Used for display only (see parse_cxb): segment boundaries
    come from each segment's own header, because not every served file has
    a clean plaintext preamble (some have valid LZO2A segments but binary
    data where a preamble would be). Returns None if the preamble doesn't
    look usable.
    """
    preamble = bytes(b if 32 <= b < 127 else 0x20 for b in data[:first_index])
    tokens = re.findall(rb"\S+", preamble)
    if len(tokens) < 2 * count:
        return None
    names = []
    for i in range(0, 2 * count, 2):
        try:
            int(tokens[i + 1])  # just validating this looks like a real (name, size) pair
        except ValueError:
            return None
        names.append(tokens[i].decode("ascii", "replace"))
    return names


def parse_cxb(data: bytes) -> dict:
    """Returns {segment_name: xml_text}. A segment that fails to parse
    (checksum mismatch, truncated data) is included as a `_ERROR_<name>` key
    with the exception text rather than aborting the whole file, so the
    segments of a partial file that did parse are still returned."""
    indices = [i for i in range(len(data) - 8) if data[i:i + 8] == MAGIC_BYTES]
    if not indices:
        raise ValueError("no magic number found in CXB data")

    names = _guess_segment_names(data, indices[0], len(indices))

    result = {}
    for seg_num, index in enumerate(indices):
        name = names[seg_num] if names else f"segment_{seg_num}"
        start = index - 4
        try:
            uncompressed_size = struct.unpack("<I", data[start:start + 4])[0]
            if data[start + 4:start + 12] != MAGIC_BYTES:
                raise ValueError("magic mismatch at expected segment start")
            pos = start + 19  # 4 (uncompressed_size) + 8 (magic) + 2 (version) + 1 (algo) + 2 + 2
            xml_bytes = bytearray()
            # Segment length is self-describing (own header's uncompressed_size,
            # reached by walking blocks) - NOT derived from the preamble's size
            # token, which isn't always trustworthy.
            while len(xml_bytes) < uncompressed_size:
                if pos >= len(data):
                    raise ValueError(f"ran out of data ({len(xml_bytes)}/{uncompressed_size} bytes recovered)")
                is_compressed = data[pos]; pos += 1
                if is_compressed:
                    en_size, de_size, checksum = struct.unpack("<III", data[pos:pos + 12]); pos += 12
                    comp_data = data[pos:pos + en_size]; pos += en_size
                    if len(comp_data) < en_size:
                        raise ValueError(f"truncated compressed block ({len(xml_bytes)}/{uncompressed_size} bytes recovered)")
                    # lzo_adler32() is called with an initial seed of 0, not the
                    # usual Adler-32 seed of 1 - zlib.adler32's `value` param
                    # matches that exactly (same algorithm, per lzo_util.c).
                    if zlib.adler32(comp_data, 0) != checksum:
                        raise ValueError("LZO2A block checksum mismatch")
                    xml_bytes += lzo2a_decompress(comp_data, de_size)
                else:
                    data_size = struct.unpack("<I", data[pos:pos + 4])[0]; pos += 4
                    chunk = data[pos:pos + data_size]; pos += data_size
                    if len(chunk) < data_size:
                        raise ValueError(f"truncated raw block ({len(xml_bytes)}/{uncompressed_size} bytes recovered)")
                    xml_bytes += chunk
            if xml_bytes and xml_bytes[-1] == 0:
                xml_bytes = xml_bytes[:-1]
            result[name] = xml_bytes.decode("ascii")
        except Exception as e:
            result[f"_ERROR_{name}"] = str(e)
    return result
