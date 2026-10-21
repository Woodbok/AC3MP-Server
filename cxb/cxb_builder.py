"""
Minimal CXB file builder, ported from CXBTool's ConvertToCXB
(https://github.com/ACBMP/CXBTool, C++, LZO2A-compressed).

Writes uncompressed segment blocks instead of LZO2A-compressing them: the
format's reader (per CXBTool's ExtractCXB) branches on a per-block
`is_compressed` flag and supports a raw path, so no LZO2A compressor is
needed and the file is still valid.

XML content is re-parsed and re-serialized compactly (no indentation, no
comments) via xml.etree.ElementTree, mirroring pugixml's default behaviour in
CXBTool (comments dropped on parse, `format_raw` strips indentation).
"""
import re
import struct
import xml.etree.ElementTree as ET

MAGIC = 0x1004FA9957FBAA33
VERSION = 1
ALGO = 2
DECOMP_BUFFER_SIZE = 32768
COMP_BUFFER_SIZE = 0

# This format uses raw, unescaped "<N>" tokens inside attribute values as
# cross-reference IDs (e.g. StoreIDTag="<1>" ... value="<1>") - technically
# invalid XML (pugixml tolerates it, Python's strict parser doesn't). Escape
# before parsing, then restore the exact original raw form after
# serializing, since that's what the game's own parser expects on the wire.
_REF_TOKEN = re.compile(r"<(\d+)>")
_ESCAPED_REF_TOKEN = re.compile(rb"&lt;(\d+)&gt;")


def minify_xml(xml_text: str) -> bytes:
    xml_text = xml_text.rstrip("\x00")  # stray trailing NUL from some exports
    escaped = _REF_TOKEN.sub(r"&lt;\1&gt;", xml_text)
    root = ET.fromstring(escaped)
    serialized = ET.tostring(root, encoding="us-ascii", xml_declaration=True)
    return _ESCAPED_REF_TOKEN.sub(lambda m: b"<" + m.group(1) + b">", serialized)


def build_segment(xml_bytes: bytes) -> bytes:
    xml_bytes = xml_bytes + b"\x00"
    uncompressed_size = len(xml_bytes)
    header = struct.pack("<IQHBHH", uncompressed_size, MAGIC, VERSION, ALGO,
                          DECOMP_BUFFER_SIZE, COMP_BUFFER_SIZE)
    blocks = bytearray()
    for i in range(0, len(xml_bytes), DECOMP_BUFFER_SIZE):
        chunk = xml_bytes[i:i + DECOMP_BUFFER_SIZE]
        blocks.append(0)  # is_compressed = 0 (raw block)
        blocks += struct.pack("<I", len(chunk))
        blocks += chunk
    return header + bytes(blocks)


def convert_to_cxb(segments: list, name_field_size: int = 40) -> bytes:
    """segments: list of (name, xml_text) tuples. `name` becomes the internal
    segment name (matches CXBTool's convention of using the filename minus
    its extension)."""
    file_infos = bytearray()
    file_data = bytearray()

    for name, xml_text in segments:
        minified = minify_xml(xml_text)
        segment = build_segment(minified)
        file_data += segment

        base_name = name.split(".")[0]
        name_buf = base_name.encode("ascii")[:name_field_size].ljust(name_field_size, b"\x00")
        size_str = str(len(segment)).encode("ascii")[:8].ljust(8, b"\x00")
        file_infos += name_buf + size_str

    file_infos.append(0x30)
    file_infos += b"\x00" * (name_field_size + 7)

    return bytes(file_infos) + bytes(file_data)
