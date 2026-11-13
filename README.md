# salvage7z

Recover data from a damaged `.7z` archive that 7-Zip refuses to open.

If 7-Zip says **"Cannot open the file as [7z] archive"** or **"Headers Error"**, the
archive has usually lost its *end header*: the index that 7-Zip writes last, holding
every file name and size. An interrupted copy or download, a full disk or a bad sector
is enough. The compressed data in front of it is often still fine, but no ordinary tool
will touch it without the index.

`salvage7z` reads that data anyway:

1. **Finds the compressed data without the index.** LZMA2 data is a chain of small
   chunks that each state their own size, so the chain can be followed from the start
   of the file. After a damaged spot it searches for the next point where decoding can
   restart, so one bad region does not cost the rest of the archive.
2. **Unpacks it.** Each solid block comes out as one long run of bytes: all its files
   back to back, with no names.
3. **Cuts it back into files.** It recognises file formats and works out from each
   file's own structure where it ends, so files come out whole and openable instead of
   as one multi-gigabyte blob.

It is pure Python, standard library only, and never writes to the damaged archive.

## What you get back, and what you do not

| | |
|---|---|
| File contents | Yes, for every part of the archive that survived |
| File names, folders, dates | **No.** They were only in the lost index. Files are numbered in archive order. A few formats carry their own "last saved" date, which is restored |
| Checksums | No. The CRCs were in the index too. Files are validated by their format instead (see [Limitations](#limitations)) |

To get names back for files you still have copies of somewhere else, see
[`match`](#match-get-names-back-from-copies-you-still-have).

**It cannot help when:**

- the archive is **encrypted** (the data is unreadable without the key and the index);
- the data was packed with something other than **LZMA2** (LZMA, PPMd, BZip2, Deflate,
  Copy). LZMA2 has been 7-Zip's default for `.7z` for many years, so most archives qualify;
- the bytes themselves are gone. If only the first 7 GB of a 30 GB file was ever
  written, only what is in those 7 GB can come back.

If the end header is intact and only part of the data is damaged, try `7z x` first: it
keeps names for every file outside the damaged block. Use `salvage7z` for what it
could not extract.

## Install

Python 3.9 or newer. No dependencies.

```bash
git clone https://github.com/kartikddoshi/salvage7z.git
cd salvage7z
pip install .
```

Or in one step: `pip install git+https://github.com/kartikddoshi/salvage7z.git`

Or run it straight from a checkout: `python -m salvage7z ...` with `src` on `PYTHONPATH`.

## Use

### `info`: is anything recoverable?

Reads only. Run this first.

```
$ salvage7z info backup.7z
file size        : 30,311,913,509 bytes
start header     : ok (format 0.4)
expected size    : 30,311,913,509 bytes
end header       : the place where it should be is all zeros. File names and folders are lost
LZMA2 streams    : 14 found, 7,090,504,946 bytes packed -> 26,968,693,608 bytes unpacked
   #0    offset              32  packed    342,414,626  unpacked   2,013,935,470  blocks   31
   ...
   #13   offset   6,576,445,975  packed    514,059,003  unpacked   1,272,431,809  blocks   19 INCOMPLETE
unreadable region: 7,090,504,978 .. 30,311,913,469  (23,221,408,491 bytes, zeros)
```

That is a real case: a 30 GB archive of which only the first 7 GB had ever been
written. `recover` got 195 files (27 GB) out of it.

### `recover`: unpack and split into files

```bash
salvage7z recover backup.7z D:\recovered
```

Writes:

```
D:\recovered\
  files\           s0000-0001.3dm, s0000-0002.jpg, ...   (s<stream>-<position in stream>)
  manifest.csv     one row per file: type, status, size, date, notes, SHA-256
  manifest.json    the same, for scripts
  scan.json        where every stream and damaged region is in the archive
```

Each file has a status:

- `complete`: the format confirms where the file ends.
- `partial`: cut off by damage, or its end had to be guessed. The note says which.
- `unidentified`: bytes no format module recognised, kept as `.bin` (or `.txt` if they
  are text). This can be one file or several of unknown types.

Useful options:

| Option | |
|---|---|
| `-j N` | unpack N streams in parallel (default: up to 4) |
| `--previews` | also save thumbnails embedded in recovered files (Rhino `.3dm`) into `previews\` |
| `--keep-streams` | keep the raw unpacked streams in `streams\` |
| `--no-split` | stop after unpacking; write only the raw streams |
| `--no-resync` | stop at the first damaged spot |
| `--max-gap MIB` | how much unrecognisable data to search through before giving up (default 64) |

**Disk space:** you need room for the unpacked data plus the largest single stream
(`info` shows both). The tool checks before it starts.

### `split`: cut up a raw stream you already have

If you followed [7-Zip's own recovery guide](https://www.7-zip.org/recover.html) you
ended up with one large raw file. This cuts it into files:

```bash
salvage7z split D:\recovered raw_stream.bin
```

### `match`: get names back from copies you still have

Recovered files have no names. If older copies of some of them still exist in other
folders, this finds byte-identical ones (size, then SHA-256) and tells you their names:

```bash
salvage7z match D:\recovered E:\old-backups F:\projects --rename
```

`--rename` renames matched files to `<id>__<original name>`. Files with no match are
the ones that exist only in the archive.

## File types it can separate

| Format | How the end is found |
|---|---|
| Rhino `.3dm` | chunk chain; the final chunk states the file length (self-checking) |
| STL (binary and text) | triangle count in the header / `endsolid` |
| JPEG | marker segments up to the end-of-image marker |
| PNG | chunks up to `IEND` |
| GIF | blocks up to the trailer |
| BMP, WAV, AVI, WebP, CorelDRAW | size stated in the header |
| ZIP, DOCX, XLSX, PPTX, ODF, JAR | end-of-central-directory record |
| Legacy Office (`.doc`, `.xls`, `.ppt`, `.msg`) | sector allocation table |
| PDF | `%%EOF` followed by another recognisable file |
| Nested `.7z` | its own start header |

Anything else is kept as an `unidentified` piece, so no bytes are thrown away.
Adding a format is a small, self-contained job: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Limitations

- **No checksums.** A file marked `complete` has a consistent structure, which is
  strong evidence but not proof that every byte is right. Compressed chunks do verify
  themselves when decoded; *stored* chunks (data 7-Zip could not compress, such as
  JPEGs or video) do not, so damage inside them can pass unnoticed.
- **Names, folders, timestamps and empty files are not recoverable.**
- **Encrypted archives and non-LZMA2 methods are not supported.** Archives that used
  a BCJ filter for executables will unpack, but the executables come out filtered and
  unusable.
- **Unknown file types are not separated** from each other.
- **ZIP64 and multi-volume archives** (`.7z.001`) are not handled yet. Joining the
  volumes into one file first should work for the latter.
- **Speed.** Unpacking runs at liblzma speed. Searching *non-zero* garbage for the
  next good data is slow (a few MiB/s); zeroed regions are skipped at disk speed.

## How it works

[docs/how-it-works.md](docs/how-it-works.md) explains the 7z container, the LZMA2 chunk
format, why decoding can restart after damage, and how the splitter decides where
files end.

## Contributing

Bug reports, new file formats and support for more compression methods are welcome.
See [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

MIT. See [LICENSE](LICENSE).

salvage7z is an independent project and is not affiliated with 7-Zip.
