"""
Pure-Python port of liblzo2's LZO2A decompressor (lzo2a_d.ch /
lzo2a_d2.c / config2a.h, from the real LZO source, lzo-2.10), since no
Python binding for this specific LZO variant exists. `CXBTool`
(https://github.com/ACBMP/CXBTool, the community C++ tool that
reverse-engineered the CXB container format) links against liblzo2 for
this; this module ports the same bit-exact algorithm so we can decode
Ubisoft's served CXB files (`algo=2` in the segment header) without a C
toolchain.

liblzo2 is GPLv2+ (Markus F.X.J. Oberhumer) - this is a from-scratch
Python re-expression of its published algorithm/format, not a copy of its
source text, for the same interoperability purpose CXBTool itself serves.

Algorithm notes (SWD_N=8191 is fixed by upstream config2a.h regardless of
compression level - the alternate SWD_N>=8192 code path is unreachable in
any real liblzo2 build and is not implemented here):

- Bitstream: a 1-bit flag selects a literal byte vs. a match.
- A second 1-bit flag (only checked for non-literals) selects a short "M1"
  match: 2-5 byte copy, 1-byte back-offset (0-255).
- Otherwise it's a general match: 2 header bytes encode a 13-bit
  back-offset and a 3-bit length class. Length class 0 means a longer,
  separately-encoded length (a run of 0x00 bytes each adding 255, plus a
  final terminating byte); a back-offset of exactly 0 with length class 1
  is reserved as the end-of-stream marker.
"""


class Lzo2aError(ValueError):
    pass


def lzo2a_decompress(data: bytes, expected_size: int) -> bytes:
    """Decompresses one LZO2A-compressed block. `expected_size` is the
    block's own declared decompressed size (`de_size` in cxb_builder.py's
    terms) - used only as a sanity bound, not to pre-size the output."""
    ip = 0
    ip_end = len(data)
    out = bytearray()
    bit_buf = 0
    bit_count = 0

    def need_bits(j):
        nonlocal ip, bit_buf, bit_count
        if bit_count < j:
            if ip >= ip_end:
                raise Lzo2aError("input overrun while refilling bit buffer")
            bit_buf |= data[ip] << bit_count
            ip += 1
            bit_count += 8

    def mask_bits(j):
        return bit_buf & ((1 << j) - 1)

    def dump_bits(j):
        nonlocal bit_buf, bit_count
        bit_buf >>= j
        bit_count -= j

    def copy_match(m_pos, length):
        if m_pos < 0 or m_pos >= len(out):
            raise Lzo2aError("lookbehind overrun")
        if len(out) + length > expected_size:
            raise Lzo2aError("output overrun")
        for _ in range(length):
            out.append(out[m_pos])
            m_pos += 1

    while ip < ip_end:
        need_bits(1)
        if mask_bits(1) == 0:
            dump_bits(1)
            if ip >= ip_end:
                raise Lzo2aError("input overrun on literal")
            if len(out) + 1 > expected_size:
                raise Lzo2aError("output overrun on literal")
            out.append(data[ip])
            ip += 1
            continue
        dump_bits(1)

        need_bits(1)
        if mask_bits(1) == 0:
            dump_bits(1)
            # M1 match: length 2-5, 1-byte back-offset.
            need_bits(2)
            length = 2 + mask_bits(2)
            dump_bits(2)
            if ip >= ip_end:
                raise Lzo2aError("input overrun on M1 offset byte")
            offset = data[ip]
            ip += 1
            copy_match(len(out) - 1 - offset, length)
            continue
        dump_bits(1)

        # General match: 2 header bytes, 13-bit back-offset + 3-bit length class.
        if ip_end - ip < 2:
            raise Lzo2aError("input overrun on match header")
        byte1 = data[ip]
        byte2 = data[ip + 1]
        ip += 2
        m_pos = len(out) - ((byte1 & 31) | (byte2 << 5))
        length_class = byte1 >> 5

        if length_class == 0:
            length = 9  # 10 - 1, extended below
            if ip >= ip_end:
                raise Lzo2aError("input overrun on extended length")
            while data[ip] == 0:
                length += 255
                ip += 1
                if ip >= ip_end:
                    raise Lzo2aError("input overrun on extended length run")
            length += data[ip]
            ip += 1
        else:
            if m_pos == len(out):
                # Reserved offset-0 code = end-of-stream marker.
                return bytes(out)
            length = length_class + 2

        copy_match(m_pos, length)

    # Real LZO2A streams always end via the explicit EOF marker above -
    # running out of input first means the block was truncated/corrupt.
    raise Lzo2aError("no end-of-stream marker found before input ran out")
