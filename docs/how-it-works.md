# How salvage7z works

## 1. What a .7z file looks like

```
+----------------+------------------------------------------+-------------+
| start header   | packed streams                           | end header  |
| 32 bytes       | stream 0 | stream 1 | ... | packed index | (small)     |
+----------------+------------------------------------------+-------------+
```

- The **start header** holds the magic bytes, and the offset, size and CRC of the end
  header.
- The **packed streams** are the compressed data. In a *solid* archive (the default),
  many files are concatenated and compressed together as one stream, called a
  *folder* or *solid block*. Large archives have several.
- The **end header** is the index: file names, sizes, timestamps, CRCs, which
  compression method each stream uses, and where each file sits inside its solid block.
  It is usually compressed itself and stored as the last packed stream, with a tiny
  pointer after it.

7-Zip writes the end header last. Anything that stops the file from being completed
or copied in full (interrupted transfer, full disk, pre-allocated download that never
finished) takes the index with it. Without it 7-Zip reports *"Cannot open the file as
archive"* even if 99% of the data is present.

The official guide, <https://www.7-zip.org/recover.html>, describes a manual way out:
build a replacement header in a hex editor, extract one raw stream, and then somehow
parse that stream. salvage7z automates both halves and adds recovery past damaged
regions.

## 2. Finding the data without the index

The index would normally say where each stream starts and how to decode it. For LZMA2
that information can be rebuilt from the data, because an LZMA2 stream is a chain of
self-describing chunks:

| Control byte | Meaning | Header |
|---|---|---|
| `0x00` | end of stream | 1 byte |
| `0x01` | stored chunk, dictionary reset | control, size-1 (2 bytes) |
| `0x02` | stored chunk | control, size-1 (2 bytes) |
| `0x80`-`0xFF` | LZMA chunk | control, unpacked size-1 (2 bytes, 5 more bits in the control byte), packed size-1 (2 bytes), properties byte if control >= `0xC0` |

Since every chunk states its packed size, the scanner can hop from header to header
through gigabytes in seconds, reading a few bytes per chunk. The first stream starts at
offset 32. A stream ends at a `0x00` byte, and the next one begins right after it.

This gives the position and unpacked size of every stream with no decompression.

## 3. Getting past damage

In an LZMA chunk the top bits of the control byte say what is reset before it:

- `0xE0`-`0xFF`: dictionary, state and properties are all reset. (`0x01` does the same
  for stored chunks.)

A chunk with a dictionary reset depends on nothing before it. 7-Zip's multi-threaded
LZMA2 encoder compresses its input in independent blocks (four times the dictionary
size by default) and starts each with such a chunk. So a 2 GB solid block made with a
16 MB dictionary contains about 32 points where decoding can restart.

When the chain breaks, salvage7z searches forward for the next reset chunk:

1. Skip runs of zeros at disk speed.
2. For each byte that could be a reset control byte, follow the would-be chain through
   16 chunk headers. Random data fails this within a few hops.
3. Test-decode the first chunks with liblzma. An LZMA chunk must produce exactly its
   stated unpacked size from exactly its stated packed size, so a false match is
   practically impossible. A run of stored chunks proves less, so at least four are
   required.

Data found this way starts in the middle of some file, and the bytes between the break
and the restart point are lost. The output says so: such a stream is marked "starts
after damage", and the first piece cut from it is labelled as the tail of a file.

## 4. Unpacking without settings

The index also held the coder settings. For LZMA2 the only setting is the dictionary
size, and a decoder with a dictionary at least as large as the encoder's works. The
tool starts with 256 MiB and retries a failing block with 1536 MiB before concluding
it is damaged.

Each block is decoded on its own, and only whole chunks are handed to the decoder at a
time. If a block fails, it is decoded again chunk by chunk so that everything up to
the bad chunk is kept and nothing from the bad chunk leaks into the output. The blocks
after it go to a new output file, because the bytes in between are missing.

### Zeroed regions

A zero byte is the end-of-stream marker, so a chain that runs into a zeroed region
looks as if it ended cleanly. An LZMA chunk that is partly zeros fails to decode and
is dropped. A stored chunk accepts any bytes, so when an "end marker" is followed by
more zeros, trailing stored chunks whose data ends in zeros are dropped too and the
stream is marked incomplete.

## 5. Cutting a solid block into files

A decoded solid block is file after file with nothing in between. The index recorded
the lengths; without it, the only source left is the files themselves. Most formats
say how long they are, one way or another:

- a length field (`.3dm`, BMP, RIFF, nested 7z),
- a count that fixes the length (binary STL: 84 + 50 x triangles),
- a terminating structure (JPEG end marker, PNG `IEND`, ZIP end-of-central-directory),
- an allocation table (OLE2 documents).

The splitter starts at offset 0 and asks each format module, "is there one of yours
here, and how long is it?" It jumps that far and asks again. Because files are
contiguous, a correct answer lands exactly on the next file's first byte, which the
next probe then confirms. A chain of files that all parse cleanly end to end is strong
evidence that every boundary is right.

When no module recognises the bytes at the current position, the splitter searches
forward for the next signature that a module can *verify* (a signature alone is not
trusted) and saves the bytes in between as one `unidentified` piece.

Two special cases:

- **Formats without a signature.** Binary STL has none. Its module offers a "weak"
  probe: plausible triangle data, believed only when a recognisable file follows.
- **Files that were already broken when archived.** A program that was interrupted
  while saving can leave a file with no end marker. A format module can flag this
  (`absorb_unknown`) so the unidentified bytes that follow are kept as part of that
  file instead of being split off.

## 6. What is and is not verified

| Stage | Check |
|---|---|
| Chunk chain | every header must be valid and lead to another valid header |
| LZMA chunks | liblzma verifies packed and unpacked sizes; corruption is detected |
| Stored chunks | none possible |
| File boundaries | format structure; adjacent files confirm each other |
| File contents | **not** verified: the CRCs were in the lost index |

`manifest.csv` records a SHA-256 for every recovered file so you can compare against
other copies. The `match` command does this for you.
