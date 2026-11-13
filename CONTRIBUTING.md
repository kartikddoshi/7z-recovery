# Contributing

Thanks for helping. People arrive here because they have lost something, so the bar
for changes is simple: recover more, and never make things worse.

## Ground rules

- **Never write to the source archive.** Open it read-only. All output goes to the
  output folder.
- **Never drop bytes silently.** If something cannot be identified, it is still written
  out and listed in the manifest.
- **Say what is uncertain.** A file is `complete` only when its format confirms its
  end. Guesses are `partial` with a note.
- **Standard library only.** The tool has to work on whatever machine the damaged
  archive is on, without installing anything else.
- **No real files in the repository.** Tests build their samples in code
  (`tests/samples.py`). Do not commit your own recovered data, or files you do not hold
  the rights to, as fixtures.

## Set up

```bash
git clone <your fork>
cd salvage7z
python -m unittest discover -s tests -v
```

That is all. The tests add `src` to the path themselves. One test builds an archive
with the real 7-Zip command line (`7z`, `7zz` or `7za`) and is skipped if none is
installed.

## Layout

```
src/salvage7z/
  container.py   the 32-byte start header and the diagnosis
  lzma2.py       chunk chain scanning, resync after damage, decoding
  splitter.py    cutting a decoded block into files; writing files and manifest
  pipeline.py    the steps behind each command
  cli.py         argument parsing only
  formats/       one module per file format
tests/
  samples.py     generated sample files and archives
docs/how-it-works.md
```

## Adding a file format

This is the most useful kind of contribution: every format added means fewer
`unidentified` blobs for someone.

1. Create `src/salvage7z/formats/<name>.py`:

   ```python
   from .base import Found

   EXT = "xyz"
   SCAN_PATTERNS = (rb"XYZ\x01\x00",)      # bytes regexes; () if the signature is under ~4 bytes

   def probe(buf, pos, end):
       """Return Found(...) if a file of this format starts at pos, else None."""
       if buf[pos:pos + 5] != b"XYZ\x01\x00":
           return None
       length = ...                         # from the format's own structure
       if pos + length > end:
           return None
       return Found("XYZ document", EXT, length)
   ```

2. Add the module to `MODULES` in `formats/__init__.py`.
3. Add a generator to `tests/samples.py` and to its `ALL` table. The existing tests
   then check exact length, splitting and rejection of junk for your format.

What makes a good `probe`:

- **The length must come from the file's structure**, not from "wherever the next
  signature is". If the format truly has no way to tell, return `complete=False` with a
  note (see `pdf.py`).
- **Reject early and cheaply.** `probe` is called at many positions that are not your
  format.
- **Never read at or beyond `end`.** `buf` may be a `bytes` object or an `mmap` of a
  multi-gigabyte file: slice small ranges, use `struct.unpack_from` and `buf.find`.
- If a strong signature matches but the file runs past `end`, return what is there with
  `complete=False`: that is the file the damage cut.
- `struct.error`, `IndexError` and `ValueError` are caught by the registry and count as
  "not mine", so there is no need to guard every read.

Optional hooks (`weak_probe`, `describe`, `sidecars`) are described at the top of
`formats/__init__.py`.

## Ideas that need an owner

- **LZMA (version 1) streams.** Older archives use LZMA, which has no chunk headers.
  The properties and dictionary size would have to be guessed and stream ends detected.
- **BCJ / BCJ2 / Delta filters**, so executables come out usable.
- **Recovering a partly damaged index.** The end header is sometimes present but
  fails its CRC; reading what is readable would bring names back.
- **More formats:** MP4/MOV, TIFF and camera raw, MP3/FLAC, SQLite, PSD, DWG,
  ZIP64, 3dm version 1.
- **Multi-volume archives** (`.7z.001`, `.7z.002`, ...) without joining them first.
- **Faster resync search** on non-zero garbage.
- **An HTML report** with thumbnails, for browsing thousands of unnamed files.

Open an issue before starting anything large so effort is not duplicated.

## Pull requests

- One change per pull request, with a test that fails without it.
- Run the whole suite before sending.
- If behaviour or output changes, update `README.md` and `docs/how-it-works.md`.

## Reporting a failed recovery

The most helpful report contains the output of `salvage7z info <archive>` and, if you
can share them, the first few kilobytes of the archive and of an `unidentified` piece.
Check that what you attach holds nothing private first.
