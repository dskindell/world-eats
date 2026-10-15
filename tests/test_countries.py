"""Feature 3: the country list (contract .claude/specs/3-countries-meals.md, sections 2 and 6)."""

import json
import re
import unicodedata
import unittest
from collections import Counter
from pathlib import Path

from tests.helpers import ServerTestCase
from tests.test_passports import PENDING

import countries  # noqa: E402  (app/ is on sys.path via tests.helpers)

SPEC_JSON = Path(__file__).resolve().parent.parent / ".claude" / "specs" / "3-countries.json"
REGIONS = ["Africa", "Asia", "Europe", "North America", "Oceania", "South America"]


def sort_key(name):
    """Contract §2: accents removed, case-folded."""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


class CountryDataTests(unittest.TestCase):
    def test_count_and_uniqueness(self):
        self.assertEqual(len(countries.COUNTRIES), 197)
        self.assertEqual(len({c.code for c in countries.COUNTRIES}), 197)
        self.assertEqual(len({c.name for c in countries.COUNTRIES}), 197)
        self.assertEqual(len(countries.BY_CODE), 197)
        for c in countries.COUNTRIES:
            self.assertIs(countries.BY_CODE[c.code], c)

    def test_codes_and_regions(self):
        self.assertEqual(list(countries.REGIONS), REGIONS)
        for c in countries.COUNTRIES:
            with self.subTest(code=c.code):
                self.assertIsNotNone(re.fullmatch(r"[A-Z]{2}", c.code))
                self.assertIn(c.region, REGIONS)
                self.assertIsInstance(c.aliases, tuple)
                self.assertTrue(all(isinstance(a, str) and a for a in c.aliases))
        self.assertEqual(Counter(c.region for c in countries.COUNTRIES),
                         {"Africa": 54, "Asia": 49, "Europe": 45, "North America": 23, "Oceania": 14,
                          "South America": 12})

    def test_order(self):
        names = [c.name for c in countries.COUNTRIES]
        self.assertEqual(names, sorted(names, key=sort_key))
        codes = [c.code for c in countries.COUNTRIES]
        # "Côte d'Ivoire" sorts under C, between Costa Rica and Croatia
        self.assertEqual(codes[codes.index("CR") + 1:codes.index("HR")], ["CI"])

    def test_spot_checks(self):
        expected = {"RU": "Europe", "TR": "Asia", "GE": "Asia", "AM": "Asia", "AZ": "Asia", "CY": "Asia",
                    "KZ": "Asia", "EG": "Africa", "MX": "North America", "PA": "North America",
                    "TT": "North America", "GY": "South America", "SR": "South America", "TW": "Asia",
                    "PS": "Asia", "VA": "Europe", "XK": "Europe"}
        for code, region in expected.items():
            with self.subTest(code=code):
                self.assertEqual(countries.BY_CODE[code].region, region)
        self.assertEqual(countries.BY_CODE["CI"].name, "Côte d'Ivoire")
        self.assertEqual(countries.BY_CODE["PE"].name, "Peru")

    @unittest.skipUnless(SPEC_JSON.exists(), "spec JSON not present (it isn't in the repository)")
    def test_matches_spec_json(self):
        rows = json.loads(SPEC_JSON.read_text(encoding="utf-8"))
        self.assertEqual([(c.code, c.name, c.region, list(c.aliases)) for c in countries.COUNTRIES],
                         [(r["code"], r["name"], r["region"], r["aliases"]) for r in rows])


class CountriesEndpointTests(ServerTestCase):
    def test_list(self):
        token = self.setup_admin()
        resp = self.request("GET", "/api/countries", token=token)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        body = resp.json()
        self.assertEqual(set(body), {"regions", "countries"})
        self.assertEqual(body["regions"], REGIONS)
        self.assertEqual(len(body["countries"]), 197)
        for item, country in zip(body["countries"], countries.COUNTRIES):
            self.assertEqual(item, {"code": country.code, "name": country.name, "region": country.region,
                                    "aliases": list(country.aliases)})
        # UTF-8, not \u escapes (contract §6, review L3)
        self.assertIn("Côte d'Ivoire".encode("utf-8"), resp.body)
        self.assertNotIn(b"\\u", resp.body)

    def test_logged_out(self):
        self.setup_admin()
        resp = self.request("GET", "/api/countries")
        self.assertEqual((resp.status, resp.error), (401, "Not logged in"))

    def test_pending(self):
        token = self.setup_admin()
        with self.db() as conn:
            conn.execute("UPDATE users SET must_change_password = 1")
        resp = self.request("GET", "/api/countries", token=token)
        self.assertEqual((resp.status, resp.json()), (403, PENDING))

    def test_methods(self):
        token = self.setup_admin()
        self.assertEqual(self.request("POST", "/api/countries", {}, token=token).status, 405)
        resp = self.request("HEAD", "/api/countries", token=token)
        self.assertEqual((resp.status, resp.body), (200, b""))
