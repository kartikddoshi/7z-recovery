"""The 32-byte signature header at the start of every .7z file, and what it tells us.

Layout (little endian):

    0   6  magic  37 7A BC AF 27 1C
    6   2  format version (major, minor)
    8   4  CRC32 of bytes 12..31
    12  8  NextHeaderOffset  (counted from byte 32)
    20  8  NextHeaderSize
    28  4  NextHeaderCRC

The "next header" (usually called the end header) sits after all the packed data and
holds file names, sizes, CRCs and the coder settings. 7-Zip writes it last, so an
interrupted copy or download loses it first.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass
from typing import BinaryIO, Optional

MAGIC = b"7z\xbc\xaf'\x1c"
HEADER_SIZE = 32


@dataclass
class Diagnosis:
    file_size: int
    has_magic: bool
    start_header_ok: bool = False
    version: str = ""
    end_header_offset: Optional[int] = None   # absolute offset
    end_header_size: Optional[int] = None
    expected_size: Optional[int] = None       # file size the header implies
    end_header_state: str = "unknown"         # ok | zeros | corrupt | missing | unknown

    @property
    def packed_end(self) -> int:
        """Where packed data can end at the latest (absolute offset)."""
        if self.start_header_ok and self.end_header_offset is not None:
            return min(self.file_size, self.end_header_offset)
        return self.file_size

    def lines(self) -> list[str]:
        out = [f"file size        : {self.file_size:,} bytes"]
        if not self.has_magic:
            out.append("signature        : NOT a 7z signature at offset 0 (scanning anyway)")
            return out
        if not self.start_header_ok:
            out.append("start header     : damaged (CRC mismatch); the end header position is unknown")
            return out
        out.append(f"start header     : ok (format {self.version})")
        out.append(f"expected size    : {self.expected_size:,} bytes")
        if self.expected_size is not None and self.expected_size > self.file_size:
            out.append(f"                   file is {self.expected_size - self.file_size:,} bytes SHORTER than that (truncated)")
        elif self.expected_size is not None and self.expected_size < self.file_size:
            out.append(f"                   file is {self.file_size - self.expected_size:,} bytes longer than that")
        text = {
            "ok": "present and its CRC matches. 7-Zip should be able to list this archive; try `7z x` first",
            "zeros": "the place where it should be is all zeros. File names and folders are lost",
            "corrupt": "something is there but its CRC does not match. File names and folders are probably lost",
            "missing": "lies beyond the end of the file. File names and folders are lost",
        }.get(self.end_header_state, "unknown")
        out.append(f"end header       : {text}")
        return out


def diagnose(f: BinaryIO) -> Diagnosis:
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)
    head = f.read(HEADER_SIZE)
    d = Diagnosis(file_size=size, has_magic=head[:6] == MAGIC)
    if not d.has_magic or len(head) < HEADER_SIZE:
        return d
    d.version = f"{head[6]}.{head[7]}"
    stored_crc, offset, hsize, next_crc = struct.unpack_from("<IQQI", head, 8)
    d.start_header_ok = zlib.crc32(head[12:32]) == stored_crc
    if not d.start_header_ok:
        return d
    d.end_header_offset = HEADER_SIZE + offset
    d.end_header_size = hsize
    d.expected_size = HEADER_SIZE + offset + hsize
    if d.expected_size > size:
        d.end_header_state = "missing"
        return d
    if hsize > (64 << 20):          # absurd; do not read it
        d.end_header_state = "corrupt"
        return d
    f.seek(d.end_header_offset)
    blob = f.read(hsize)
    if zlib.crc32(blob) == next_crc and hsize > 0:
        d.end_header_state = "ok"
    elif blob.count(0) == len(blob):
        d.end_header_state = "zeros"
    else:
        d.end_header_state = "corrupt"
    return d
