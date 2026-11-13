import struct
import unittest

import samples
from salvage7z import formats
from salvage7z.splitter import split_buffer

JUNK = b"\x07junk-after-the-file\x07" * 5


class ProbeTests(unittest.TestCase):
    def test_every_format_reports_its_exact_length(self):
        for name, make in samples.ALL.items():
            with self.subTest(format=name):
                data = make()
                buf = data + JUNK
                found = formats.probe(buf, 0, len(buf))
                self.assertIsNotNone(found, name)
                self.assertEqual(found.length, len(data))
                # A PDF has no length field: its end is only certain when a known file follows.
                self.assertEqual(found.complete, name != "pdf")

    def test_pdf_end_is_certain_before_a_known_file_or_at_the_end(self):
        data = samples.pdf()
        for buf in (data, data + samples.png()):
            found = formats.probe(buf, 0, len(buf))
            self.assertEqual((found.length, found.complete), (len(data), True))

    def test_pdf_with_incremental_update_keeps_both_sections(self):
        data = samples.pdf() + b"2 0 obj\n<< >>\nendobj\ntrailer\n<< >>\n%%EOF\n"
        buf = data + samples.jpeg()
        self.assertEqual(formats.probe(buf, 0, len(buf)).length, len(data))

    def test_extension_is_refined_from_content(self):
        for name in ("docx", "doc", "wav"):
            with self.subTest(format=name):
                data = samples.ALL[name]()
                self.assertEqual(formats.probe(data, 0, len(data)).ext, name)

    def test_probe_at_an_offset(self):
        data = samples.png()
        buf = JUNK + data + JUNK
        found = formats.probe(buf, len(JUNK), len(buf))
        self.assertEqual(found.length, len(data))

    def test_junk_is_not_recognised(self):
        self.assertIsNone(formats.probe(JUNK * 10, 0, len(JUNK) * 10))

    def test_ascii_stl(self):
        data = samples.stl_ascii()
        found = formats.probe(data + JUNK, 0, len(data) + len(JUNK))
        self.assertEqual(found.length, len(data))

    def test_nested_7z_with_bad_header_crc_is_rejected(self):
        data = bytearray(samples.seven_zip())
        data[20] ^= 0xFF
        self.assertIsNone(formats.probe(bytes(data), 0, len(data)))


class RhinoTests(unittest.TestCase):
    def test_metadata(self):
        for version in (4, 50, 70):
            with self.subTest(version=version):
                data = samples.rhino_3dm(version)
                found = formats.probe(data, 0, len(data))
                formats.describe(data, 0, found)
                self.assertEqual(found.info["version"], version)
                self.assertEqual(found.info["created_by"], "alice")
                self.assertEqual(found.info["edited_by"], "bob")
                self.assertEqual(found.info["revisions"], 7)
                self.assertEqual(found.info["notes"], "ring, size 12")
                self.assertEqual(found.info["mtime"], 1614845700)      # 2021-03-04 08:15 UTC

    def test_cut_off_file_is_partial(self):
        data = samples.rhino_3dm(4)[:-200]
        found = formats.probe(data, 0, len(data))
        self.assertFalse(found.complete)
        self.assertEqual(found.length, len(data))

    def test_unfinished_save_takes_the_bytes_after_it(self):
        unfinished = samples.rhino_3dm(4, finished=False)
        tail = b"\x71\x00\x00\x82half-written object" * 3
        nxt = samples.png()
        buf = unfinished + tail + nxt
        items = split_buffer(buf, len(buf))
        self.assertEqual([i.found.ext for i in items], ["3dm", "png"])
        self.assertEqual(items[0].found.length, len(unfinished) + len(tail))
        self.assertFalse(items[0].found.complete)

    def test_wrong_stated_length_is_flagged(self):
        data = bytearray(samples.rhino_3dm(4))
        data[-4:] = struct.pack("<I", 12345)
        found = formats.probe(bytes(data), 0, len(data))
        self.assertFalse(found.complete)


class StlTests(unittest.TestCase):
    def test_cut_off_stl_gets_a_corrected_triangle_count(self):
        data = samples.stl(40)[:84 + 50 * 13 + 7]
        found = formats.probe(data, 0, len(data))
        self.assertFalse(found.complete)
        self.assertEqual(found.length, 84 + 50 * 13)
        self.assertEqual(found.patches, ((80, struct.pack("<I", 13)),))

    def test_unknown_header_needs_a_recognisable_neighbour(self):
        mesh = samples.stl(40, header=b"exported by some other tool")
        self.assertIsNone(formats.probe(mesh, 0, len(mesh)))
        buf = mesh + samples.png()
        self.assertEqual([i.found.ext for i in split_buffer(buf, len(buf))], ["stl", "png"])

    def test_random_bytes_are_not_a_mesh(self):
        data = samples.noise(3, 84 + 50 * 20)
        self.assertIsNone(formats.weak_probe(data, 0, len(data)))


class SplitTests(unittest.TestCase):
    def test_back_to_back_files_are_separated_exactly(self):
        names = list(samples.ALL)
        parts = [samples.ALL[n]() for n in names]
        buf = b"".join(parts)
        items = split_buffer(buf, len(buf))
        self.assertEqual([i.found.length for i in items], [len(p) for p in parts])
        self.assertEqual([i.offset for i in items][1], len(parts[0]))
        self.assertTrue(all(i.identified and i.found.complete for i in items))

    def test_unknown_bytes_between_files_are_kept(self):
        blob = samples.noise(9, 5000)
        buf = samples.jpeg() + blob + samples.png() + b"plain notes\r\nline two\r\n" + samples.rhino_3dm()
        items = split_buffer(buf, len(buf))
        self.assertEqual([i.found.ext for i in items], ["jpg", "bin", "png", "txt", "3dm"])
        self.assertEqual(items[1].found.length, len(blob))
        self.assertFalse(items[1].identified)
        self.assertEqual(sum(i.found.length for i in items), len(buf))

    def test_fragment_notes(self):
        buf = b"\x00\x01tail of something" + samples.png() + b"\x00\x02head of something"
        items = split_buffer(buf, len(buf), starts_mid_file=True, truncated=True)
        self.assertIn("beginning was in the damaged part", items[0].found.note)
        self.assertIn("continues into the damaged part", items[-1].found.note)


if __name__ == "__main__":
    unittest.main()
