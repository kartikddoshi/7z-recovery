"""Builders for small synthetic files and archives used by the tests.

Nothing here is copied from real files: every sample is generated, so the test suite
carries no third-party data.
"""

from __future__ import annotations

import io
import lzma
import os
import random
import struct
import sys
import wave
import zipfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from salvage7z.container import MAGIC  # noqa: E402

WORDS = [b"alpha", b"bravo", b"charlie", b"delta", b"echo", b"foxtrot", b"golf", b"hotel", b"india", b"juliet"]


def noise(seed: int, n: int) -> bytes:
    """Incompressible bytes: LZMA2 stores these in uncompressed chunks."""
    return random.Random(seed).randbytes(n)


def texty(seed: int, n: int) -> bytes:
    """Compressible bytes: LZMA2 packs these into LZMA chunks."""
    rng = random.Random(seed)
    out = bytearray()
    while len(out) < n:
        out += rng.choice(WORDS) + b" " + str(rng.randrange(10 ** 6)).encode() + b"\n"
    return bytes(out[:n])


# ---------------------------------------------------------------------------
# LZMA2 streams and 7z containers
# ---------------------------------------------------------------------------

def raw_lzma2(data: bytes) -> bytes:
    """One raw LZMA2 stream, end marker included."""
    out = lzma.compress(data, format=lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA2, "preset": 1}])
    assert out.endswith(b"\0")
    return out


def blocked_stream(parts: list) -> tuple:
    """One stream made of independently packed blocks, as 7-Zip's multi-threaded encoder writes.

    Returns (stream bytes, [offset of each block within the stream]).
    """
    out = bytearray()
    offsets = []
    for part in parts:
        offsets.append(len(out))
        out += raw_lzma2(part)[:-1]
    out += b"\0"
    return bytes(out), offsets


def archive(streams: list, end_header: bytes = b"\x17" + b"H" * 39) -> bytes:
    """A .7z container: valid signature header, the given packed streams, an end header."""
    packed = b"".join(streams)
    start = struct.pack("<QQI", len(packed), len(end_header), zlib.crc32(end_header))
    return MAGIC + b"\x00\x04" + struct.pack("<I", zlib.crc32(start)) + start + packed + end_header


# ---------------------------------------------------------------------------
# sample files
# ---------------------------------------------------------------------------

def _wstr(s: str) -> bytes:
    return struct.pack("<I", len(s) + 1) + (s + "\0").encode("utf-16le")


def _tm(year, month, day, hour=0, minute=0, second=0) -> bytes:
    return struct.pack("<8i", second, minute, hour, day, month - 1, year - 1900, 0, 0)


def rhino_3dm(version: int = 4, payload: bytes = b"object-table-bytes" * 50, finished: bool = True) -> bytes:
    big = version >= 50
    fmt = "<q" if big else "<i"
    hl = 12 if big else 8

    def chunk(tcode: int, data: bytes) -> bytes:
        return struct.pack("<I", tcode) + struct.pack(fmt, len(data)) + data

    history = b"\x10" + _wstr("alice") + _tm(2020, 5, 17, 10, 30) + _wstr("bob") + _tm(2021, 3, 4, 8, 15) + struct.pack("<i", 7)
    notes = b"\x10" + struct.pack("<i", 0) + _wstr("ring, size 12")
    props = chunk(0x20008021, history) + chunk(0x20008022, notes) + struct.pack("<I", 0xFFFFFFFF) + struct.pack(fmt, 0)
    out = b"3D Geometry File Format " + str(version).rjust(8).encode()
    out += chunk(0x00000001, b"written by a test")
    out += chunk(0x10000014, props)
    out += chunk(0x10000015, b"settings")
    if not finished:
        # object table with its length never filled in, then a few object records and no end marker
        out += struct.pack("<I", 0x10000013) + struct.pack(fmt, 0)
        for i in range(3):
            out += chunk(0x20008070, payload + bytes([i]))
        return out
    out += chunk(0x10000013, payload)
    n = 8 if big else 4
    total = len(out) + hl + n
    return out + struct.pack("<I", 0x00007FFF) + struct.pack(fmt, n) + struct.pack("<Q" if big else "<I", total)


def stl(triangles: int = 40, header: bytes = b"Rhinoceros Binary STL ( test )") -> bytes:
    out = bytearray(header.ljust(80, b" ")) + struct.pack("<I", triangles)
    for i in range(triangles):
        out += struct.pack("<12fH", 0.0, 0.0, 1.0, i, 0.0, 0.0, i + 1.0, 0.0, 0.0, i, 1.0, 0.0, 0)
    return bytes(out)


def stl_ascii() -> bytes:
    return (b"solid test\n facet normal 0 0 1\n  outer loop\n   vertex 0 0 0\n   vertex 1 0 0\n   vertex 0 1 0\n"
            b"  endloop\n endfacet\nendsolid test\n")


def jpeg() -> bytes:
    def seg(marker: int, data: bytes) -> bytes:
        return bytes([0xFF, marker]) + struct.pack(">H", len(data) + 2) + data

    scan = b"\x12\x34\xff\x00\x56\xff\xd0\x78\x9a\xff\x00\xbc"       # stuffed FF and a restart marker
    return (b"\xff\xd8" + seg(0xE0, b"JFIF\0\x01\x01\0\0\x01\0\x01\0\0") + seg(0xDB, bytes(65))
            + seg(0xC0, bytes(9)) + seg(0xC4, bytes(20)) + seg(0xDA, bytes(6)) + scan + b"\xff\xd9")


def png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\0" + bytes([x, 0, 255 - x] * 8) for x in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def gif() -> bytes:
    return (b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0x80, 0, 0) + b"\0\0\0\xff\xff\xff"
            + b"\x21\xf9\x04\x00\x00\x00\x00\x00"
            + b"\x2c" + struct.pack("<HHHHB", 0, 0, 1, 1, 0) + b"\x02\x02\x4c\x01\x00" + b"\x3b")


def bmp() -> bytes:
    pixels = bytes(range(48))                                       # 4x4, 24 bit
    return (b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
            + struct.pack("<IiiHHIIiiII", 40, 4, 4, 1, 24, 0, len(pixels), 2835, 2835, 0, 0) + pixels)


def wav() -> bytes:
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(bytes(range(200)))
    return b.getvalue()


def zip_file(names=("readme.txt", "data/values.csv")) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(n, ("content of " + n + "\n") * 20)
    return b.getvalue()


def docx() -> bytes:
    return zip_file(("[Content_Types].xml", "word/document.xml", "_rels/.rels"))


def pdf() -> bytes:
    return (b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\nxref\n0 1\n0000000000 65535 f \n"
            b"trailer\n<< /Root 1 0 R >>\nstartxref\n50\n%%EOF\n")


def ole() -> bytes:
    """Smallest sensible compound file: header + FAT sector + directory sector + one data sector."""
    free, end, fat = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
    header = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(16) + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + bytes(6)
              + struct.pack("<IIIIIIIII", 0, 1, 1, 0, 4096, end, 0, end, 0)
              + struct.pack("<109I", 0, *([free] * 108)))
    assert len(header) == 512
    fat_sector = struct.pack("<128I", fat, end, end, *([free] * 125))
    directory = "WordDocument".encode("utf-16le").ljust(512, b"\0")
    return header + fat_sector + directory + bytes(range(256)) * 2


def seven_zip(payload: bytes = b"inner packed data " * 20) -> bytes:
    return archive([payload])


ALL = {
    "3dm": lambda: rhino_3dm(4),
    "3dm-v50": lambda: rhino_3dm(50),
    "stl": stl,
    "jpg": jpeg,
    "png": png,
    "gif": gif,
    "wav": wav,
    "zip": zip_file,
    "docx": docx,
    "pdf": pdf,
    "doc": ole,
    "7z": seven_zip,
    "bmp": bmp,
}
