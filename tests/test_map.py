"""Feature 6: the world map data.

MapFileTests check the generated app/static/map.json; MapServeTests check how it's served;
the Build* tests run tools/build_map.py on a tiny synthetic GeoJSON.
"""

import copy
import hashlib
import importlib.util
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import SECURITY_HEADERS, ServerTestCase

import countries  # noqa: E402  (app/ is on sys.path via tests.helpers)

ROOT = Path(__file__).resolve().parent.parent
MAP_FILE = ROOT / "app" / "static" / "map.json"
SCRIPT = ROOT / "tools" / "build_map.py"
PATH_RE = re.compile(r"[MLZ0-9.,\-]+")
PATH_SHAPE_RE = re.compile(r"(?:M-?\d+\.\d,-?\d+\.\d(?:L-?\d+\.\d,-?\d+\.\d)*Z)+")
PAIR_RE = re.compile(r"(-?\d+\.\d),(-?\d+\.\d)")
TOP_KEYS = {"attribution", "width", "height", "countries", "dots", "other"}
# What the crop added to every Y of the pinned source (build_map.py prints it); see test_crop_offset.
MAP_Y_OFFSET = -0.3


def load_script():
    spec = importlib.util.spec_from_file_location("build_map", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_map = load_script()


def strict_json(data):
    def refuse(name):
        raise ValueError(name)
    return json.loads(data, parse_constant=refuse)


def rings_of(path):
    return [[tuple(map(float, p.split(","))) for p in ring.split("L")]
            for ring in re.findall(r"M([^Z]*)Z", path)]


def point_in_ring(ring, point):
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def rings_containing(path, point):
    return sum(point_in_ring(ring, point) for ring in rings_of(path))


def point_in_path(path, point):
    """Filled under fill-rule: evenodd (what the frontend uses)."""
    return rings_containing(path, point) % 2 == 1


def on_map(lon, lat, offset=MAP_Y_OFFSET):
    x, y = build_map.to_canvas(lon, lat)
    return x, y + offset


# --- The generated file ---

class MapFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = MAP_FILE.read_bytes()
        cls.map = strict_json(cls.raw)

    def assertInCanvas(self, x, y):
        self.assertTrue(-1 <= x <= self.map["width"] + 1, x)
        self.assertTrue(-1 <= y <= self.map["height"] + 1, y)

    def test_size(self):
        self.assertLess(len(self.raw), 400 * 1024)

    def test_top_level(self):
        self.assertIsInstance(self.map, dict)
        self.assertEqual(set(self.map), TOP_KEYS)
        self.assertEqual(self.map["attribution"], "Made with Natural Earth (1:50m Admin 0, v5.1.1). Public domain.")
        self.assertEqual(self.map["width"], 1000)
        self.assertEqual(self.map["height"], 435.2)

    def test_cropped(self):
        """Contract v2.2: 2 units of sea above the topmost and below the bottom-most point."""
        ys = [y for path in [*self.map["countries"].values(), *self.map["other"]]
              for ring in rings_of(path) for _, y in ring]
        ys += [y for _, y in self.map["dots"].values()]
        self.assertEqual(min(ys), 2.0)
        self.assertAlmostEqual(max(ys), self.map["height"] - 2, places=6)

    def test_crop_offset(self):
        """MAP_Y_OFFSET really is the file's shift: known places land in their countries."""
        for code, (lon, lat) in {"FR": (2.35, 48.86), "BR": (-47.9, -15.8), "AU": (149.1, -35.3),
                                 "CA": (-75.7, 45.4), "ZA": (28.2, -25.75), "CL": (-70.9, -53.2)}.items():
            with self.subTest(code=code):
                self.assertTrue(point_in_path(self.map["countries"][code], on_map(lon, lat)))

    def test_exactly_the_197_countries(self):
        self.assertEqual(set(self.map["countries"]), set(countries.BY_CODE))
        self.assertEqual(len(self.map["countries"]), 197)

    def test_paths(self):
        self.assertIsInstance(self.map["other"], list)
        paths = list(self.map["countries"].items()) + [("other", p) for p in self.map["other"]]
        for name, path in paths:
            with self.subTest(name=name):
                self.assertIsInstance(path, str)
                self.assertTrue(path)
                self.assertTrue(PATH_RE.fullmatch(path))
                self.assertTrue(PATH_SHAPE_RE.fullmatch(path))
                for x, y in PAIR_RE.findall(path):
                    self.assertInCanvas(float(x), float(y))

    def test_dots(self):
        dots = self.map["dots"]
        self.assertIsInstance(dots, dict)
        self.assertLessEqual(set(dots), set(self.map["countries"]))
        for code in ("AD", "MC", "VA", "SM", "LI", "MT", "SG",
                     "KI", "MH", "FM", "PW", "TO", "CV", "ST"):  # island states spread over a wide area
            self.assertIn(code, dots)
        for code in ("RU", "BR", "FR"):
            self.assertNotIn(code, dots)
        for code, point in dots.items():
            with self.subTest(code=code):
                self.assertIsInstance(point, list)
                self.assertEqual(len(point), 2)
                self.assertTrue(all(isinstance(v, float) for v in point))
                self.assertInCanvas(*point)
                # The dot sits on its country, not somewhere else on the map.
                xs = [x for ring in rings_of(self.map["countries"][code]) for x, _ in ring]
                ys = [y for ring in rings_of(self.map["countries"][code]) for _, y in ring]
                self.assertTrue(min(xs) - 1 <= point[0] <= max(xs) + 1)
                self.assertTrue(min(ys) - 1 <= point[1] <= max(ys) + 1)

    def test_dot_rule(self):
        """Contract v2.1: a dot exactly when every drawn ring is smaller than 6 x 6."""
        for code, path in self.map["countries"].items():
            with self.subTest(code=code):
                small = all(max(x for x, _ in r) - min(x for x, _ in r) < 6
                            and max(y for _, y in r) - min(y for _, y in r) < 6 for r in rings_of(path))
                self.assertEqual(code in self.map["dots"], small)

    def test_lesotho_is_a_hole_in_south_africa(self):
        """Under even-odd, Maseru is inside Lesotho and not filled as South Africa."""
        maseru = on_map(27.48, -29.31)
        self.assertTrue(point_in_path(self.map["countries"]["LS"], maseru))
        self.assertEqual(rings_containing(self.map["countries"]["ZA"], maseru), 2)  # outer ring + hole
        self.assertFalse(point_in_path(self.map["countries"]["ZA"], maseru))
        self.assertTrue(point_in_path(self.map["countries"]["ZA"], on_map(28.2, -25.75)))  # Pretoria

    def test_merged_areas(self):
        """Somaliland, Northern Cyprus and Siachen Glacier are part of SO, CY and IN, not grey land."""
        places = {"SO": [(44.06, 9.56), (45.34, 2.04)],  # Hargeisa, Mogadishu
                  "CY": [(33.9, 35.3), (32.9, 34.8)],  # north, south
                  "IN": [(77.1, 35.42), (77.2, 28.6)]}  # Siachen, Delhi
        for code, points in places.items():
            for lon, lat in points:
                with self.subTest(code=code, lon=lon):
                    point = on_map(lon, lat)
                    self.assertTrue(point_in_path(self.map["countries"][code], point))
                    self.assertFalse(any(point_in_path(p, point) for p in self.map["other"]))

    def test_canonical_encoding(self):
        """Sorted keys, fixed separators, ASCII, one trailing newline: what the script writes."""
        self.assertEqual(self.raw, (json.dumps(self.map, sort_keys=True, separators=(",", ":"))
                                    + "\n").encode("ascii"))
        for bad in (b"nan", b"inf", b"e+", b"e-", b"<", b">", b"script"):
            self.assertNotIn(bad, self.raw.lower())


class MapServeTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        shutil.copyfile(MAP_FILE, Path(self.static_dir, "map.json"))

    def assertStaticHeaders(self, resp):
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(resp.headers.get_all(name), [value], name)
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(resp.headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(resp.headers["Cache-Control"], "no-cache")

    def test_get(self):
        resp = self.request("GET", "/map.json")
        self.assertEqual(resp.status, 200)
        self.assertStaticHeaders(resp)
        self.assertEqual(resp.body, MAP_FILE.read_bytes())
        self.assertEqual(resp.headers["Content-Length"], str(MAP_FILE.stat().st_size))
        self.assertEqual(set(strict_json(resp.body)), TOP_KEYS)

    def test_head(self):
        resp = self.request("HEAD", "/map.json")
        self.assertEqual(resp.status, 200)
        self.assertStaticHeaders(resp)
        self.assertEqual(resp.body, b"")
        self.assertEqual(resp.headers["Content-Length"], str(MAP_FILE.stat().st_size))

    def test_disallowed_host(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                resp = self.raw_request(method, "/map.json", host="evil.test")
                self.assertEqual(resp.status, 421)
                self.assertNotIn(b"attribution", resp.body)

    def test_writes_not_allowed(self):
        for method in ("POST", "PATCH"):
            with self.subTest(method=method):
                resp = self.request(method, "/map.json", {})
                self.assertEqual(resp.status, 405)
                self.assertEqual(resp.error, "Method not allowed")
        self.assertEqual(self.request("DELETE", "/map.json").status, 405)

    def test_only_the_exact_path(self):
        for path in ("/map.json/", "/MAP.JSON", "/static/map.json", "/map.json%00", "/map"):
            with self.subTest(path=path):
                self.assertEqual(self.raw_request("GET", path).status, 404)

    def test_missing_file_is_404(self):
        Path(self.static_dir, "map.json").unlink()
        self.assertEqual(self.request("GET", "/map.json").status, 404)


# --- The build script, on synthetic data ---

TINY = {"AD", "MC"}  # 0.5 degree squares: dots
MERGE_NAMES = {"Somaliland": "SO", "Northern Cyprus": "CY", "Siachen Glacier": "IN"}


def square(lon, lat, size):
    return [[lon, lat], [lon + size, lat], [lon + size, lat + size], [lon, lat + size], [lon, lat]]


def feature(code, ring_or_rings, admin=None, kind="Sovereign country", name=None, multi=False):
    properties = {"ISO_A2_EH": code, "ADMIN": admin or f"Land {code}", "TYPE": kind, "NAME": name or code}
    if multi:
        geometry = {"type": "MultiPolygon", "coordinates": [[r] for r in ring_or_rings]}
    else:
        geometry = {"type": "Polygon", "coordinates": [ring_or_rings]}
    return {"type": "Feature", "properties": properties, "geometry": geometry}


def base_features():
    """One 4-degree square per country on a grid, the merge features, Antarctica and a dependency."""
    features = []
    for i, code in enumerate(sorted(countries.BY_CODE)):
        lon, lat = -170 + (i % 20) * 17, -70 + (i // 20) * 14
        features.append(feature(code, square(lon, lat, 0.5 if code in TINY else 4)))
    features.append(feature("-99", square(10, 75, 3), admin="Somaliland"))
    features.append(feature("-99", square(20, 75, 3), admin="Northern Cyprus"))
    features.append(feature("-99", square(30, 75, 3), admin="Siachen Glacier", kind="Indeterminate"))
    features.append(feature("AQ", square(-170, -89, 60), admin="Antarctica", kind="Indeterminate"))
    features.append(feature("AU", square(100, -15, 2), admin="Indian Ocean Territories", kind="Dependency"))
    features.append(feature("GL", square(-50, 65, 10), admin="Greenland", kind="Country"))
    return features


def encode(features):
    return json.dumps({"type": "FeatureCollection", "features": features}).encode("utf-8")


class BuildTestCase(unittest.TestCase):
    def build(self, features=None, data=None, merge=None):
        """The built map; self.offset is its Y shift from to_canvas()."""
        data = data if data is not None else encode(features if features is not None else base_features())
        result, self.offset = build_map.build_with_offset(data, hashlib.sha256(data).hexdigest(), merge)
        return result

    def at(self, lon, lat):
        """Where (lon, lat) lands on the last built map."""
        return on_map(lon, lat, self.offset)

    def render(self, features=None, merge=None):
        return build_map.render(self.build(features, merge=merge))

    def assertRefused(self, data, message):
        with self.assertRaises(build_map.MapError) as ctx:
            build_map.build(data, hashlib.sha256(data).hexdigest())
        self.assertIn(message, str(ctx.exception))


class ProjectionTests(unittest.TestCase):
    def test_against_proj_vectors(self):
        """Ratios from PROJ's eqearth test vectors (contract 2.2)."""
        x180, _ = build_map.equal_earth(180, 0)
        x180n, ymax = build_map.equal_earth(180, 90)
        self.assertAlmostEqual(build_map.equal_earth(0, 90)[1], 1.3173627591574, places=10)
        self.assertAlmostEqual(ymax, 1.3173627591574, places=10)
        self.assertAlmostEqual(x180 / ymax, 2.054582130, places=8)
        self.assertAlmostEqual(x180n / ymax, 1.217271884, places=8)

    def test_canvas(self):
        self.assertEqual(build_map.WIDTH, 1000)
        self.assertAlmostEqual(build_map.HEIGHT_EXACT, 486.7, delta=0.05)
        self.assertAlmostEqual(build_map.SCALE, 184.7, places=1)
        for (lon, lat), (x, y) in {(-180, 0): (0, 243.35), (180, 0): (1000, 243.35), (0, 0): (500, 243.35),
                                   (0, 90): (500, 0), (0, -90): (500, 486.7)}.items():
            px, py = build_map.to_canvas(lon, lat)
            self.assertAlmostEqual(px, x, places=1)
            self.assertAlmostEqual(py, y, delta=0.05)
        self.assertLess(build_map.to_canvas(10, 50)[1], build_map.to_canvas(10, 40)[1])  # y down

    def test_douglas_peucker(self):
        line = [(0, 0), (1, 0.05), (2, -0.05), (3, 0.1), (4, 0)]
        self.assertEqual(build_map.douglas_peucker(line, 0.2), [(0, 0), (4, 0)])
        bump = [(0, 0), (1, 0), (2, 5), (3, 0), (4, 0)]
        self.assertEqual(build_map.douglas_peucker(bump, 0.2), [(0, 0), (1, 0), (2, 5), (3, 0), (4, 0)])
        self.assertEqual(build_map.douglas_peucker(bump, 10), [(0, 0), (4, 0)])
        self.assertEqual(build_map.douglas_peucker([(1, 1), (2, 2)], 0.2), [(1, 1), (2, 2)])
        ring = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
        simplified = build_map.douglas_peucker(ring, 0.2)
        self.assertEqual(simplified[0], (0, 0))
        self.assertEqual(simplified[-1], (0, 0))
        self.assertEqual(len(simplified), 5)

    def test_number_format(self):
        self.assertEqual(build_map.fmt(-0.04), "0.0")
        self.assertEqual(build_map.fmt(1e-9), "0.0")
        self.assertEqual(build_map.fmt(999.96), "1000.0")
        self.assertEqual(build_map.fmt(12.25), "12.2")


class BuildTests(BuildTestCase):
    def test_happy_path(self):
        result = self.build()
        self.assertEqual(set(result), TOP_KEYS)
        self.assertEqual(set(result["countries"]), set(countries.BY_CODE))
        body = build_map.render(result)
        parsed = strict_json(body)
        self.assertEqual(parsed, result)
        for path in list(parsed["countries"].values()) + parsed["other"]:
            self.assertTrue(PATH_SHAPE_RE.fullmatch(path))

    def test_deterministic(self):
        first, second = self.render(), self.render()
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"}\n"))
        self.assertNotIn(b" ", first.replace(build_map.ATTRIBUTION.encode("ascii"), b""))
        self.assertEqual(first, (json.dumps(strict_json(first), sort_keys=True, separators=(",", ":"))
                                 + "\n").encode("ascii"))

    def test_dependency_is_other_land(self):
        with_dep = self.build()
        features = [f for f in base_features() if f["properties"]["TYPE"] != "Dependency"]
        without = self.build(features)
        self.assertEqual(with_dep["countries"]["AU"], without["countries"]["AU"])
        self.assertEqual(len(with_dep["other"]), len(without["other"]) + 1)

    def test_dependency_alone_does_not_count(self):
        features = [f for f in base_features() if not (f["properties"]["ISO_A2_EH"] == "AU"
                                                        and f["properties"]["TYPE"] != "Dependency")]
        self.assertRaisesRegex(build_map.MapError, "no feature for AU", self.build, features)

    def test_antarctica_dropped(self):
        result = self.build()
        features = [f for f in base_features() if f["properties"]["ISO_A2_EH"] != "AQ"]
        self.assertEqual(result, self.build(features))
        # The bottom of the map is the lowest synthetic square (lat -70), not Antarctica.
        self.assertAlmostEqual(result["height"] - 2, self.at(0, -70)[1], delta=0.1)

    def test_missing_country_fails(self):
        features = [f for f in base_features() if f["properties"]["ISO_A2_EH"] != "PE"]
        self.assertRaisesRegex(build_map.MapError, "no feature for PE", self.build, features)

    def test_duplicate_country_fails(self):
        features = base_features() + [feature("PE", square(0, 0, 2))]
        self.assertRaisesRegex(build_map.MapError, "PE matches more than one", self.build, features)

    def test_unknown_codes_are_other(self):
        result = self.build()
        self.assertEqual(len(result["other"]), 2)  # the AU dependency and Greenland
        self.assertNotIn("GL", result["countries"])

    def test_no_input_strings_in_output(self):
        evil = "<script>alert(1)</script>"
        features = base_features()
        for f in features:
            f["properties"]["NAME"] = evil
            f["properties"]["ADMIN"] = f["properties"]["ADMIN"] if f["properties"]["ADMIN"] in MERGE_NAMES else evil
        features[0]["properties"]["NAME_EN"] = evil
        features.append(feature("ZZ", square(40, 40, 3), admin=evil, name=evil))
        body = self.render(features)
        for needle in (b"script", b"<", b"Land", b"Somaliland", b"Sovereign"):
            self.assertNotIn(needle, body)
        self.assertEqual(strict_json(body)["attribution"],
                         "Made with Natural Earth (1:50m Admin 0, v5.1.1). Public domain.")

    def test_dots(self):
        result = self.build()
        self.assertEqual(set(result["dots"]), TINY)
        x, y = result["dots"]["AD"]
        i = sorted(countries.BY_CODE).index("AD")
        cx, cy = self.at(-170 + (i % 20) * 17 + 0.25, -70 + (i // 20) * 14 + 0.25)
        self.assertAlmostEqual(x, cx, delta=0.1)
        self.assertAlmostEqual(y, cy, delta=0.1)

    def test_dot_uses_largest_ring(self):
        features = base_features()
        i = sorted(countries.BY_CODE).index("MC")
        lon, lat = -170 + (i % 20) * 17, -70 + (i // 20) * 14
        rings = [square(lon, lat, 0.2), square(lon + 1, lat, 0.6)]
        features[i] = feature("MC", rings, multi=True)
        x, _ = self.build(features)["dots"]["MC"]
        self.assertAlmostEqual(x, self.at(lon + 1.3, lat + 0.3)[0], delta=0.1)

    def test_dot_for_spread_out_islands(self):
        """Tiny islands far apart still get a dot; one tappable ring anywhere means no dot."""
        features = base_features()
        i = sorted(countries.BY_CODE).index("KI")
        lon, lat = -170 + (i % 20) * 17, -70 + (i // 20) * 14
        islands = [square(lon, lat, 0.5), square(lon + 12, lat + 10, 0.8)]
        features[i] = feature("KI", islands, multi=True)
        result = self.build(features)
        self.assertEqual(result["countries"]["KI"].count("M"), 2)
        x, y = result["dots"]["KI"]  # on the larger island
        cx, cy = self.at(lon + 12.4, lat + 10.4)
        self.assertAlmostEqual(x, cx, delta=0.15)
        self.assertAlmostEqual(y, cy, delta=0.15)
        features[i] = feature("KI", islands + [square(lon + 4, lat, 4)], multi=True)
        self.assertNotIn("KI", self.build(features)["dots"])

    def test_dot_measured_on_drawn_rings(self):
        """A ring dropped by simplification doesn't count; only what is drawn does."""
        features = base_features()
        i = sorted(countries.BY_CODE).index("PW")
        lon, lat = -170 + (i % 20) * 17, -70 + (i // 20) * 14
        sliver = [[lon + 3, lat], [lon + 9, lat], [lon + 9, lat + 0.0001], [lon + 3, lat]]  # 6 degrees long, dropped
        features[i] = feature("PW", [square(lon, lat, 0.5), sliver], multi=True)
        result = self.build(features)
        self.assertEqual(result["countries"]["PW"].count("M"), 1)
        self.assertIn("PW", result["dots"])

    def test_tiny_country_keeps_a_ring(self):
        """A country whose rings all vanish in simplification keeps its largest ring unsimplified."""
        features = base_features()
        i = sorted(countries.BY_CODE).index("VA")
        lon, lat = -170 + (i % 20) * 17, -70 + (i // 20) * 14
        features[i] = feature("VA", [square(lon, lat, 0.001), square(lon + 1, lat, 0.002)], multi=True)
        result = self.build(features)
        path = result["countries"]["VA"]
        self.assertTrue(PATH_SHAPE_RE.fullmatch(path))
        self.assertEqual(path.count("M"), 1)
        self.assertEqual(path.count("L"), 3)  # 5 points, closing point removed
        self.assertIn("VA", result["dots"])

    def test_tiny_other_land_dropped(self):
        features = base_features() + [feature("ZZ", square(0, 80, 0.001))]
        self.assertEqual(len(self.build(features)["other"]), 2)

    def test_multipolygon_and_holes(self):
        features = base_features()
        i = sorted(countries.BY_CODE).index("BR")
        lon, lat = -170 + (i % 20) * 17, -70 + (i // 20) * 14
        features[i]["geometry"]["coordinates"].append(square(lon + 1, lat + 1, 2))  # a hole
        path = self.build(features)["countries"]["BR"]
        self.assertEqual(path.count("M"), 2)
        self.assertTrue(point_in_path(path, self.at(lon + 0.5, lat + 0.5)))  # land
        self.assertEqual(rings_containing(path, self.at(lon + 2, lat + 2)), 2)
        self.assertFalse(point_in_path(path, self.at(lon + 2, lat + 2)))  # the hole stays unfilled


class MergeTests(BuildTestCase):
    def test_merged_into_country(self):
        merged = self.build()
        kept = self.build(merge={})
        for code in ("SO", "CY", "IN"):
            self.assertEqual(merged["countries"][code].count("M"), 2)
            self.assertTrue(merged["countries"][code].startswith(kept["countries"][code]))
        self.assertEqual(len(kept["other"]), len(merged["other"]) + 3)
        point = self.at(11, 76)  # inside the synthetic "Somaliland"
        self.assertTrue(point_in_path(merged["countries"]["SO"], point))
        self.assertFalse(any(point_in_path(p, point) for p in merged["other"]))
        self.assertTrue(any(point_in_path(p, point) for p in kept["other"]))

    def test_default_merge_is_the_users_choice(self):
        self.assertEqual(build_map.MERGE, MERGE_NAMES)

    def test_merge_name_missing_fails(self):
        features = [f for f in base_features() if f["properties"]["ADMIN"] != "Northern Cyprus"]
        self.assertRaisesRegex(build_map.MapError, "Northern Cyprus", self.build, features)

    def test_merge_name_twice_fails(self):
        features = base_features() + [feature("-99", square(40, 75, 2), admin="Siachen Glacier")]
        self.assertRaisesRegex(build_map.MapError, "Siachen Glacier", self.build, features)

    def test_merge_name_must_match_exactly(self):
        features = base_features()
        for f in features:
            if f["properties"]["ADMIN"] == "Somaliland":
                f["properties"]["ADMIN"] = "somaliland"
        self.assertRaisesRegex(build_map.MapError, "Somaliland", self.build, features)


class SourceCheckTests(BuildTestCase):
    def test_hash_mismatch_refused_before_parsing(self):
        data = encode(base_features())
        with mock.patch.object(build_map.json, "loads", side_effect=AssertionError("parsed")) as loads:
            with self.assertRaisesRegex(build_map.MapError, "SHA-256"):
                build_map.build(data, "0" * 64)
            with self.assertRaisesRegex(build_map.MapError, "SHA-256"):
                build_map.build(data)  # the real pinned hash
        loads.assert_not_called()

    def test_hash_match_is_parsed(self):
        data = encode(base_features())
        with mock.patch.object(build_map.json, "loads", wraps=json.loads) as loads:
            build_map.build(data, hashlib.sha256(data).hexdigest())
        loads.assert_called_once()

    def test_pinned_hash(self):
        self.assertEqual(build_map.SOURCE_SHA256,
                         "3e458fc036ad0a66411f2c1e6cac49c5d7bfb81cb1123bc513b22511a2b7fdeb")
        self.assertIn("9380cca83db5f9aef52d5e762765100745f84b27", build_map.__doc__)
        self.assertIn(build_map.SOURCE_SHA256, build_map.__doc__)

    def test_nan_and_infinity_refused(self):
        good = encode(base_features())
        target = b"[-170, -70]"
        self.assertIn(target, good)
        for literal in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(literal=literal):
                self.assertRefused(good.replace(target, b"[" + literal + b", -70]", 1), "NaN or Infinity")
        # Even in properties nobody reads.
        features = base_features()
        features[0]["properties"]["SCALERANK"] = float("nan")
        self.assertRefused(encode(features), "NaN or Infinity")

    def test_out_of_range_refused(self):
        for position in ([180.5, 0], [-180.01, 0], [0, 90.0001], [0, -91], [10 ** 400, 0], [0, 1e400]):
            with self.subTest(position=position):
                features = base_features()
                features[3]["geometry"]["coordinates"][0][1] = position
                data = encode(features) if position[1] != 1e400 else encode(features).replace(
                    b"Infinity", b"1e400")
                self.assertRefused(data, "feature 3")

    def test_range_limits_accepted(self):
        features = base_features()
        features.append(feature("ZZ", [[-180, -90], [180, -90], [180, 90], [-180, 90], [-180, -90]]))
        result = self.build(features)
        self.assertEqual(len(result["other"]), 3)
        build_map.check(result)
        self.assertAlmostEqual(build_map.to_canvas(-180, 0)[0], 0, places=9)
        self.assertAlmostEqual(build_map.to_canvas(180, 0)[0], 1000, places=9)
        self.assertAlmostEqual(result["height"], round(build_map.HEIGHT_EXACT, 1) + 4, delta=0.15)

    def test_malformed_refused(self):
        cases = {
            "string coordinate": lambda f: f["geometry"]["coordinates"][0].__setitem__(1, ["1", "2"]),
            "bool coordinate": lambda f: f["geometry"]["coordinates"][0].__setitem__(1, [True, 2]),
            "3 numbers": lambda f: f["geometry"]["coordinates"][0].__setitem__(1, [1, 2, 3]),
            "null coordinate": lambda f: f["geometry"]["coordinates"][0].__setitem__(1, None),
            "empty ring": lambda f: f["geometry"]["coordinates"].__setitem__(0, []),
            "no rings": lambda f: f["geometry"].__setitem__("coordinates", []),
            "point": lambda f: f["geometry"].__setitem__("type", "Point"),
            "no geometry": lambda f: f.__setitem__("geometry", None),
            "no properties": lambda f: f.__setitem__("properties", None),
        }
        for label, change in cases.items():
            with self.subTest(label):
                features = base_features()
                change(features[5])
                self.assertRefused(encode(features), "feature 5")

    def test_not_json_or_wrong_shape(self):
        for data in (b"", b"{", b"[]", b'{"features": {}}', b'{"features": [1]}', b"[" * 100000):
            with self.subTest(data=data[:20]):
                self.assertRaises(build_map.MapError, build_map.build, data, hashlib.sha256(data).hexdigest())

    def test_non_string_properties_ignored(self):
        features = base_features()
        features.append(feature(["PE"], square(0, 0, 2)))
        features.append(feature({"x": 1}, square(5, 0, 2)))
        self.assertEqual(len(self.build(features)["other"]), 4)


class CheckAndWriteTests(BuildTestCase):
    def test_check_rejects_bad_maps(self):
        good = self.build()
        changes = [
            lambda m: m.__setitem__("extra", 1),
            lambda m: m.pop("other"),
            lambda m: m.__setitem__("attribution", "x"),
            lambda m: m["countries"].pop("PE"),
            lambda m: m["countries"].__setitem__("ZZ", "M1.0,1.0L2.0,2.0L3.0,1.0Z"),
            lambda m: m["countries"].__setitem__("PE", ""),
            lambda m: m["countries"].__setitem__("PE", "M1.0,1.0L2.0,2.0L3.0,1.0Z\"><script>"),
            lambda m: m["countries"].__setitem__("PE", "M1.0,1.0L2.0,2.0L3.0,1e5Z"),
            lambda m: m["countries"].__setitem__("PE", "M1.0,1.0L2.0,2.0L3.0Z"),
            lambda m: m["countries"].__setitem__("PE", "M1.0,1.0L2.0,2.0L1001.5,1.0Z"),
            lambda m: m["countries"].__setitem__("PE", "M1.0,-1.5L2.0,2.0L3.0,1.0Z"),
            lambda m: m["other"].append("javascript:alert(1)"),
            lambda m: m["other"].append("M1.0,1.0L2.0,487.8L3.0,1.0Z"),
            lambda m: m["dots"].__setitem__("ZZ", [1.0, 1.0]),
            lambda m: m["dots"].__setitem__("PE", [1.0, 488.0]),
            lambda m: m["other"].append("M1.0,1.5L2.0,2.0L3.0,1.5Z"),  # above the 2-unit margin
            lambda m: m.__setitem__("height", m["height"] + 0.5),  # bottom margin no longer 2
            lambda m: m.__setitem__("height", 0.0),
            lambda m: m.__setitem__("height", float("inf")),
            lambda m: m.__setitem__("height", 20000.0),
            lambda m: m.__setitem__("height", str(m["height"])),
            lambda m: m["dots"].__setitem__("PE", [float("nan"), 1.0]),
            lambda m: m["dots"].__setitem__("PE", ["1", "2"]),
        ]
        build_map.check(copy.deepcopy(good))
        for i, change in enumerate(changes):
            with self.subTest(i=i):
                bad = copy.deepcopy(good)
                change(bad)
                self.assertRaises(build_map.MapError, build_map.check, bad)

    def test_write_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp, "in.geojson"), Path(tmp, "map.json")
            data = encode(base_features())
            source.write_bytes(data)
            body, height, offset = build_map.write_map(source, out, hashlib.sha256(data).hexdigest())
            self.assertEqual(strict_json(body)["height"], height)
            self.assertIsInstance(offset, float)
            self.assertEqual(out.read_bytes(), body)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["in.geojson", "map.json"])

    def test_nothing_written_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp, "in.geojson"), Path(tmp, "map.json")
            data = encode([f for f in base_features() if f["properties"]["ISO_A2_EH"] != "PE"])
            source.write_bytes(data)
            self.assertRaises(build_map.MapError, build_map.write_map, source, out,
                              hashlib.sha256(data).hexdigest())
            self.assertRaises(build_map.MapError, build_map.write_map, source, out)  # wrong hash
            out.write_bytes(b"previous")
            self.assertRaises(build_map.MapError, build_map.write_map, source, out)
            self.assertEqual(out.read_bytes(), b"previous")
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["in.geojson", "map.json"])

    def test_main_usage_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stderr"), mock.patch("sys.stdout"):
            self.assertEqual(build_map.main(["build_map.py"]), 2)
            source = Path(tmp, "in.geojson")
            source.write_bytes(b"{}")
            self.assertEqual(build_map.main(["build_map.py", str(source), str(Path(tmp, "map.json"))]), 1)
            self.assertEqual(build_map.main(["build_map.py", str(Path(tmp, "nope")), str(Path(tmp, "m"))]), 1)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["in.geojson"])

    def test_script_sets_up_imports_safely(self):
        source = SCRIPT.read_text()
        self.assertIn("sys.dont_write_bytecode = True", source)
        self.assertIn("sys.path.append(", source)
        self.assertNotIn("sys.path.insert(", source)
        self.assertNotRegex(source, r"\b(urllib|http\.client|socket|subprocess)\b")


if __name__ == "__main__":
    unittest.main()
