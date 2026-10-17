"""Builds app/static/map.json from Natural Earth.

Run by hand, never in the image:

    python3 -I tools/build_map.py <path to ne_50m_admin_0_countries.geojson> app/static/map.json

Source (public domain), Natural Earth 1:50m Admin 0 - Countries, v5.1.1, commit-pinned:
https://raw.githubusercontent.com/nvkelso/natural-earth-vector/9380cca83db5f9aef52d5e762765100745f84b27/geojson/ne_50m_admin_0_countries.geojson
SHA-256: 3e458fc036ad0a66411f2c1e6cac49c5d7bfb81cb1123bc513b22511a2b7fdeb

Download it into a new empty directory outside the repo. The file is treated as untrusted: the
hash is checked before parsing, and no string from it reaches the output.
"""

import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.append(str(Path(__file__).resolve().parent.parent / "app"))

import countries  # noqa: E402

SOURCE_SHA256 = "3e458fc036ad0a66411f2c1e6cac49c5d7bfb81cb1123bc513b22511a2b7fdeb"
ATTRIBUTION = "Made with Natural Earth (1:50m Admin 0, v5.1.1). Public domain."

# Natural Earth draws these separately; set to {} to leave them as grey "other" land.
MERGE = {"Somaliland": "SO", "Northern Cyprus": "CY", "Siachen Glacier": "IN"}

TOLERANCE = 0.2  # Douglas-Peucker, in canvas units
DOT_SIZE = 6  # a country whose drawn rings are all smaller than this both ways gets a dot
TOP_KEYS = {"attribution", "width", "height", "countries", "dots", "other"}
PATH_RE = re.compile(r"[MLZ0-9.,\-]+")
_PAIR = r"-?[0-9]+\.[0-9],-?[0-9]+\.[0-9]"
PATH_SHAPE_RE = re.compile(rf"(?:M{_PAIR}(?:L{_PAIR})*Z)+")
NUMBER_PAIR_RE = re.compile(r"(-?[0-9]+\.[0-9]),(-?[0-9]+\.[0-9])")

# Equal Earth (Savric, Patterson & Jenny 2018), spherical form.
A1, A2, A3, A4 = 1.340264, -0.081106, 0.000893, 0.003796
M = math.sqrt(3) / 2


class MapError(Exception):
    pass


def equal_earth(lon, lat):
    """Projects degrees to Equal Earth units."""
    lam = math.radians(lon)
    theta = math.asin(M * math.sin(math.radians(lat)))
    t2 = theta * theta
    t6 = t2 * t2 * t2
    x = 2 * math.sqrt(3) * lam * math.cos(theta) / (3 * (9 * A4 * t6 * t2 + 7 * A3 * t6 + 3 * A2 * t2 + A1))
    y = theta * (A4 * t6 * t2 + A3 * t6 + A2 * t2 + A1)
    return x, y


SCALE = 500 / equal_earth(180, 0)[0]
WIDTH = 1000
HEIGHT_EXACT = 2 * equal_earth(0, 90)[1] * SCALE  # the uncropped canvas
MARGIN = 20  # tenths of a unit kept above the topmost and below the bottom-most point
MAX_HEIGHT = 10000


def to_canvas(lon, lat):
    x, y = equal_earth(lon, lat)
    return x * SCALE + WIDTH / 2, HEIGHT_EXACT / 2 - y * SCALE


# --- Reading and checking the source ---

def _reject_constant(name):
    raise MapError("refused: the file contains NaN or Infinity")


def parse(data, expected_sha256):
    """Checks the hash of the raw bytes, then parses. Never parses a file with the wrong hash."""
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256:
        raise MapError(f"refused: SHA-256 {digest} doesn't match the expected {expected_sha256}")
    try:
        return json.loads(data, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as e:
        raise MapError("refused: not valid JSON") from e


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def checked_rings(feature, index):
    """The feature's rings as lists of (lon, lat), after checking every coordinate."""
    geometry = feature.get("geometry") if isinstance(feature, dict) else None
    if not isinstance(geometry, dict):
        raise MapError(f"refused: feature {index} has no geometry")
    kind, coords = geometry.get("type"), geometry.get("coordinates")
    if kind == "Polygon":
        polygons = [coords]
    elif kind == "MultiPolygon" and isinstance(coords, list):
        polygons = coords
    else:
        raise MapError(f"refused: feature {index} isn't a Polygon or MultiPolygon")
    rings = []
    for polygon in polygons:
        if not isinstance(polygon, list):
            raise MapError(f"refused: feature {index} has a malformed polygon")
        for ring in polygon:
            if not isinstance(ring, list) or not ring:
                raise MapError(f"refused: feature {index} has a malformed ring")
            points = []
            for position in ring:
                if not (isinstance(position, list) and len(position) == 2
                        and _number(position[0]) and _number(position[1])):
                    raise MapError(f"refused: feature {index} has a malformed coordinate")
                lon, lat = position
                # Comparisons first: they work on huge ints, which float() can't convert.
                if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                    raise MapError(f"refused: feature {index} has a coordinate out of range")
                lon, lat = float(lon), float(lat)
                if not (math.isfinite(lon) and math.isfinite(lat)):
                    raise MapError(f"refused: feature {index} has a non-finite coordinate")
                points.append((lon, lat))
            rings.append(points)
    if not rings:
        raise MapError(f"refused: feature {index} has no rings")
    return rings


def _text(properties, key):
    value = properties.get(key)
    return value if isinstance(value, str) else None


def classify(doc, merge):
    """Returns ({code: [ring, ...]}, [[ring, ...] per other feature]). Rings are (lon, lat) lists."""
    features = doc.get("features") if isinstance(doc, dict) else None
    if not isinstance(features, list):
        raise MapError("refused: no feature list")
    for target in merge.values():
        if target not in countries.BY_CODE:
            raise MapError(f"MERGE target {target} isn't one of the 197")
    ours, others = {}, []
    merged = {name: [] for name in merge}
    for index, feature in enumerate(features):
        rings = checked_rings(feature, index)
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            raise MapError(f"refused: feature {index} has no properties")
        code, admin = _text(properties, "ISO_A2_EH"), _text(properties, "ADMIN")
        if code == "AQ":
            continue
        if admin in merge:
            merged[admin].append(rings)
        elif code in countries.BY_CODE and properties.get("TYPE") != "Dependency":
            if code in ours:
                raise MapError(f"{code} matches more than one feature")
            ours[code] = rings
        else:
            others.append(rings)
    for name, found in merged.items():
        if len(found) != 1:
            raise MapError(f"MERGE name {name!r} found {len(found)} times, expected once")
    missing = sorted(set(countries.BY_CODE) - set(ours))
    if missing:
        raise MapError(f"no feature for {', '.join(missing)}")
    for name, found in merged.items():
        ours[merge[name]] = ours[merge[name]] + found[0]
    return ours, others


# --- Geometry ---

def _segment_distance(p, a, b):
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def douglas_peucker(points, tolerance):
    """Simplifies a polyline; the first and last points are always kept."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        best, best_index = 0.0, -1
        for i in range(first + 1, last):
            d = _segment_distance(points[i], points[first], points[last])
            if d > best:
                best, best_index = d, i
        if best > tolerance:
            keep[best_index] = True
            stack += [(first, best_index), (best_index, last)]
    return [p for p, k in zip(points, keep) if k]


def signed_area(points):
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])) / 2


def centroid(points):
    area = signed_area(points)
    if abs(area) < 1e-12:
        return sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points)
    cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        cross = x1 * y2 - x2 * y1
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    return cx / (6 * area), cy / (6 * area)


def fmt(value):
    text = f"{value:.1f}"
    return "0.0" if text == "-0.0" else text


def ring_points(points):
    """Rounded points for a path: consecutive duplicates and the closing point removed."""
    out = []
    for x, y in points:
        p = (fmt(x), fmt(y))
        if not out or out[-1] != p:
            out.append(p)
    if len(out) > 1 and out[-1] == out[0]:
        out.pop()
    return out


def ring_path(points):
    return "M" + "L".join(f"{x},{y}" for x, y in points) + "Z"


def kept_rings(rings):
    """Rounded points of the projected rings that keep at least 3 points after simplification."""
    kept = []
    for ring in rings:
        points = ring_points(douglas_peucker(ring, TOLERANCE))
        if len(points) >= 3:
            kept.append(points)
    return kept


def country_rings(rings):
    """The rings drawn for one of ours: never none."""
    kept = kept_rings(rings)
    if not kept:
        # Never lose one of ours: keep its largest ring unsimplified (rounded, no deduplication).
        largest = max(rings, key=lambda r: abs(signed_area(r)))
        points = [(fmt(x), fmt(y)) for x, y in largest]
        if len(points) > 1 and points[-1] == points[0]:
            points.pop()
        if not points:
            raise MapError("a country has no usable ring")
        kept = [points]
    return kept


def dot_for(kept):
    """[x, y] when every drawn ring is too small to tap at 1x, else None (contract v2.1)."""
    rings = [[(float(x), float(y)) for x, y in ring] for ring in kept]
    for ring in rings:
        xs, ys = [x for x, _ in ring], [y for _, y in ring]
        if max(xs) - min(xs) >= DOT_SIZE or max(ys) - min(ys) >= DOT_SIZE:
            return None
    largest = max(rings, key=lambda r: abs(signed_area(r)))
    return [float(fmt(v)) for v in centroid(largest)]


def project(rings):
    return [[to_canvas(lon, lat) for lon, lat in ring] for ring in rings]


# --- Building and writing ---

def tenths(text):
    return round(float(text) * 10)


def crop(country_rings_by_code, other_rings, dots):
    """Shifts every Y so the topmost point is at MARGIN, by whole tenths (contract v2.2).

    Returns (countries, other, dots, height, offset); offset is what was added to every Y.
    """
    ys = [tenths(y) for rings in [*country_rings_by_code.values(), *other_rings]
          for ring in rings for _, y in ring]
    ys += [round(y * 10) for _, y in dots.values()]
    shift = MARGIN - min(ys)

    def path(rings):
        return "".join(ring_path([(x, fmt((tenths(y) + shift) / 10)) for x, y in ring]) for ring in rings)

    moved_dots = {code: [x, float(fmt((round(y * 10) + shift) / 10))] for code, (x, y) in dots.items()}
    countries_out = {code: path(rings) for code, rings in country_rings_by_code.items()}
    others_out = [path(rings) for rings in other_rings]
    height = (max(ys) + shift + MARGIN) / 10
    return countries_out, others_out, moved_dots, float(fmt(height)), float(fmt(shift / 10))


def build_with_offset(data, expected_sha256=SOURCE_SHA256, merge=None):
    """(map, offset): offset is the Y shift from to_canvas() to the cropped canvas."""
    merge = MERGE if merge is None else merge
    ours, others = classify(parse(data, expected_sha256), merge)
    drawn, dots = {}, {}
    for code in sorted(ours):
        drawn[code] = country_rings(project(ours[code]))
        dot = dot_for(drawn[code])
        if dot is not None:
            dots[code] = dot
    other_rings = [kept for kept in (kept_rings(project(rings)) for rings in others) if kept]
    countries_out, others_out, dots_out, height, offset = crop(drawn, other_rings, dots)
    result = {"attribution": ATTRIBUTION, "width": WIDTH, "height": height,
              "countries": countries_out, "dots": dots_out, "other": others_out}
    return result, offset


def build(data, expected_sha256=SOURCE_SHA256, merge=None):
    """The map as a dict, from the raw GeoJSON bytes."""
    return build_with_offset(data, expected_sha256, merge)[0]


def check(result):
    """The pre-write assertions (contract 2.2). Raises MapError on the first failure."""
    if not isinstance(result, dict) or set(result) != TOP_KEYS:
        raise MapError("check: wrong top-level keys")
    height = result["height"]
    if result["attribution"] != ATTRIBUTION or result["width"] != WIDTH:
        raise MapError("check: wrong attribution or width")
    if not (isinstance(height, float) and math.isfinite(height) and 1 <= height <= MAX_HEIGHT):
        raise MapError("check: height must be a number from 1 to 10000")
    if set(result["countries"]) != set(countries.BY_CODE) or len(result["countries"]) != 197:
        raise MapError("check: countries must be exactly the 197 codes")
    if not set(result["dots"]) <= set(result["countries"]):
        raise MapError("check: a dot has no country")
    ys = []
    for path in list(result["countries"].values()) + list(result["other"]):
        if not isinstance(path, str) or not PATH_RE.fullmatch(path):
            raise MapError("check: a path is empty or has a character outside M L Z 0-9 . , -")
        if not PATH_SHAPE_RE.fullmatch(path):
            raise MapError("check: a path isn't made of M/L/Z and x,y pairs")
        for x, y in NUMBER_PAIR_RE.findall(path):
            _check_point(float(x), float(y), height)
            ys.append(float(y))
    for point in result["dots"].values():
        if not (isinstance(point, list) and len(point) == 2 and all(isinstance(v, float) for v in point)):
            raise MapError("check: a dot isn't two numbers")
        _check_point(*point, height)
        ys.append(point[1])
    if min(ys) != MARGIN / 10 or max(ys) != float(fmt(height - MARGIN / 10)):
        raise MapError("check: the map isn't cropped to a 2-unit margin")
    return result


def _check_point(x, y, height):
    if not (math.isfinite(x) and math.isfinite(y) and -1 <= x <= WIDTH + 1 and -1 <= y <= height + 1):
        raise MapError("check: a coordinate is outside the canvas")


def render(result):
    text = json.dumps(check(result), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return (text + "\n").encode("ascii")


def write_map(input_path, output_path, expected_sha256=SOURCE_SHA256, merge=None):
    """Reads, builds, checks, then writes atomically. Nothing is written if any step fails."""
    result, offset = build_with_offset(Path(input_path).read_bytes(), expected_sha256, merge)
    body = render(result)
    output = Path(output_path)
    temp = output.with_name(output.name + ".tmp")
    try:
        temp.write_bytes(body)
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    return body, result["height"], offset


def main(argv):
    if len(argv) != 3:
        print("usage: python3 -I tools/build_map.py <ne_50m_admin_0_countries.geojson> <map.json>",
              file=sys.stderr)
        return 2
    try:
        body, height, offset = write_map(argv[1], argv[2])
    except (MapError, OSError) as e:
        print(f"build_map: {e}", file=sys.stderr)
        return 1
    print(f"wrote {argv[2]} ({len(body)} bytes, height {height}, Y offset {offset})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
