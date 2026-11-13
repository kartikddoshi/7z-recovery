"""Find and decode LZMA2 data without the archive's index.

An LZMA2 stream is a chain of chunks. Every chunk starts with a control byte:

    0x00         end of stream
    0x01         stored (uncompressed) chunk, dictionary reset
    0x02         stored chunk, no reset
    0x80..0xFF   LZMA chunk; bits 5-6 say what is reset:
                 0x80 nothing, 0xA0 state, 0xC0 state + new properties,
                 0xE0 state + new properties + dictionary

Stored chunk:  control, 2 bytes (size - 1), data.
LZMA chunk:    control (low 5 bits = high bits of unpacked size - 1), 2 bytes unpacked size - 1,
               2 bytes packed size - 1, [1 properties byte if control >= 0xC0], data.

Two consequences make header-less recovery possible:

* the chain can be walked without decompressing anything, because every chunk states its
  own packed size; and
* a chunk with a dictionary reset depends on nothing before it, so decoding can restart
  there. 7-Zip's multi-threaded encoder emits such a reset at the start of every block
  (4 x dictionary size by default), so damage costs at most one block of a solid stream.
"""

from __future__ import annotations

import lzma
import re
from dataclasses import dataclass, field
from typing import BinaryIO, Callable, Iterator, Optional

MIB = 1 << 20
VERIFY_CHUNKS = 24                      # chunks test-decoded before a resync point is trusted
DEFAULT_DICT_SIZES = (256 * MIB, 1536 * MIB)
_CANDIDATE = re.compile(rb"[\x01\xe0-\xff]")


@dataclass
class Block:
    """A run of chunks that starts with a dictionary reset: decodable on its own."""
    start: int
    end: int = 0
    unpacked: int = 0
    chunks: int = 0


@dataclass
class Stream:
    """One LZMA2 stream (a 7z "folder"/solid block), or the surviving part of one."""
    start: int
    end: int = 0
    unpacked: int = 0
    chunks: int = 0
    blocks: list = field(default_factory=list)
    complete: bool = False     # ended with a proper end marker
    resynced: bool = False     # found after damage: the beginning of the original stream is missing
    index: int = 0

    @property
    def packed(self) -> int:
        return self.end - self.start


@dataclass
class Gap:
    """A region the chunk chain could not cross."""
    start: int
    end: int
    zero_bytes: int = 0

    @property
    def kind(self) -> str:
        n = self.end - self.start
        if n and self.zero_bytes == n:
            return "zeros"
        return "mostly zeros" if self.zero_bytes > 0.99 * n else "damaged or not LZMA2"


@dataclass
class ScanResult:
    streams: list
    gaps: list
    gave_up_at: Optional[int] = None   # resync search stopped here (max_garbage reached)


class Window:
    """Cheap random access with a read-ahead buffer (chunk hops are short and forward)."""

    def __init__(self, f: BinaryIO, size: int, bufsize: int = MIB):
        self.f, self.size, self.bufsize = f, size, bufsize
        self.base, self.buf = 0, b""

    def get(self, pos: int, n: int) -> bytes:
        off = pos - self.base
        if off < 0 or off + n > len(self.buf):
            self.f.seek(pos)
            self.buf = self.f.read(max(self.bufsize, n))
            self.base = pos
            off = 0
        return self.buf[off:off + n]


def chunk_info(h: bytes):
    """Parse a chunk header. Returns (size_in_file, unpacked, is_lzma, has_props, resets_dict) or None."""
    if not h:
        return None
    c = h[0]
    if c in (1, 2):
        if len(h) < 3:
            return None
        u = ((h[1] << 8) | h[2]) + 1
        return 3 + u, u, False, False, c == 1
    if c >= 0x80:
        if len(h) < 5:
            return None
        u = (((c & 0x1F) << 16) | (h[1] << 8) | h[2]) + 1
        p = ((h[3] << 8) | h[4]) + 1
        if c >= 0xC0:
            if len(h) < 6 or h[5] > 224:        # properties byte = (pb * 5 + lp) * 9 + lc
                return None
            return 6 + p, u, True, True, c >= 0xE0
        return 5 + p, u, True, False, False
    return None


def walk(win: Window, pos: int, limit: int, max_chunks: Optional[int] = None) -> Optional[Stream]:
    """Follow the chunk chain from `pos`. None if `pos` cannot be the start of a stream."""
    s = Stream(start=pos)
    p = pos
    need_props = True
    block = None
    while True:
        if p >= limit or (max_chunks is not None and s.chunks >= max_chunks):
            break
        h = win.get(p, 6)
        if h[:1] == b"\0":
            if s.chunks == 0:
                return None
            s.complete = True
            p += 1
            break
        info = chunk_info(h)
        if info is None:
            break
        size, unpacked, is_lzma, has_props, resets = info
        if p + size > limit:
            break
        if s.chunks == 0 and not resets:
            return None
        if resets:
            need_props = True
        if is_lzma:
            if has_props:
                need_props = False
            elif need_props:
                break
        if resets:
            block = Block(start=p)
            s.blocks.append(block)
        p += size
        block.end = p
        block.unpacked += unpacked
        block.chunks += 1
        s.unpacked += unpacked
        s.chunks += 1
    if s.chunks == 0:
        return None
    s.end = p
    return s


def iter_chunks(win: Window, start: int, end: int) -> Iterator[tuple]:
    """Yield (offset, size_in_file, unpacked, is_lzma) for each chunk in [start, end)."""
    p = start
    while p < end:
        info = chunk_info(win.get(p, 6))
        if info is None:
            return
        yield p, info[0], info[1], info[2]
        p += info[0]


def _new_decoder(dict_size: int):
    return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA2, "dict_size": dict_size}])


def _is_sync_point(win: Window, pos: int, limit: int) -> bool:
    """True if a dictionary-reset chunk at `pos` starts data that really decodes."""
    s = walk(win, pos, limit, max_chunks=VERIFY_CHUNKS)
    if s is None:
        return False
    chunks = list(iter_chunks(win, s.start, s.end - (1 if s.complete else 0)))
    n_lzma = sum(1 for c in chunks if c[3])
    stopped_early = not s.complete and s.end < limit and s.chunks < VERIFY_CHUNKS
    # Stored chunks accept any bytes, so they prove little on their own.
    if n_lzma == 0 and len(chunks) < 4:
        return False
    if stopped_early and n_lzma < 2:
        return False
    win.f.seek(s.start)
    data = win.f.read(chunks[-1][0] + chunks[-1][1] - s.start)
    try:
        out = _new_decoder(DEFAULT_DICT_SIZES[0]).decompress(data)
    except lzma.LZMAError:
        # A reference further back than our dictionary also raises; chain length is the only evidence then.
        return n_lzma >= 8 and not stopped_early and _decodes_with(data, DEFAULT_DICT_SIZES[-1])
    return len(out) == s.unpacked


def _decodes_with(data: bytes, dict_size: int) -> bool:
    try:
        _new_decoder(dict_size).decompress(data)
        return True
    except (lzma.LZMAError, MemoryError):
        return False


def find_sync(win: Window, pos: int, limit: int, max_garbage: int) -> tuple:
    """Search forward for the next place decoding can restart.

    Returns (offset or None, zero_bytes_skipped, stopped_at). Runs of zeros are skipped
    quickly and do not count against `max_garbage`.
    """
    zeros = 0
    garbage = 0
    p = pos
    step = 4 * MIB
    lookahead = 2 * MIB         # room for _chain_plausible to follow a chain without touching the file
    while p < limit:
        win.f.seek(p)
        buf = win.f.read(min(step + lookahead, limit - p))
        if not buf:
            break
        n = min(step, len(buf))
        nz = buf.count(0, 0, n)
        if nz == n:
            zeros += nz
            p += n
            continue
        for m in _CANDIDATE.finditer(buf, 0, n):
            i = m.start()
            if not _chain_plausible(buf, i):
                continue
            win.base, win.buf = p, buf          # let the full check read from memory
            if _is_sync_point(win, p + i, limit):
                return p + i, zeros + buf.count(0, 0, i), p + i
        zeros += nz
        garbage += n - nz
        p += n
        if garbage > max_garbage:
            return None, zeros, p
    return None, zeros, p


def _chain_plausible(buf: bytes, i: int, hops: int = 16) -> bool:
    """Follow chunk headers through `buf` from a candidate start.

    False means "certainly not LZMA2 here". True means the chain held for `hops` chunks,
    reached an end marker, or left the buffer: worth a real check.
    """
    n = len(buf)
    j = i
    need_props = True
    first = True
    for _ in range(hops):
        if j >= n:
            return True
        c = buf[j]
        if c == 0:
            if first:
                return False
            # after a stream comes another stream, 7z's LZMA-packed index (first byte 0), or zeros
            return j + 1 >= n or buf[j + 1] <= 1 or buf[j + 1] >= 0xE0
        if c <= 2:
            if first and c != 1:
                return False
            if j + 3 > n:
                return True
            if c == 1:
                need_props = True
            j += 4 + ((buf[j + 1] << 8) | buf[j + 2])
        elif c >= 0x80:
            if first and c < 0xE0:
                return False
            if j + 6 > n:
                return True
            if c >= 0xE0:
                need_props = True
            if c >= 0xC0:
                if buf[j + 5] > 224:
                    return False
                need_props = False
                j += 7 + ((buf[j + 3] << 8) | buf[j + 4])
            elif need_props:
                return False
            else:
                j += 6 + ((buf[j + 3] << 8) | buf[j + 4])
        else:
            return False
        first = False
    return True


def _distrust_end(win: Window, s: Stream) -> None:
    """Handle an "end marker" that is really the first byte of a zeroed region.

    A zero byte is the end-of-stream marker, so a chain that runs into zeros looks as if
    it ended cleanly. LZMA chunks are caught later because they fail to decode, but stored
    chunks accept any bytes: drop trailing stored chunks whose data ends in zeros.
    """
    s.complete = False
    s.end -= 1
    while s.blocks:
        b = s.blocks[-1]
        chunks = list(iter_chunks(win, b.start, b.end))
        while chunks:
            off, size, unpacked, is_lzma = chunks[-1]
            tail = win.get(off + size - min(16, size - 3), min(16, size - 3))
            if is_lzma or tail.count(0) != len(tail):
                break
            chunks.pop()
            b.end = off
            b.unpacked -= unpacked
            b.chunks -= 1
            s.unpacked -= unpacked
            s.chunks -= 1
        if b.chunks:
            break
        s.blocks.pop()
    s.end = s.blocks[-1].end if s.blocks else s.start


def scan(f: BinaryIO, size: int, start: int = 32, limit: Optional[int] = None, resync: bool = True,
         max_garbage: int = 64 * MIB, progress: Optional[Callable[[int], None]] = None) -> ScanResult:
    """Map every decodable LZMA2 stream between `start` and `limit` without decompressing."""
    limit = size if limit is None else min(limit, size)
    win = Window(f, size)
    streams: list = []
    gaps: list = []
    gave_up = None
    pos = start
    resynced = False
    while pos < limit:
        s = walk(win, pos, limit)
        brk = pos
        if s is not None and s.complete and s.end < limit:
            after = win.get(s.end, 16)
            if after.count(0) == len(after):
                _distrust_end(win, s)
                if not s.blocks:
                    s = None
        if s is not None:
            s.resynced = resynced
            s.index = len(streams)
            streams.append(s)
            resynced = False
            pos = brk = s.end
            if progress:
                progress(pos)
            if s.complete:
                continue
            if pos >= limit:
                break
        if not resync:
            gaps.append(Gap(brk, limit))
            break
        nxt, zeros, stopped = find_sync(win, brk + 1, limit, max_garbage)
        zeros += 1 if win.get(brk, 1) == b"\0" else 0
        if nxt is None:
            if stopped < limit:
                gave_up = stopped
            gaps.append(Gap(brk, limit, zeros))
            break
        gaps.append(Gap(brk, nxt, zeros))
        pos = nxt
        resynced = True
        if progress:
            progress(pos)
    return ScanResult(streams, gaps, gave_up)


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    """One output file of decoded data: a stream, or the part of it on one side of damage."""
    path: str
    size: int = 0
    starts_mid_file: bool = False   # data begins somewhere inside a file
    truncated: bool = False         # data stops before the stream's real end
    stream: int = 0
    part: int = 0


def _batches(win: Window, block: Block, batch_bytes: int) -> Iterator[tuple]:
    start = None
    end = 0
    unpacked = 0
    for off, size, u, _ in iter_chunks(win, block.start, block.end):
        if start is not None and off + size - start > batch_bytes:
            yield start, end, unpacked
            start, unpacked = None, 0
        if start is None:
            start = off
        end = off + size
        unpacked += u
    if start is not None:
        yield start, end, unpacked


def _run_block(f: BinaryIO, win: Window, block: Block, out, dict_size: int, skip: int, batch_bytes: int) -> tuple:
    """Decode one block, writing output past the first `skip` bytes. Returns (good_bytes, ok).

    Only whole chunks are fed per call, so a failed call never leaks bytes from a bad chunk.
    """
    dec = _new_decoder(dict_size)
    produced = 0
    for start, end, unpacked in _batches(win, block, batch_bytes):
        f.seek(start)
        data = f.read(end - start)
        try:
            o = dec.decompress(data)
        except lzma.LZMAError:
            return produced, False
        if len(o) != unpacked:
            return produced, False
        if produced + len(o) > skip:
            out.write(o[max(0, skip - produced):])
        produced += len(o)
    return produced, True


def decode_stream(f: BinaryIO, size: int, stream: Stream, path_for: Callable[[int], str],
                  dict_sizes: tuple = DEFAULT_DICT_SIZES) -> list:
    """Decode a stream into one or more files. `path_for(part)` names each output file.

    A block that fails is cut after its last good chunk; the blocks after it go to a new
    file, because the bytes in between are missing.
    """
    win = Window(f, size)
    dict_i = 0
    segments: list = []
    seg = None
    out = None

    def open_segment(mid: bool):
        nonlocal seg, out
        seg = Segment(path=path_for(len(segments)), starts_mid_file=mid, stream=stream.index, part=len(segments))
        segments.append(seg)
        out = open(seg.path, "wb")

    def close_segment(truncated: bool):
        nonlocal out
        seg.truncated = truncated
        seg.size = out.tell()
        out.close()
        out = None

    open_segment(stream.resynced)
    for bi, block in enumerate(stream.blocks):
        if out is None:
            open_segment(True)
        good, ok = _run_block(f, win, block, out, dict_sizes[dict_i], 0, MIB)
        if not ok and dict_i + 1 < len(dict_sizes):
            try:    # maybe the archive used a bigger dictionary than we assumed
                g2, ok2 = _run_block(f, win, block, out, dict_sizes[dict_i + 1], good, MIB)
            except MemoryError:
                g2, ok2 = good, False
            if g2 > good:
                dict_i += 1
                good, ok = g2, ok2
        if not ok:      # go chunk by chunk to keep everything before the bad chunk
            g3, _ = _run_block(f, win, block, out, dict_sizes[dict_i], good, 1)
            good = max(good, g3)
            close_segment(True)
    if out is not None:
        close_segment(not stream.complete)
    return [s for s in segments if s.size > 0]
