import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import samples
from salvage7z import cli, container, lzma2, pipeline


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="salvage7z-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name: str, data: bytes) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def unpack(self, path: str, **kw):
        diag, result = pipeline.inspect(path, **kw)
        out = os.path.join(self.tmp, "streams")
        segs = pipeline.unpack(path, result.streams, out)
        data = []
        for s in segs:
            with open(s.path, "rb") as f:
                data.append(f.read())
        return diag, result, segs, data


class ScanAndUnpackTests(Base):
    def test_intact_streams(self):
        parts = [samples.texty(1, 3_000_000), samples.noise(2, 200_000), samples.texty(3, 50_000)]
        path = self.write("a.7z", samples.archive([samples.raw_lzma2(p) for p in parts]))
        diag, result, segs, data = self.unpack(path)
        self.assertEqual(diag.end_header_state, "ok")
        self.assertEqual([s.complete for s in result.streams], [True, True, True])
        self.assertEqual([s.unpacked for s in result.streams], [len(p) for p in parts])
        self.assertEqual(result.gaps, [])
        self.assertEqual(data, parts)
        self.assertFalse(any(s.truncated or s.starts_mid_file for s in segs))

    def test_tail_overwritten_with_zeros(self):
        parts = [samples.texty(1, 600_000), samples.texty(2, 6_000_000)]
        blob = bytearray(samples.archive([samples.raw_lzma2(p) for p in parts]))
        cut = len(blob) * 6 // 10
        blob[cut:] = bytes(len(blob) - cut)
        path = self.write("a.7z", bytes(blob))
        diag, result, segs, data = self.unpack(path)
        self.assertEqual(diag.end_header_state, "zeros")
        self.assertEqual(data[0], parts[0])
        self.assertTrue(segs[-1].truncated)
        self.assertGreater(len(data[1]), 1_000_000)
        self.assertEqual(data[1], parts[1][:len(data[1])])      # nothing wrong, only shorter
        self.assertEqual(result.gaps[-1].end, diag.packed_end)
        self.assertEqual(result.gaps[-1].kind, "zeros")

    def test_truncated_file(self):
        part = samples.texty(5, 6_000_000)
        blob = samples.archive([samples.raw_lzma2(part)])
        path = self.write("a.7z", blob[:len(blob) // 2])
        diag, result, segs, data = self.unpack(path)
        self.assertEqual(diag.end_header_state, "missing")
        self.assertTrue(segs[0].truncated)
        self.assertGreater(len(data[0]), 500_000)
        self.assertEqual(data[0], part[:len(data[0])])

    def test_damage_inside_a_chunk_costs_only_that_block(self):
        parts = [samples.texty(i, 400_000) for i in range(6)]
        stream, offsets = samples.blocked_stream(parts)
        blob = bytearray(samples.archive([stream]))
        hit = 32 + offsets[2] + 2000                         # inside block 2's packed data
        blob[hit:hit + 64] = samples.noise(99, 64)
        path = self.write("a.7z", bytes(blob))
        _, result, segs, data = self.unpack(path)
        self.assertEqual(len(result.streams), 1)             # chunk headers are intact, so the chain is too
        self.assertEqual(len(segs), 2)
        self.assertTrue(segs[0].truncated)
        self.assertTrue(segs[1].starts_mid_file)
        whole = parts[0] + parts[1] + parts[2]
        self.assertGreaterEqual(len(data[0]), len(parts[0]) + len(parts[1]))
        self.assertEqual(data[0], whole[:len(data[0])])
        self.assertEqual(data[1], parts[3] + parts[4] + parts[5])

    def test_resync_after_a_destroyed_region(self):
        parts = [samples.texty(i, 400_000) for i in range(6)]
        stream, offsets = samples.blocked_stream(parts)
        blob = bytearray(samples.archive([stream]))
        start = 32 + offsets[2] - 3000                       # wipes the end of block 1 and the start of block 2
        blob[start:start + 9000] = samples.noise(7, 9000)
        path = self.write("a.7z", bytes(blob))
        _, result, segs, data = self.unpack(path)
        self.assertEqual(len(result.streams), 2)
        self.assertFalse(result.streams[0].complete)
        self.assertTrue(result.streams[1].resynced)
        self.assertEqual(result.streams[1].start, 32 + offsets[3])
        self.assertEqual(data[0], (parts[0] + parts[1])[:len(data[0])])
        self.assertGreaterEqual(len(data[0]), len(parts[0]))
        self.assertEqual(data[-1], parts[3] + parts[4] + parts[5])

    def test_zeroed_hole_in_the_middle(self):
        parts = [samples.noise(i, 300_000) for i in range(5)]     # stored chunks this time
        stream, offsets = samples.blocked_stream(parts)
        blob = bytearray(samples.archive([stream]))
        start = 32 + offsets[1] + 100
        blob[start:32 + offsets[2] + 50] = bytes(32 + offsets[2] + 50 - start)
        path = self.write("a.7z", bytes(blob))
        _, result, segs, data = self.unpack(path)
        self.assertEqual(data[0], parts[0])
        self.assertEqual(data[-1], parts[3] + parts[4])
        self.assertTrue(result.streams[-1].resynced)

    def test_no_resync_stops_at_first_damage(self):
        parts = [samples.texty(i, 400_000) for i in range(4)]
        stream, offsets = samples.blocked_stream(parts)
        blob = bytearray(samples.archive([stream]))
        start = 32 + offsets[1] - 1000
        blob[start:start + 5000] = samples.noise(7, 5000)
        path = self.write("a.7z", bytes(blob))
        _, result = pipeline.inspect(path, resync=False)
        self.assertEqual(len(result.streams), 1)

    def test_data_that_is_not_lzma2(self):
        path = self.write("a.7z", samples.archive([samples.noise(4, 6_000_000)]))
        _, result = pipeline.inspect(path, max_garbage=1 << 20)
        self.assertEqual(result.streams, [])
        self.assertIsNotNone(result.gave_up_at)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["info", path, "--max-gap", "1"]), 2)

    def test_damaged_start_header(self):
        blob = bytearray(samples.archive([samples.raw_lzma2(samples.texty(1, 100_000))]))
        blob[14] ^= 0xFF
        path = self.write("a.7z", bytes(blob))
        diag, result, _, data = self.unpack(path)
        self.assertFalse(diag.start_header_ok)
        self.assertEqual(data[0], samples.texty(1, 100_000))


class EndToEndTests(Base):
    def make_files(self):
        files = {name: make() for name, make in samples.ALL.items() if name != "bmp"}
        files["big.3dm"] = samples.rhino_3dm(4, payload=samples.texty(8, 3_000_000))
        files["mesh.stl"] = samples.stl(20_000)
        return files

    def run_cli(self, *argv) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue()

    def test_recover_solid_archive_with_lost_end_header(self):
        files = self.make_files()
        solid = b"".join(files.values())
        blob = bytearray(samples.archive([samples.raw_lzma2(solid)]))
        blob[-40:] = bytes(40)
        path = self.write("lost-index.7z", bytes(blob))
        out = os.path.join(self.tmp, "out")
        self.run_cli("recover", path, out, "-j", "1")
        with open(os.path.join(out, "manifest.json"), encoding="utf-8") as f:
            records = json.load(f)
        self.assertEqual(len(records), len(files))
        self.assertEqual({r["sha256"] for r in records}, {sha(v) for v in files.values()})
        self.assertTrue(all(r["status"] == "complete" for r in records))
        for r in records:
            with open(os.path.join(out, "files", r["file"]), "rb") as f:
                self.assertEqual(sha(f.read()), r["sha256"])
        self.assertFalse(os.path.exists(os.path.join(out, "streams")))
        self.assertTrue(os.path.exists(os.path.join(out, "manifest.csv")))
        big = next(r for r in records if r["bytes"] == len(files["big.3dm"]))
        self.assertEqual(big["modified"], "2021-03-04 08:15:00")
        self.assertEqual(big["info"]["edited_by"], "bob")

    def test_partial_last_file_is_written_and_flagged(self):
        files = self.make_files()
        solid = b"".join(files.values()) + samples.stl(60_000)
        blob = samples.archive([samples.raw_lzma2(solid)])
        path = self.write("cut.7z", blob[:len(blob) - 20_000])
        out = os.path.join(self.tmp, "out")
        self.run_cli("recover", path, out, "-j", "1", "--keep-streams")
        with open(os.path.join(out, "manifest.json"), encoding="utf-8") as f:
            records = json.load(f)
        self.assertEqual({r["sha256"] for r in records[:-1]}, {sha(v) for v in files.values()})
        last = records[-1]
        self.assertEqual((last["kind"], last["status"]), ("STL mesh", "partial"))
        with open(os.path.join(out, "files", last["file"]), "rb") as f:
            data = f.read()
        self.assertEqual(len(data), 84 + 50 * int.from_bytes(data[80:84], "little"))
        self.assertTrue(os.path.isdir(os.path.join(out, "streams")))

    def test_split_and_match_commands(self):
        files = self.make_files()
        raw = self.write("raw.bin", b"".join(files.values()))
        out = os.path.join(self.tmp, "out")
        self.run_cli("split", out, raw)
        originals = os.path.join(self.tmp, "originals", "nested")
        os.makedirs(originals)
        for name in ("big.3dm", "mesh.stl"):
            with open(os.path.join(originals, name if "." in name else name + ".dat"), "wb") as f:
                f.write(files[name])
        text = self.run_cli("match", out, os.path.join(self.tmp, "originals"), "--rename")
        self.assertIn("big.3dm", text)
        names = os.listdir(os.path.join(out, "files"))
        self.assertTrue(any(n.endswith("__big.3dm") for n in names))
        self.assertTrue(any(n.endswith("__mesh.stl") for n in names))
        with open(os.path.join(out, "matches.csv"), encoding="utf-8-sig") as f:
            self.assertEqual(len(f.read().strip().splitlines()), 3)

    def test_not_enough_space_is_reported(self):
        path = self.write("a.7z", samples.archive([samples.raw_lzma2(samples.texty(1, 100_000))]))
        real = shutil.disk_usage
        shutil.disk_usage = lambda p: type("U", (), {"free": 10})()
        try:
            with self.assertRaises(pipeline.SalvageError):
                pipeline.recover(path, os.path.join(self.tmp, "out"))
        finally:
            shutil.disk_usage = real


def find_7z():
    for name in ("7z", "7zz", "7za", "7zr"):
        p = shutil.which(name)
        if p:
            return p
    for p in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if os.path.exists(p):
            return p
    return None


@unittest.skipUnless(find_7z(), "7-Zip command line not installed")
class Real7zTests(Base):
    """The same recovery against an archive written by 7-Zip itself."""

    def test_archive_from_7zip_with_wiped_index(self):
        files = {
            "model_a.3dm": samples.rhino_3dm(4, payload=samples.texty(1, 2_500_000)),
            "model_b.3dm": samples.rhino_3dm(50, payload=samples.texty(2, 900_000)),
            "mesh.stl": samples.stl(30_000),
            "photo.jpg": samples.jpeg(),
            "pic.png": samples.png(),
            "report.pdf": samples.pdf(),
            "sheet.docx": samples.docx(),
        }
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        for name, data in files.items():
            with open(os.path.join(src, name), "wb") as f:
                f.write(data)
        path = os.path.join(self.tmp, "real.7z")
        subprocess.run([find_7z(), "a", "-t7z", "-m0=lzma2", "-mx=3", "-ms=on", "-mmt=2", path, "*"],
                       cwd=src, check=True, stdout=subprocess.DEVNULL)
        with open(path, "rb") as f:
            diag = container.diagnose(f)
        self.assertEqual(diag.end_header_state, "ok")
        with open(path, "r+b") as f:                         # lose the index, as an interrupted copy would
            f.seek(diag.end_header_offset - 16)
            f.write(bytes(diag.file_size - diag.end_header_offset + 16))
        out = os.path.join(self.tmp, "out")
        summary = pipeline.recover(path, out, jobs=2)
        self.assertEqual(summary["files"], len(files))
        with open(os.path.join(out, "manifest.json"), encoding="utf-8") as f:
            records = json.load(f)
        self.assertEqual({r["sha256"] for r in records}, {sha(v) for v in files.values()})


class ChunkHeaderTests(unittest.TestCase):
    def test_chunk_info(self):
        self.assertEqual(lzma2.chunk_info(b"\x01\x00\x09"), (13, 10, False, False, True))
        self.assertEqual(lzma2.chunk_info(b"\x02\xff\xff"), (3 + 65536, 65536, False, False, False))
        self.assertEqual(lzma2.chunk_info(b"\xe0\x00\x00\x00\x04\x5d"), (11, 1, True, True, True))
        self.assertEqual(lzma2.chunk_info(b"\x9f\xff\xff\x00\x00"), (6, 1 << 21, True, False, False))
        self.assertIsNone(lzma2.chunk_info(b"\x03\x00\x00"))
        self.assertIsNone(lzma2.chunk_info(b"\xe0\x00\x00\x00\x04\xff"))     # impossible properties byte
        self.assertIsNone(lzma2.chunk_info(b"\x80\x00"))


if __name__ == "__main__":
    unittest.main()
