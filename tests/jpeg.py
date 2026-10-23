"""JPEG fixtures and builders for the change 11d tests.

The real fixtures are canvas.toBlob('image/jpeg', 0.85) output at 256 x 256
from Chrome 124 and Firefox 131 (same generator: fixtures/chrome-fixtures.js;
Firefox ran in the Playwright 1.48.0 image). Hostile files are built from the
Chrome photo here by splitting the file into segments and editing or adding segments.
"""

import struct
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CHROME_PHOTO = (FIXTURES / "chrome-124-photo-q85.jpg").read_bytes()
CHROME_DETAILED = (FIXTURES / "chrome-124-detailed-q85.jpg").read_bytes()
REAL_FIXTURES = {
    "chrome-124-photo-q85": CHROME_PHOTO,
    "chrome-124-detailed-q85": CHROME_DETAILED,
    "firefox-131-photo-q85": (FIXTURES / "firefox-131-photo-q85.jpg").read_bytes(),
    "firefox-131-detailed-q85": (FIXTURES / "firefox-131-detailed-q85.jpg").read_bytes(),
}

FIXED_APP0 = bytes.fromhex("ffe000104a46494600010100000100010000")


class Segment:
    """One marker segment: marker byte, payload, and the entropy-coded data after an SOS."""

    def __init__(self, marker, payload=b"", scan=b""):
        self.marker = marker
        self.payload = bytes(payload)
        self.scan = bytes(scan)

    def encode(self, length=None):
        size = len(self.payload) + 2 if length is None else length
        return b"\xff" + bytes([self.marker]) + struct.pack(">H", size) + self.payload + self.scan

    def __repr__(self):
        return f"<Segment {self.marker:02X} {len(self.payload)}+{len(self.scan)}>"


def split(data):
    """The segments of a well-formed file (between SOI and EOI)."""
    assert data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"
    segments, pos = [], 2
    while True:
        assert data[pos] == 0xFF
        marker = data[pos + 1]
        if marker == 0xD9:
            assert pos + 2 == len(data)
            return segments
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        payload = data[pos + 4:pos + 2 + length]
        pos += 2 + length
        scan = b""
        if marker == 0xDA:
            end = pos
            while True:
                end = data.index(b"\xff", end)
                if data[end + 1] == 0 or 0xD0 <= data[end + 1] <= 0xD7:
                    end += 2
                    continue
                break
            scan, pos = data[pos:end], end
        segments.append(Segment(marker, payload, scan))


def join(segments, head=b"\xff\xd8", tail=b"\xff\xd9"):
    return head + b"".join(s.encode() if isinstance(s, Segment) else s for s in segments) + tail


def chrome_segments():
    return split(CHROME_PHOTO)


def first(segments, marker):
    return next(i for i, s in enumerate(segments) if s.marker == marker)


def sof_payload(width=256, height=256, components=((1, 0x22, 0), (2, 0x11, 1), (3, 0x11, 1)), precision=8):
    out = bytes([precision]) + struct.pack(">HH", height, width) + bytes([len(components)])
    for component in components:
        out += bytes(component)
    return out


def sos_payload(components=((1, 0x00), (2, 0x11), (3, 0x11)), ss=0, se=63, ah=0, al=0):
    out = bytes([len(components)])
    for component in components:
        out += bytes(component)
    return out + bytes([ss, se, (ah << 4) | al])


def dht_payload(table_class, table, counts, symbols=None):
    counts = list(counts) + [0] * (16 - len(counts))
    total = sum(counts)
    symbols = bytes(range(total)) if symbols is None else symbols
    return bytes([(table_class << 4) | table]) + bytes(counts) + symbols


def tables():
    """Chrome's DQT and DHT segments (tables 0 and 1 of each kind)."""
    return [s for s in chrome_segments() if s.marker in (0xDB, 0xC4)]


def synthetic(scans=None, sof_marker=0xC0, sof=None, extra=(), dri=None):
    """A structurally valid file with Chrome's tables, 1-byte scans: for counting and progressive rules.

    It isn't decodable image data; rebuild() checks structure only.
    """
    # Chrome's JFIF APP0 first, as browsers send one (rebuild() never grows a file, §4b-8).
    segments = [chrome_segments()[0]] + list(extra) + tables()
    if dri is not None:
        segments.append(Segment(0xDD, struct.pack(">H", dri)))
    segments.append(Segment(sof_marker, sof if sof is not None else sof_payload()))
    for payload in scans if scans is not None else [sos_payload()]:
        segments.append(Segment(0xDA, payload, b"\x12\x34"))
    return join(segments)


def exif_app1():
    """APP1 Exif with GPS tags and an embedded thumbnail JPEG of the uncropped photo."""
    thumbnail = b"\xff\xd8\xff\xdb\x00\x43\x00" + bytes(64) + b"\xff\xd9"
    tiff = (b"MM\x00\x2a\x00\x00\x00\x08" + b"\x00\x02" + b"\x88\x25\x00\x04\x00\x00\x00\x01\x00\x00\x00\x26"
            + b"\x02\x01\x00\x04\x00\x00\x00\x01\x00\x00\x00\x40" + b"GPSLatitude 51.5007N GPSLongitude 0.1246W")
    return Segment(0xE1, b"Exif\x00\x00" + tiff + thumbnail)


def adobe_app14(transform):
    return Segment(0xEE, b"Adobe" + b"\x00\x64" + b"\x00\x00" + b"\x00\x00" + bytes([transform]))
