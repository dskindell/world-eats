"""Change 11d: profile photo checks (contract 11d §4, security notes §4b-1 … §4b-10).

rebuild() parses a browser's 256 x 256 JPEG structurally and returns a new file
made only of the image segments (tables, frame, scans) behind a fixed JFIF
header: no APPn/COM metadata (EXIF, GPS, thumbnails, ICC), no fill bytes, no
trailing data. Pure functions over bytes: no I/O, no logging, no recursion.
Anything unexpected raises InvalidImage, whose text never describes the input.

The scan data itself is copied, not decoded; only its framing is checked
(byte stuffing, restart markers only after a non-zero DRI).
"""

SIZE = 256  # the only accepted width and height
MAX_BYTES = 131072  # the upload limit; nothing larger is parsed
MAX_SEGMENTS = 64  # marker segments with a length field, of any kind
MAX_SCANS = 32

SOI, EOI, SOS = 0xD8, 0xD9, 0xDA
DQT, DHT, DRI = 0xDB, 0xC4, 0xDD
APP14, COM = 0xEE, 0xFE
BASELINE, EXTENDED, PROGRESSIVE = 0xC0, 0xC1, 0xC2
SEQUENTIAL = frozenset({BASELINE, EXTENDED})
APP_MARKERS = frozenset(range(0xE0, 0xF0))
RST_MARKERS = frozenset(range(0xD0, 0xD8))
LUMA_SAMPLING = frozenset({0x11, 0x21, 0x12, 0x22})  # 1x1, 2x1, 1x2, 2x2

# Fixed server header: JFIF 1.01, no units, 1:1 density, no thumbnail.
JFIF_APP0 = bytes.fromhex("ffe000104a46494600010100000100010000")


class InvalidImage(Exception):
    """The upload isn't a JPEG this app accepts. Deliberately carries no detail."""

    def __init__(self):
        super().__init__("invalid image")


def rebuild(data):
    """The rebuilt JPEG for `data`, or InvalidImage. The output is re-validated."""
    try:
        if type(data) is not bytes or len(data) > MAX_BYTES:
            raise InvalidImage()
        out = _rebuild(data)
        # The output must parse to itself. The size check is a sanity bound that can't trigger
        # in practice: rebuilding only drops bytes, then adds the fixed APP0 (a file sent with
        # no APPn grows by exactly that). _rebuild() has no input cap, so a full-size file passes.
        if len(out) > len(data) + len(JFIF_APP0) or _rebuild(out) != out:
            raise InvalidImage()
        return out
    except InvalidImage:
        raise
    except Exception:
        raise InvalidImage() from None


class _Parse:
    """State while walking one file."""

    def __init__(self):
        self.frame = None  # (marker, {component id: quant table id}) once the SOF is seen
        self.quant = set()  # defined quantisation table ids
        self.huffman = set()  # defined (class, id) pairs: class 0 DC, 1 AC
        self.restart_interval = 0
        self.scans = 0
        self.segments = 0
        self.adobe_transform = None


def _rebuild(data):
    n = len(data)
    if n < 4 or data[0] != 0xFF or data[1] != SOI:
        raise InvalidImage()
    state = _Parse()
    kept = []  # (start, end) of each kept segment's bytes after its marker
    pos = 2
    while True:
        if pos >= n or data[pos] != 0xFF:
            raise InvalidImage()  # only markers (with fill bytes) between segments
        while pos < n and data[pos] == 0xFF:
            pos += 1  # fill bytes
        if pos >= n:
            raise InvalidImage()
        marker = data[pos]
        pos += 1
        if marker == EOI:
            if pos != n or state.frame is None or state.scans == 0:
                raise InvalidImage()  # trailing bytes, or no image
            break
        if marker < 0xC0 or marker == SOI or marker in RST_MARKERS:
            raise InvalidImage()  # stuffed 00, TEM, reserved, a second SOI, RST outside a scan
        if pos + 2 > n:
            raise InvalidImage()
        length = (data[pos] << 8) | data[pos + 1]
        if length < 2 or pos + length > n:
            raise InvalidImage()
        state.segments += 1
        if state.segments > MAX_SEGMENTS:
            raise InvalidImage()
        start, end = pos + 2, pos + length  # the segment's payload
        if marker in APP_MARKERS or marker == COM:
            if marker == APP14:
                _adobe(data, start, end, state)
            pos = end  # dropped
            continue
        if marker == DQT:
            _dqt(data, start, end, state)
        elif marker == DHT:
            _dht(data, start, end, state)
        elif marker == DRI:
            if length != 4:
                raise InvalidImage()
            state.restart_interval = (data[start] << 8) | data[start + 1]
        elif marker in (BASELINE, EXTENDED, PROGRESSIVE):
            _sof(data, marker, start, end, state)
        elif marker == SOS:
            _sos(data, start, end, state)
            end = _scan_end(data, end, state)
        else:
            raise InvalidImage()  # other SOFs (lossless, hierarchical, arithmetic), DAC, DNL, DHP, EXP, JPGn
        kept.append((pos - 1, end))
        pos = end
    if state.adobe_transform not in (None, 1) and len(state.frame[1]) == 3:
        raise InvalidImage()  # Adobe APP14 saying the 3 components aren't YCbCr
    parts = [b"\xff\xd8", JFIF_APP0]
    for start, end in kept:
        parts.append(b"\xff")
        parts.append(data[start:end])  # marker byte, length, payload (and scan data after an SOS)
    parts.append(b"\xff\xd9")
    return b"".join(parts)


def _adobe(data, start, end, state):
    if data[start:start + 5] != b"Adobe":
        return
    if end - start < 12:
        raise InvalidImage()
    state.adobe_transform = data[start + 11]


def _dqt(data, start, end, state):
    size = end - start
    if size == 0 or size % 65:
        raise InvalidImage()  # 8-bit tables only: 1 + 64 bytes each
    for pos in range(start, end, 65):
        precision, table = data[pos] >> 4, data[pos] & 0x0F
        if precision != 0 or table > 3:
            raise InvalidImage()
        state.quant.add(table)


def _dht(data, start, end, state):
    pos = start
    if pos == end:
        raise InvalidImage()
    while pos < end:
        if pos + 17 > end:
            raise InvalidImage()
        table_class, table = data[pos] >> 4, data[pos] & 0x0F
        if table_class > 1 or table > 3:
            raise InvalidImage()
        total = 0
        code = 0
        for bits in range(1, 17):
            count = data[pos + bits]
            total += count
            code += count
            # Canonical codes (T.81 Annex C): the codes of each length must fit,
            # leaving the all-ones code unused (as libjpeg requires).
            if count and code >= (1 << bits):
                raise InvalidImage()
            code <<= 1
        if not 1 <= total <= 256:
            raise InvalidImage()
        pos += 17 + total
        if pos > end:
            raise InvalidImage()
        state.huffman.add((table_class, table))


def _sof(data, marker, start, end, state):
    if state.frame is not None:
        raise InvalidImage()  # exactly one frame
    if end - start < 6:
        raise InvalidImage()
    precision = data[start]
    height = (data[start + 1] << 8) | data[start + 2]
    width = (data[start + 3] << 8) | data[start + 4]
    count = data[start + 5]
    if precision != 8 or height != SIZE or width != SIZE or count not in (1, 3):
        raise InvalidImage()
    if end - start != 6 + 3 * count:
        raise InvalidImage()
    components = {}
    for index in range(count):
        pos = start + 6 + 3 * index
        component, sampling, table = data[pos], data[pos + 1], data[pos + 2]
        if component != index + 1 or table > 3:
            raise InvalidImage()  # ids 1 (and 2, 3) in order, so also unique
        if index == 0:
            allowed = {0x11} if count == 1 else LUMA_SAMPLING
        else:
            allowed = {0x11}
        if sampling not in allowed:
            raise InvalidImage()
        components[component] = table
    state.frame = (marker, components)


def _sos(data, start, end, state):
    if state.frame is None:
        raise InvalidImage()
    marker, components = state.frame
    if end - start < 1:
        raise InvalidImage()
    count = data[start]
    if not 1 <= count <= len(components) or end - start != 4 + 2 * count:
        raise InvalidImage()
    pos = start + 1 + 2 * count
    first, last, approx = data[pos], data[pos + 1], data[pos + 2]
    high, low = approx >> 4, approx & 0x0F
    if marker in SEQUENTIAL:
        if first != 0 or last != 63 or approx != 0:
            raise InvalidImage()
        needs_dc = needs_ac = True
    else:
        if first > last or last > 63 or high > 13 or low > 13:
            raise InvalidImage()
        if first == 0 and last != 0:
            raise InvalidImage()  # a DC scan carries only the DC coefficient
        if first > 0 and count != 1:
            raise InvalidImage()  # AC scans have one component
        needs_dc = first == 0 and high == 0  # DC refinement scans use no Huffman table
        needs_ac = first > 0
    if state.scans == 0:
        # Every quantisation table the frame uses is defined before the first scan.
        if any(table not in state.quant for table in components.values()):
            raise InvalidImage()
    seen = set()
    for index in range(count):
        pos = start + 1 + 2 * index
        component, tables = data[pos], data[pos + 1]
        if component not in components or component in seen:
            raise InvalidImage()
        seen.add(component)
        dc, ac = tables >> 4, tables & 0x0F
        if needs_dc and (0, dc) not in state.huffman:
            raise InvalidImage()
        if needs_ac and (1, ac) not in state.huffman:
            raise InvalidImage()
    state.scans += 1
    if state.scans > MAX_SCANS:
        raise InvalidImage()


def _scan_end(data, pos, state):
    """The index of the marker ending the entropy-coded data that starts at `pos`."""
    while True:
        pos = data.find(b"\xff", pos)
        if pos < 0 or pos + 1 >= len(data):
            raise InvalidImage()
        following = data[pos + 1]
        if following == 0x00:
            pos += 2  # a stuffed FF
        elif following in RST_MARKERS:
            if state.restart_interval == 0:
                raise InvalidImage()
            pos += 2
        else:
            return pos
