"""Change 11d: the JPEG structure check and rebuild (contract 11d §4, security notes §4b-1 … §4b-10).

These call photos.rebuild() directly; tests/test_photo_http.py covers the endpoints.
"""

import random
import struct
import unittest

from tests import helpers  # noqa: F401  (puts app/ on sys.path)
from tests import jpeg
from tests.jpeg import Segment, first, join, sof_payload, sos_payload

import photos  # noqa: E402


def segments_of(data):
    """(marker, payload) for every segment of a rebuilt file, plus the scans."""
    return [(s.marker, s.payload) for s in jpeg.split(data)]


class Assertions:
    def assert_rejected(self, data, msg=None):
        with self.assertRaises(photos.InvalidImage, msg=msg) as ctx:
            photos.rebuild(data)
        self.assertEqual(str(ctx.exception), "invalid image", "no detail about the input")

    def assert_clean(self, out):
        """§4b-8: SOI, the fixed JFIF APP0, image segments only, EOI."""
        self.assertTrue(out.startswith(b"\xff\xd8" + jpeg.FIXED_APP0))
        self.assertTrue(out.startswith(bytes.fromhex("ffd8ffe000104a464946")))
        self.assertTrue(out.endswith(b"\xff\xd9"))
        markers = [m for m, _ in segments_of(out)]
        self.assertEqual(markers[0], 0xE0)
        self.assertEqual(markers.count(0xE0), 1)
        for marker in markers[1:]:
            self.assertIn(marker, (0xDB, 0xC4, 0xDD, 0xC0, 0xC1, 0xC2, 0xDA))
        self.assertEqual(photos.rebuild(out), out, "rebuilding the output changes nothing")


# --- Real browser output (§4b-10) ---

class RealFixtureTests(Assertions, unittest.TestCase):
    def test_real_fixtures_accepted(self):
        for name, data in jpeg.REAL_FIXTURES.items():
            with self.subTest(name=name):
                out = photos.rebuild(data)
                self.assert_clean(out)
                self.assertLessEqual(len(out), len(data))
                self.assertLessEqual(len(data), photos.MAX_BYTES)

    def test_chrome_output_shape(self):
        """What Chrome 124 sends: JFIF, an sRGB ICC profile, baseline 4:2:0. Only the ICC profile goes."""
        markers = [s.marker for s in jpeg.split(jpeg.CHROME_PHOTO)]
        self.assertEqual(markers, [0xE0, 0xE2, 0xDB, 0xDB, 0xC0, 0xC4, 0xC4, 0xC4, 0xC4, 0xDA])
        self.assertIn(b"ICC_PROFILE", jpeg.CHROME_PHOTO)
        out = photos.rebuild(jpeg.CHROME_PHOTO)
        self.assertNotIn(b"ICC_PROFILE", out)
        kept = [s for s in jpeg.split(jpeg.CHROME_PHOTO) if s.marker not in (0xE0, 0xE2)]
        self.assertEqual(out, join([Segment(0xE0, jpeg.FIXED_APP0[4:])] + kept), "image segments copied unchanged")

    def test_input_type_and_size(self):
        self.assert_rejected(bytearray(jpeg.CHROME_PHOTO))
        self.assert_rejected(memoryview(jpeg.CHROME_PHOTO))
        self.assert_rejected(jpeg.CHROME_PHOTO.decode("latin-1"))
        self.assert_rejected(None)
        self.assert_rejected(b"")
        # Over the upload limit: refused before parsing, even with a valid structure.
        big = jpeg.split(jpeg.CHROME_PHOTO)
        big.insert(0, Segment(0xFE, bytes(65533)))
        big.insert(0, Segment(0xFE, bytes(65533)))
        data = join(big)
        self.assertGreater(len(data), photos.MAX_BYTES)
        self.assert_rejected(data)


# --- Stripping (§4b-7, §4b-8) ---

class StripTests(Assertions, unittest.TestCase):
    def test_exif_gps_thumbnail_icc_and_comments_removed(self):
        segments = jpeg.chrome_segments()
        segments.insert(1, jpeg.exif_app1())
        segments.insert(2, Segment(0xFE, b"Shot on a phone at 10 Downing Street"))
        segments.insert(3, Segment(0xED, b"Photoshop 3.0\x00IPTC"))
        segments.insert(4, Segment(0xEF, b"APP15"))
        segments.insert(first(segments, 0xDA), Segment(0xFE, b"comment between tables and scan"))
        data = join(segments)
        out = photos.rebuild(data)
        self.assert_clean(out)
        for needle in (b"Exif", b"GPS", b"Downing", b"Photoshop", b"ICC_PROFILE", b"comment", b"APP15", b"MM\x00\x2a"):
            with self.subTest(needle=needle):
                self.assertIn(needle, data)
                self.assertNotIn(needle, out)
        self.assertEqual(out.count(b"\xff\xd8"), 1, "the EXIF thumbnail is gone")
        self.assertEqual(out, photos.rebuild(jpeg.CHROME_PHOTO))

    def test_jfif_header_replaced(self):
        segments = jpeg.chrome_segments()
        segments[0] = Segment(0xE0, b"JFIF\x00\x01\x02\x01\x00\x48\x00\x48\x01\x01" + bytes(3))  # with a thumbnail
        out = photos.rebuild(join(segments))
        self.assertEqual(out, photos.rebuild(jpeg.CHROME_PHOTO))

    def test_no_app0_in_input(self):
        """A file with no APPn at all is accepted: the fixed APP0 is added, 18 bytes more than was sent."""
        segments = [s for s in jpeg.chrome_segments() if s.marker != 0xE0]
        self.assertEqual(photos.rebuild(join(segments)), photos.rebuild(jpeg.CHROME_PHOTO))
        data = join([s for s in segments if s.marker != 0xE2])
        out = photos.rebuild(data)
        self.assertEqual(out, b"\xff\xd8" + photos.JFIF_APP0 + data[2:])
        self.assertEqual(len(out), len(data) + 18)

    def test_full_size_input_with_no_app0(self):
        """At exactly the cap, the output (over the cap) still re-validates: the input cap isn't re-applied."""
        segments = [s for s in jpeg.chrome_segments() if s.marker not in (0xE0, 0xE2)]
        short = photos.MAX_BYTES - len(join(segments))
        segments[-1].scan += b"\x55" * short  # longer entropy-coded data, still structurally valid
        data = join(segments)
        self.assertEqual(len(data), 131072)
        out = photos.rebuild(data)
        self.assertEqual(len(out), 131090)
        self.assertEqual(out, b"\xff\xd8" + photos.JFIF_APP0 + data[2:])
        # rebuild()'s own re-validation step; calling rebuild(out) itself would hit the input cap.
        self.assertEqual(photos._rebuild(out), out)
        self.assert_rejected(out, "131,090 bytes is over the upload cap as an input")
        self.assert_rejected(data[:-2] + b"\x55\xff\xd9", "131,073 bytes")

    def test_fill_bytes_removed(self):
        segments = jpeg.chrome_segments()
        parts = [b"\xff\xff\xff" + s.encode() for s in segments]
        data = b"\xff\xd8" + b"".join(parts) + b"\xff\xff\xff\xd9"
        out = photos.rebuild(data)
        self.assertEqual(out, photos.rebuild(jpeg.CHROME_PHOTO))

    def test_adobe_app14(self):
        segments = jpeg.chrome_segments()
        segments.insert(1, jpeg.adobe_app14(1))
        self.assertNotIn(b"Adobe", photos.rebuild(join(segments)))
        for transform in (0, 2):
            with self.subTest(transform=transform):
                segments = jpeg.chrome_segments()
                segments.insert(1, jpeg.adobe_app14(transform))
                self.assert_rejected(join(segments))
        # After the frame too.
        segments = jpeg.chrome_segments()
        segments.insert(first(segments, 0xDA), jpeg.adobe_app14(0))
        self.assert_rejected(join(segments))
        # Too short to hold a transform byte.
        segments = jpeg.chrome_segments()
        segments.insert(1, Segment(0xEE, b"Adobe\x00\x64"))
        self.assert_rejected(join(segments))
        # Another APP14 is just metadata.
        segments = jpeg.chrome_segments()
        segments.insert(1, Segment(0xEE, b"Other"))
        photos.rebuild(join(segments))

    def test_adobe_on_grayscale_is_ignored(self):
        data = jpeg.synthetic(sof=sof_payload(components=((1, 0x11, 0),)), scans=[sos_payload(((1, 0x00),))],
                              extra=[jpeg.adobe_app14(0)])
        photos.rebuild(data)


# --- Framing (§4b-3, §4b-4) ---

class FramingTests(Assertions, unittest.TestCase):
    def test_trailing_bytes(self):
        zip_tail = b"PK\x03\x04" + bytes(26) + b"payload.html" + b"PK\x05\x06" + bytes(18)
        for tail in (zip_tail, b"\x00", b"\xff", b"\xff\xd9", b"\xff\xd8\xff\xd9"):
            with self.subTest(tail=tail[:8]):
                self.assert_rejected(jpeg.CHROME_PHOTO + tail)

    def test_second_soi(self):
        self.assert_rejected(b"\xff\xd8" + jpeg.CHROME_PHOTO)
        segments = jpeg.chrome_segments()
        self.assert_rejected(join(segments[:3])[:-2] + b"\xff\xd8" + join(segments[3:])[2:])
        self.assert_rejected(jpeg.CHROME_PHOTO[:-2] + jpeg.CHROME_PHOTO)  # two files back to back

    def test_bad_start(self):
        for data in (jpeg.CHROME_PHOTO[1:], b"\x00" + jpeg.CHROME_PHOTO, b"\xff\xd9", b"\xff\xd8", b"\xff\xd8\xff",
                     b"\x89PNG\r\n\x1a\n" + bytes(40), b"GIF89a" + bytes(40)):
            with self.subTest(data=data[:8]):
                self.assert_rejected(data)

    def test_truncated(self):
        data = jpeg.CHROME_PHOTO
        for cut in list(range(0, 700)) + list(range(700, len(data), 97)) + [len(data) - 2, len(data) - 1]:
            with self.subTest(cut=cut):
                self.assert_rejected(data[:cut])

    def test_garbage_between_segments(self):
        segments = jpeg.chrome_segments()
        data = join(segments[:3])[:-2] + b"\x00" + join(segments[3:])[2:]
        self.assert_rejected(data)
        self.assert_rejected(join(segments[:3])[:-2] + b"\xff\x00" + join(segments[3:])[2:])  # stuffed byte outside a scan

    def test_no_eoi_or_no_scan(self):
        self.assert_rejected(jpeg.CHROME_PHOTO[:-2])
        segments = [s for s in jpeg.chrome_segments() if s.marker != 0xDA]
        self.assert_rejected(join(segments))
        self.assert_rejected(join([s for s in jpeg.chrome_segments() if s.marker != 0xC0]))

    def test_bad_segment_lengths(self):
        segments = jpeg.chrome_segments()
        dqt = first(segments, 0xDB)
        for length in (0, 1):
            with self.subTest(length=length):
                head = join(segments[:dqt])[:-2]
                self.assert_rejected(head + segments[dqt].encode(length) + join(segments[dqt + 1:])[2:])
        # A length running past the end of the file.
        com = Segment(0xFE, b"x" * 10)
        self.assert_rejected(b"\xff\xd8" + com.encode(len(jpeg.CHROME_PHOTO)) + jpeg.CHROME_PHOTO[2:])
        self.assert_rejected(b"\xff\xd8\xff\xfe\x00")  # length cut off
        self.assert_rejected(b"\xff\xd8\xff\xfe")

    def test_segment_limit(self):
        base = jpeg.chrome_segments()  # 10 segments with a length field
        ok = list(base)
        ok[1:1] = [Segment(0xFE, b"c")] * (photos.MAX_SEGMENTS - len(base))
        photos.rebuild(join(ok))
        too_many = list(ok)
        too_many.insert(1, Segment(0xFE, b"c"))
        self.assert_rejected(join(too_many))

    def test_rst_outside_a_scan(self):
        segments = jpeg.chrome_segments()
        self.assert_rejected(join(segments[:2])[:-2] + b"\xff\xd0" + join(segments[2:])[2:])

    def test_tem_and_reserved_markers(self):
        for marker in [0x01] + list(range(0x02, 0xC0, 7)) + [0xBF]:
            with self.subTest(marker=hex(marker)):
                segments = jpeg.chrome_segments()
                segments.insert(2, Segment(marker, b"\x00\x00"))
                self.assert_rejected(join(segments))


# --- Tables (§4b-4) ---

class TableTests(Assertions, unittest.TestCase):
    def replace(self, marker, payload, index=0):
        segments = jpeg.chrome_segments()
        position = [i for i, s in enumerate(segments) if s.marker == marker][index]
        segments[position] = Segment(marker, payload)
        return join(segments)

    def chrome_dqt(self):
        return jpeg.chrome_segments()[first(jpeg.chrome_segments(), 0xDB)].payload

    def test_dqt(self):
        dqt = self.chrome_dqt()
        self.assertEqual(len(dqt), 65)
        photos.rebuild(self.replace(0xDB, dqt))
        for payload in (b"", dqt[:64], dqt + b"\x00", bytes([0x10]) + dqt[1:],  # 16-bit precision
                        bytes([0x04]) + dqt[1:], bytes([0x21]) + dqt[1:]):
            with self.subTest(payload=payload[:2]):
                self.assert_rejected(self.replace(0xDB, payload))
        # Two tables in one segment are fine.
        both = b"".join(s.payload for s in jpeg.chrome_segments() if s.marker == 0xDB)
        segments = [s for s in jpeg.chrome_segments() if s.marker != 0xDB]
        segments.insert(2, Segment(0xDB, both))
        photos.rebuild(join(segments))

    def test_quant_table_defined_before_the_first_scan(self):
        segments = jpeg.chrome_segments()
        dqts = [s for s in segments if s.marker == 0xDB]
        segments = [s for s in segments if s.marker != 0xDB]
        segments.insert(1, dqts[0])
        self.assert_rejected(join(segments), "the chroma table (1) is never defined")
        segments.insert(len(segments), dqts[1])  # after the scan: too late
        self.assert_rejected(join(segments))

    def test_dht_class_and_id(self):
        for tc_th in (0x20, 0x04, 0x14, 0xF0):
            with self.subTest(tc_th=hex(tc_th)):
                self.assert_rejected(self.replace(0xC4, jpeg.dht_payload(tc_th >> 4, tc_th & 15, [0, 1])))

    def test_dht_counts(self):
        self.assert_rejected(self.replace(0xC4, jpeg.dht_payload(0, 0, [])), "no codes")
        self.assert_rejected(self.replace(0xC4, jpeg.dht_payload(0, 0, [0] * 15 + [255]) + b"\x00"),
                             "length doesn't match the counts")
        # 256 codes is the most a table may have; 257 (codes that would still fit) is refused.
        most = [0] * 7 + [1] + [0] * 7 + [255]
        segments = jpeg.chrome_segments()
        segments.insert(first(segments, 0xC4), Segment(0xC4, jpeg.dht_payload(0, 3, most, bytes(256))))
        photos.rebuild(join(segments))
        self.assert_rejected(self.replace(0xC4, jpeg.dht_payload(0, 0, [0] * 14 + [2, 255], bytes(257))))
        # Missing bytes.
        self.assert_rejected(self.replace(0xC4, jpeg.dht_payload(0, 0, [0, 3])[:-1]))
        self.assert_rejected(self.replace(0xC4, bytes([0x00]) + bytes(10)))

    def test_dht_over_subscribed(self):
        for counts in ([3], [2], [0, 4], [1, 2], [0, 3, 2], [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2]):
            with self.subTest(counts=counts):
                self.assert_rejected(self.replace(0xC4, jpeg.dht_payload(0, 0, counts)))

    def test_dht_canonical_codes_fit(self):
        """Valid tables (the all-ones code unused) parse; the DC luminance table of T.81 Annex K."""
        for counts in ([1], [0, 3], [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], [0, 1, 5, 1, 1, 1, 1, 1, 1]):
            with self.subTest(counts=counts):
                segments = jpeg.chrome_segments()
                position = first(segments, 0xC4)
                segments.insert(position, Segment(0xC4, jpeg.dht_payload(0, 3, counts)))  # an unused table
                photos.rebuild(join(segments))

    def test_dri(self):
        segments = jpeg.chrome_segments()
        segments.insert(2, Segment(0xDD, b"\x00\x00\x00"))
        self.assert_rejected(join(segments))
        segments[2] = Segment(0xDD, b"\x00")
        self.assert_rejected(join(segments))
        segments[2] = Segment(0xDD, b"\x00\x10")
        photos.rebuild(join(segments))


# --- Frame (§4b-5) ---

class FrameTests(Assertions, unittest.TestCase):
    def with_sof(self, payload, marker=0xC0):
        segments = jpeg.chrome_segments()
        segments[first(segments, 0xC0)] = Segment(marker, payload)
        return join(segments)

    def test_chrome_frame(self):
        self.assertEqual(jpeg.chrome_segments()[first(jpeg.chrome_segments(), 0xC0)].payload, sof_payload())

    def test_dimensions(self):
        for width, height in ((255, 256), (256, 255), (512, 512), (128, 128), (0, 256), (256, 0),
                              (0xFFFF, 0xFFFF), (256, 0xFFFF), (0xFFFF, 256), (1, 1)):
            with self.subTest(width=width, height=height):
                self.assert_rejected(self.with_sof(sof_payload(width, height)))

    def test_precision(self):
        for precision in (12, 16, 0):
            with self.subTest(precision=precision):
                self.assert_rejected(self.with_sof(sof_payload(precision=precision)))

    def test_component_count(self):
        four = ((1, 0x22, 0), (2, 0x11, 1), (3, 0x11, 1), (4, 0x11, 1))
        self.assert_rejected(self.with_sof(sof_payload(components=four)), "CMYK / 4 components")
        self.assert_rejected(self.with_sof(sof_payload(components=((1, 0x22, 0), (2, 0x11, 1)))))
        self.assert_rejected(self.with_sof(sof_payload(components=())))
        self.assert_rejected(self.with_sof(sof_payload()[:-1]), "length doesn't match Nf")
        self.assert_rejected(self.with_sof(sof_payload() + b"\x00"))
        self.assert_rejected(self.with_sof(sof_payload()[:5]))

    def test_component_ids(self):
        for ids in ((0, 1, 2), (1, 3, 2), (1, 1, 2), (82, 71, 66), (2, 1, 3)):
            with self.subTest(ids=ids):
                comps = tuple((cid, s, t) for cid, (s, t) in zip(ids, ((0x22, 0), (0x11, 1), (0x11, 1))))
                self.assert_rejected(self.with_sof(sof_payload(components=comps)))

    def test_sampling(self):
        for luma in (0x11, 0x21, 0x12, 0x22):
            with self.subTest(luma=hex(luma)):
                comps = ((1, luma, 0), (2, 0x11, 1), (3, 0x11, 1))
                photos.rebuild(self.with_sof(sof_payload(components=comps)))
        for comps in (((1, 0x44, 0), (2, 0x11, 1), (3, 0x11, 1)), ((1, 0x31, 0), (2, 0x11, 1), (3, 0x11, 1)),
                      ((1, 0x22, 0), (2, 0x22, 1), (3, 0x11, 1)), ((1, 0x22, 0), (2, 0x11, 1), (3, 0x21, 1)),
                      ((1, 0x00, 0), (2, 0x11, 1), (3, 0x11, 1))):
            with self.subTest(comps=comps):
                self.assert_rejected(self.with_sof(sof_payload(components=comps)))

    def test_quant_table_ids(self):
        self.assert_rejected(self.with_sof(sof_payload(components=((1, 0x22, 4), (2, 0x11, 1), (3, 0x11, 1)))))
        self.assert_rejected(self.with_sof(sof_payload(components=((1, 0x22, 2), (2, 0x11, 1), (3, 0x11, 1)))),
                             "table 2 is never defined")

    def test_grayscale(self):
        gray = sof_payload(components=((1, 0x11, 0),))
        photos.rebuild(jpeg.synthetic(sof=gray, scans=[sos_payload(((1, 0x00),))]))
        for comps in (((1, 0x22, 0),), ((2, 0x11, 0),)):
            with self.subTest(comps=comps):
                self.assert_rejected(jpeg.synthetic(sof=sof_payload(components=comps), scans=[sos_payload(((1, 0),))]))

    def test_allowed_frame_types(self):
        photos.rebuild(self.with_sof(sof_payload(), 0xC1))  # extended sequential, 8-bit
        photos.rebuild(jpeg.synthetic())

    def test_rejected_frame_and_other_markers(self):
        names = {0xC3: "SOF3 lossless", 0xC5: "SOF5", 0xC6: "SOF6", 0xC7: "SOF7", 0xC9: "SOF9 arithmetic",
                 0xCA: "SOF10 arithmetic", 0xCB: "SOF11", 0xCD: "SOF13", 0xCE: "SOF14", 0xCF: "SOF15",
                 0xC8: "JPG"}
        for marker, name in names.items():
            with self.subTest(name=name):
                self.assert_rejected(self.with_sof(sof_payload(), marker))
        for marker, payload, name in ((0xCC, b"\x00\x10", "DAC"), (0xDC, b"\x01\x00", "DNL"),
                                      (0xDE, sof_payload(), "DHP"), (0xDF, b"\x11", "EXP")):
            with self.subTest(name=name):
                segments = jpeg.chrome_segments()
                segments.insert(first(segments, 0xDA), Segment(marker, payload))
                self.assert_rejected(join(segments))
        for marker in range(0xF0, 0xFE):
            with self.subTest(name=f"JPG{marker - 0xF0}"):
                segments = jpeg.chrome_segments()
                segments.insert(2, Segment(marker, b"\x00"))
                self.assert_rejected(join(segments))

    def test_dnl_after_the_scan(self):
        """DNL (and a zero height that would need one) are refused."""
        segments = jpeg.chrome_segments()
        data = join(segments)[:-2] + Segment(0xDC, b"\x01\x00").encode() + b"\xff\xd9"
        self.assert_rejected(data)
        self.assert_rejected(self.with_sof(sof_payload(height=0)))

    def test_one_frame_only(self):
        segments = jpeg.chrome_segments()
        segments.insert(first(segments, 0xC0), Segment(0xC0, sof_payload()))
        self.assert_rejected(join(segments))
        segments = jpeg.chrome_segments()
        segments.append(Segment(0xC2, sof_payload()))
        self.assert_rejected(join(segments))


# --- Scans (§4b-6) ---

class ScanTests(Assertions, unittest.TestCase):
    def test_scan_before_frame(self):
        segments = jpeg.chrome_segments()
        sos = segments.pop(first(segments, 0xDA))
        segments.insert(first(segments, 0xC0), sos)
        self.assert_rejected(join(segments))

    def test_sequential_parameters(self):
        for kwargs in ({"ss": 1}, {"se": 62}, {"se": 0}, {"ah": 1}, {"al": 1}):
            with self.subTest(**kwargs):
                self.assert_rejected(jpeg.synthetic(scans=[sos_payload(**kwargs)]))

    def test_components(self):
        for comps in (((4, 0x00),), ((1, 0x00), (1, 0x11)), ((1, 0x00), (2, 0x11), (3, 0x11), (3, 0x11)), ()):
            with self.subTest(comps=comps):
                self.assert_rejected(jpeg.synthetic(scans=[sos_payload(comps)]))
        # Non-interleaved sequential scans, one per component, are fine.
        photos.rebuild(jpeg.synthetic(scans=[sos_payload(((1, 0x00),)), sos_payload(((2, 0x11),)),
                                             sos_payload(((3, 0x11),))]))

    def test_header_length(self):
        self.assert_rejected(jpeg.synthetic(scans=[sos_payload()[:-1]]))
        self.assert_rejected(jpeg.synthetic(scans=[sos_payload() + b"\x00"]))
        self.assert_rejected(jpeg.synthetic(scans=[b""]))

    def test_undefined_huffman_table(self):
        for comps in (((1, 0x20), (2, 0x11), (3, 0x11)), ((1, 0x02), (2, 0x11), (3, 0x11)),
                      ((1, 0x00), (2, 0x44), (3, 0x11)), ((1, 0x00), (2, 0x11), (3, 0x1F))):
            with self.subTest(comps=comps):
                self.assert_rejected(jpeg.synthetic(scans=[sos_payload(comps)]))
        # Only the AC table 0 defined: a sequential scan needs both kinds.
        segments = [s for s in jpeg.chrome_segments() if not (s.marker == 0xC4 and s.payload[0] == 0x00)]
        self.assert_rejected(join(segments))

    def test_scan_limit(self):
        scans = [sos_payload(((1, 0x00),))] * photos.MAX_SCANS
        photos.rebuild(jpeg.synthetic(scans=scans))
        self.assert_rejected(jpeg.synthetic(scans=scans + [sos_payload(((1, 0x00),))]))

    def test_restart_markers(self):
        segments = jpeg.split(jpeg.synthetic())
        segments[-1].scan = b"\x12\xff\xd0\x34\xff\xd7\x56"
        self.assert_rejected(join(segments), "RST without DRI")
        segments.insert(0, Segment(0xDD, b"\x00\x00"))
        self.assert_rejected(join(segments), "RST after a zero DRI")
        segments[0] = Segment(0xDD, b"\x00\x04")
        out = photos.rebuild(join(segments))
        self.assertIn(b"\x12\xff\xd0\x34\xff\xd7\x56", out)

    def test_stuffed_bytes_kept(self):
        segments = jpeg.split(jpeg.synthetic())
        segments[-1].scan = b"\xff\x00\x01\xff\x00"
        self.assertIn(b"\xff\x00\x01\xff\x00\xff\xd9", photos.rebuild(join(segments)))

    def test_scan_ends_at_eof(self):
        segments = jpeg.split(jpeg.synthetic())
        self.assert_rejected(join(segments, tail=b""))
        self.assert_rejected(join(segments, tail=b"\xff"))


class ProgressiveTests(Assertions, unittest.TestCase):
    """Structure only: Chrome's encoder is baseline, so these files are synthetic."""

    def progressive(self, scans):
        return jpeg.synthetic(sof_marker=0xC2, scans=scans)

    def test_typical_script(self):
        dc = ((1, 0x00), (2, 0x11), (3, 0x11))
        scans = [sos_payload(dc, 0, 0, 0, 1), sos_payload(((1, 0x00),), 1, 5, 0, 2),
                 sos_payload(((3, 0x11),), 1, 63, 0, 1), sos_payload(((2, 0x11),), 1, 63, 0, 1),
                 sos_payload(((1, 0x00),), 6, 63, 0, 2), sos_payload(((1, 0x00),), 1, 63, 2, 1),
                 sos_payload(dc, 0, 0, 1, 0), sos_payload(((1, 0x00),), 1, 63, 1, 0)]
        out = photos.rebuild(self.progressive(scans))
        self.assert_clean(out)

    def test_rules(self):
        cases = {"Ss > Se": sos_payload(((1, 0x00),), 5, 4),
                 "Se > 63": sos_payload(((1, 0x00),), 1, 64),
                 "DC scan with AC coefficients": sos_payload(((1, 0x00),), 0, 5),
                 "AC scan with two components": sos_payload(((1, 0x00), (2, 0x11)), 1, 63),
                 "Ah > 13": sos_payload(((1, 0x00),), 1, 63, 14, 0),
                 "Al > 13": sos_payload(((1, 0x00),), 1, 63, 0, 14),
                 "AC table undefined": sos_payload(((1, 0x02),), 1, 63)}
        for name, scan in cases.items():
            with self.subTest(name=name):
                self.assert_rejected(self.progressive([sos_payload(((1, 0x00),), 0, 0), scan]))

    def test_dc_refinement_needs_no_table(self):
        scans = [sos_payload(((1, 0x00),), 0, 0, 0, 1), sos_payload(((1, 0x30),), 0, 0, 1, 0)]
        photos.rebuild(self.progressive(scans))
        self.assert_rejected(self.progressive([sos_payload(((1, 0x30),), 0, 0, 0, 1)]), "first DC scan: table 3 undefined")


# --- Robustness (§4b-2) ---

class MutationTests(Assertions, unittest.TestCase):
    def test_random_mutations_fail_closed(self):
        """Random byte edits either fail with InvalidImage or give a clean, stable output."""
        rng = random.Random(11)
        data = jpeg.CHROME_PHOTO
        header_end = data.index(b"\xff\xda") + 14
        accepted = 0
        for _ in range(1500):
            buf = bytearray(data)
            for _ in range(rng.randint(1, 4)):
                # Mostly in the headers, where the parser looks.
                pos = rng.randrange(header_end) if rng.random() < 0.8 else rng.randrange(len(buf))
                buf[pos] = rng.choice((0x00, 0xFF, 0x01, 0xD8, 0xD9, 0xDA, rng.randrange(256)))
            try:
                out = photos.rebuild(bytes(buf))
            except photos.InvalidImage:
                continue
            accepted += 1
            self.assertEqual(photos.rebuild(out), out)
            self.assertLessEqual(len(out), len(buf))
            self.assertTrue(out.startswith(b"\xff\xd8" + jpeg.FIXED_APP0) and out.endswith(b"\xff\xd9"))
        self.assertGreater(accepted, 0, "some edits (e.g. inside the scan) are still structurally valid")

    def test_huge_lengths_and_counts_are_cheap(self):
        # 64 KB comment segments: bounded work, no recursion, no exception other than InvalidImage.
        segments = jpeg.chrome_segments()
        segments.insert(1, Segment(0xFE, b"\xff" * 65533))
        photos.rebuild(join(segments))
        self.assert_rejected(b"\xff\xd8" + b"\xff" * 100000)


if __name__ == "__main__":
    unittest.main()
