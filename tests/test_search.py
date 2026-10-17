"""Feature 4: search and done_count."""

import json
import sqlite3
import time
import unittest
from unittest import mock

from tests.helpers import PASSWORD, ServerTestCase, db, fast_hasher, server, sha256_hex
from tests.test_meals import DATE, SUMMARY_KEYS, MealTestCase
from tests.test_passports import NOT_FOUND, PENDING, ref

import countries  # noqa: E402  (app/ is on sys.path via tests.helpers)
import search  # noqa: E402

MATCH_KEYS = {"code", "meal"}
HINT_KEYS = {"dish", "place", "notes_excerpt"}
EMPTY = "Type something to search"
TOO_LONG = "Search can be up to 200 characters"
WORD_TOO_LONG = "Search words can be up to 50 characters"
BUSY = "Server busy, try again"
ORDER = {c.code: i for i, c in enumerate(countries.COUNTRIES)}


# --- fold() (§3.1) ---

class FoldTests(unittest.TestCase):
    def test_examples(self):
        for text, expected in (
                ("PERU", "peru"), ("Côte d'Ivoire", "cote d'ivoire"), ("Crème Brûlée", "creme brulee"),
                ("Straße", "strasse"), ("ＣＥＶＩＣＨＥ", "ceviche"), ("ﬁsh", "fish"), ("İstanbul", "istanbul"),
                ("Smørrebrød", "smorrebrod"), ("Łódź", "lodz"), ("ØŁĐÐÞÆŒ", "oldd" + "th" + "ae" + "oe"),
                ("øłđðþæœı", "oldd" + "th" + "ae" + "oe" + "i"), ("Türkiye", "turkiye"),
                ("  a \t\n　 b  ", "a b"), ("́̈", ""), ("", ""), ("100% a_b \\", "100% a_b \\")):
            with self.subTest(text=text):
                self.assertEqual(search.fold(text), expected)

    def test_largest_expansion(self):
        self.assertEqual(len(search.fold("ﷺ")), 18)

    def test_names_folded_at_import(self):
        self.assertEqual([code for code, _ in search.FOLDED_NAMES], [c.code for c in countries.COUNTRIES])
        names = dict(search.FOLDED_NAMES)
        self.assertEqual(names["CI"], ("cote d'ivoire", "ivory coast"))
        self.assertEqual(names["TR"], ("turkiye", "turkey"))
        self.assertIn("sao tome and principe", names["ST"])
        self.assertEqual(names["MM"], ("myanmar", "burma"))
        for code, folded in search.FOLDED_NAMES:
            country = countries.BY_CODE[code]
            self.assertEqual(folded, tuple(search.fold(n) for n in (country.name, *country.aliases)))

    def test_meal_search_text_caps(self):
        self.assertEqual(search.meal_search_text("Crème", "La Mar", "line one\nline  two"),
                         "creme\nla mar\nline one line two")
        self.assertEqual(search.meal_search_text("", "", ""), "\n\n")
        text = search.meal_search_text("ﷺ" * 100, "ﷺ" * 100, "ﷺ" * 2000)
        dish, place, notes = text.split("\n")
        self.assertEqual((len(dish), len(place), len(notes)), (200, 200, 4000))


# --- Shared fixtures ---

class SearchTestCase(MealTestCase):
    """Admin (owner), bob (participant), sam (viewer), eve (outsider); passport "Trip" (self.pid)."""

    def path(self, pid):
        return f"/api/passports/{pid}/search"

    def search(self, pid, q, token="default", **kwargs):
        return self.api("POST", self.path(pid), {"q": q}, token=token, **kwargs)

    def matches(self, pid, q, token="default"):
        resp = self.search(pid, q, token)
        self.assertEqual(resp.status, 200, resp)
        body = resp.json()
        self.assertEqual(set(body), {"matches"})
        for match in body["matches"]:
            self.assertEqual(set(match), MATCH_KEYS)
            if match["meal"] is not None:
                self.assertEqual(set(match["meal"]), HINT_KEYS)
        codes = [m["code"] for m in body["matches"]]
        self.assertEqual(codes, sorted(codes, key=ORDER.get), "COUNTRIES order")
        return body["matches"]

    def codes(self, pid, q, token="default"):
        return [m["code"] for m in self.matches(pid, q, token)]

    def hint(self, pid, q, code, token="default"):
        return next(m["meal"] for m in self.matches(pid, q, token) if m["code"] == code)

    def insert_meals(self, pid, user_id, rows):
        """Rows of (code, eaten_on, dish, place, notes), with search_text as the app computes it."""
        texts = {}
        for _, _, dish, place, notes in rows:
            if (dish, place, notes) not in texts:
                texts[dish, place, notes] = search.meal_search_text(dish, place, notes)
        with self.db() as conn:
            conn.executemany(
                "INSERT INTO meals (passport_id, user_id, country_code, eaten_on, dish, place, notes, search_text, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1)",
                [(pid, user_id, code, eaten_on, dish, place, notes, texts[dish, place, notes])
                 for code, eaten_on, dish, place, notes in rows])


# --- Access ---

class AccessTests(SearchTestCase):
    def setUp(self):
        super().setUp()
        self.meal_id(self.pid, "PE", dish="Ceviche")

    def test_members_can_search(self):
        for token in (self.token, self.bob_token, self.sam_token):
            self.assertEqual(self.codes(self.pid, "ceviche", token), ["PE"])

    def test_non_member_same_as_missing(self):
        missing = self.search(99999, "ceviche", token=self.eve_token)
        self.assertEqual((missing.status, missing.json()), (404, NOT_FOUND))
        for pid in (self.pid, self.personal):
            resp = self.search(pid, "ceviche", token=self.eve_token)
            self.assertEqual((resp.status, resp.body), (404, missing.body), "byte-identical")
        # an admin gets no access either
        bob_personal = self.personal_id(self.bob)
        resp = self.search(bob_personal, "peru")
        self.assertEqual((resp.status, resp.body), (404, missing.body))

    def test_logged_out(self):
        resp = self.search(self.pid, "ceviche", token=None)
        self.assertEqual((resp.status, resp.error), (401, "Not logged in"))

    def test_pending(self):
        with self.db() as conn:
            conn.execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (self.bob,))
        resp = self.search(self.pid, "ceviche", token=self.bob_token)
        self.assertEqual((resp.status, resp.json()), (403, PENDING))

    def test_cross_site(self):
        resp = self.search(self.pid, "ceviche", content_type="text/plain")
        self.assertEqual((resp.status, resp.error), (415, "Content-Type must be application/json"))
        resp = self.search(self.pid, "ceviche", origin="http://evil.example")
        self.assertEqual((resp.status, resp.error), (403, "Cross-origin request blocked"))
        resp = self.search(self.pid, "ceviche", origin=None)
        self.assertEqual((resp.status, resp.error), (403, "Cross-origin request blocked"))

    def test_wrong_method(self):
        for method in ("GET", "HEAD", "PATCH"):
            with self.subTest(method=method):
                self.assertEqual(self.api(method, self.path(self.pid)).status, 405)

    def test_bad_ids(self):
        for bad in ("0", "01", "abc", "12345678901"):
            with self.subTest(bad=bad):
                resp = self.api("POST", f"/api/passports/{bad}/search", {"q": "peru"})
                self.assertEqual((resp.status, resp.error), (404, "Not found"))

    def test_validated_before_the_database(self):
        # A bad query is a 400 even where the passport would be a 404.
        for pid in (self.pid, 99999):
            resp = self.search(pid, 5, token=self.eve_token)
            self.assertEqual((resp.status, resp.error), (400, EMPTY))


# --- Validation (§4) ---

class ValidationTests(SearchTestCase):
    def assert_error(self, q, message):
        resp = self.search(self.pid, q)
        self.assertEqual((resp.status, resp.json()), (400, {"error": message}), repr(q)[:80])

    def test_not_a_string(self):
        for q in (5, 1.5, [], ["peru"], None, True, False, {}, {"q": "peru"}):
            with self.subTest(q=q):
                self.assert_error(q, EMPTY)
        resp = self.api("POST", self.path(self.pid), {})
        self.assertEqual((resp.status, resp.error), (400, EMPTY), "q missing")

    def test_empty_after_folding(self):
        for q in ("", " ", " \t\n ", "　", "́̈", " ́ "):
            with self.subTest(q=q):
                self.assert_error(q, EMPTY)

    def test_surrogate(self):
        for raw in (b'{"q": "\\ud800"}', b'{"q": "peru \\udfff"}'):
            with self.subTest(raw=raw):
                resp = self.api("POST", self.path(self.pid), raw_body=raw)
                self.assertEqual((resp.status, resp.error), (400, EMPTY))

    def test_raw_length(self):
        exactly = ("x" * 49 + " ") * 4  # 200 code points, four 49-character words
        self.assertEqual(len(exactly), 200)
        self.assertEqual(self.search(self.pid, exactly).status, 200)
        self.assert_error(exactly + "x", TOO_LONG)
        self.assert_error("x" * 201, TOO_LONG)
        self.assert_error(" " * 201, TOO_LONG)
        self.assert_error("\U0001F35C" * 201, TOO_LONG)  # code points, not UTF-16 units or bytes
        self.assertEqual(self.search(self.pid, "\U0001F35C" * 50).status, 200)

    def test_folded_length(self):
        q = "ﷺ" * 12  # 12 code points that fold to 216
        self.assertEqual(len(search.fold(q)), 216)
        self.assert_error(q, TOO_LONG)
        self.assertEqual(self.search(self.pid, "ﷺ" * 11).status, 200, "11 fold to 198")

    def test_word_length(self):
        self.assertEqual(self.search(self.pid, "x" * 50).status, 200)
        self.assert_error("x" * 51, WORD_TOO_LONG)
        self.assert_error("ß" * 26, WORD_TOO_LONG)  # 26 code points fold to 52
        self.assertEqual(self.search(self.pid, "ß" * 25).status, 200)
        self.assert_error("peru " + "x" * 51, WORD_TOO_LONG)

    def test_only_first_ten_terms(self):
        words = [f"w{i}" for i in range(1, 11)]
        self.meal_id(self.pid, "PE", notes=" ".join(words))
        self.assertEqual(self.codes(self.pid, " ".join(words)), ["PE"])
        self.assertEqual(self.codes(self.pid, " ".join(words + ["zzz"])), ["PE"], "the 11th is ignored")
        self.assertEqual(self.codes(self.pid, " ".join(words[:9] + ["zzz"])), [])
        # an 11th word over 50 characters is dropped before the length check
        self.assertEqual(self.codes(self.pid, " ".join(words + ["x" * 60])), ["PE"])

    def test_duplicates_collapsed(self):
        self.meal_id(self.pid, "PE", dish="Ceviche")
        # 11 copies of one word (in different forms) plus "zzz": deduplicated first, so zzz is a term
        q = "peru PERU Peru " * 3 + "peru pérû zzz"
        self.assertEqual(self.codes(self.pid, q), [])
        self.assertEqual(self.codes(self.pid, "peru PERU Pérú"), ["PE"])

    def test_extra_keys_ignored(self):
        resp = self.api("POST", self.path(self.pid), {"q": "peru", "passport_id": 99999, "terms": ["x"],
                                                      "limit": 0})
        self.assertEqual(resp.status, 200)
        self.assertEqual([m["code"] for m in resp.json()["matches"]], ["PE"])


# --- Matching (§4) ---

class MatchingTests(SearchTestCase):
    def test_name(self):
        for q in ("peru", "PERU", "Perú", "  peru  "):
            with self.subTest(q=q):
                self.assertEqual(self.matches(self.pid, q), [{"code": "PE", "meal": None}])
        self.assertEqual(self.codes(self.pid, "zzzz"), [])

    def test_alias(self):
        self.assertEqual(self.codes(self.pid, "burma"), ["MM"])
        self.assertEqual(self.codes(self.pid, "ivory coast"), ["CI"])
        self.assertEqual(self.codes(self.pid, "Ivory"), ["CI"])
        self.assertEqual(self.codes(self.pid, "turkey"), ["TR"])
        self.assertEqual(self.codes(self.pid, "Türkiye"), ["TR"])
        self.assertEqual(self.codes(self.pid, "cote"), ["CI"])
        self.assertEqual(self.codes(self.pid, "Sao Tomé"), ["ST"])

    def test_partial_word(self):
        self.assertIn("PE", self.codes(self.pid, "er"))
        self.assertEqual(self.codes(self.pid, "zealand"), ["NZ"])
        self.meal_id(self.pid, "JP", dish="Tonkotsu ramen")
        self.assertEqual(self.codes(self.pid, "kotsu"), ["JP"])

    def test_accents_both_ways(self):
        self.meal_id(self.pid, "FR", dish="Crème brûlée")
        self.meal_id(self.pid, "ES", dish="creme catalana")
        self.assertEqual(self.codes(self.pid, "creme"), ["FR", "ES"])  # France sorts before Spain
        self.assertEqual(self.codes(self.pid, "Crème"), ["FR", "ES"])
        self.assertEqual(self.codes(self.pid, "BRÛLÉE"), ["FR"])
        self.assertEqual(self.codes(self.pid, "catalána"), ["ES"])

    def test_multi_term_across_name_and_meal(self):
        self.meal_id(self.pid, "PE", dish="Ceviche")
        self.meal_id(self.pid, "EC", dish="Ceviche de camarón")
        self.assertEqual(self.codes(self.pid, "ceviche"), ["EC", "PE"])
        self.assertEqual(self.codes(self.pid, "peru ceviche"), ["PE"])
        self.assertEqual(self.codes(self.pid, "ceviche peru"), ["PE"])
        self.assertEqual(self.codes(self.pid, "peru camaron"), [])

    def test_terms_in_different_meals(self):
        self.meal_id(self.pid, "JP", dish="Ramen")
        self.meal_id(self.pid, "JP", self.bob_token, place="Sushi bar")
        self.meal_id(self.pid, "KR", dish="Ramen")
        self.assertEqual(self.codes(self.pid, "ramen sushi"), ["JP"])

    def test_other_passports_meals_dont_match(self):
        self.meal_id(self.personal, "PE", dish="Unicornfood")
        other = self.create_id("Other", [ref(self.bob, "bob")])
        self.meal_id(other, "JP", self.bob_token, dish="Unicornfood")
        self.assertEqual(self.codes(self.pid, "unicornfood"), [])
        self.assertEqual(self.codes(self.personal, "unicornfood"), ["PE"])
        self.assertEqual(self.codes(other, "unicornfood"), ["JP"])
        self.assertEqual(self.codes(self.pid, "peru unicornfood"), [])

    def test_like_wildcards_are_literal(self):
        self.meal_id(self.pid, "PE", dish="100% good")
        self.meal_id(self.pid, "JP", dish="axb")
        self.meal_id(self.pid, "FR", notes="back\\slash")
        self.assertEqual(self.codes(self.pid, "100%"), ["PE"])
        self.assertEqual(self.codes(self.pid, "%"), ["PE"])
        self.assertEqual(self.codes(self.pid, "a_b"), [])
        self.assertEqual(self.codes(self.pid, "_"), [])
        self.assertEqual(self.codes(self.pid, "k\\s"), ["FR"])
        self.assertEqual(self.codes(self.pid, "\\"), ["FR"])
        self.assertEqual(self.codes(self.pid, "'"), ["CI", "LA", "KP"], "names with an apostrophe")
        self.assertEqual(self.codes(self.pid, "' or 1=1 --"), [])

    def test_removed_participant(self):
        self.meal_id(self.pid, "PE", self.bob_token, dish="Bobsdish")
        self.assertEqual(self.codes(self.pid, "bobsdish"), ["PE"])
        self.assertEqual(self.remove(self.pid, self.bob).status, 200)
        self.assertEqual(self.codes(self.pid, "bobsdish"), [])

    def test_disabled_participant(self):
        self.meal_id(self.pid, "PE", self.bob_token, dish="Bobsdish")
        self.assertEqual(self.admin_action(self.bob, "disable").status, 200)
        self.assertEqual(self.codes(self.pid, "bobsdish"), ["PE"])
        self.assertEqual(self.codes(self.pid, "bobsdish", token=self.sam_token), ["PE"])

    def test_viewer_searches_everything(self):
        self.meal_id(self.pid, "PE", notes="secret sauce")
        self.assertEqual(self.matches(self.pid, "sauce", token=self.sam_token),
                         [{"code": "PE", "meal": {"dish": "", "place": "", "notes_excerpt": "secret sauce"}}])

    def test_many_matches_in_order(self):
        result = self.codes(self.pid, "a")
        self.assertGreater(len(result), 150)
        self.assertEqual(result, [c.code for c in countries.COUNTRIES if c.code in set(result)])


# --- Meal hints (§4, review M2) ---

class HintTests(SearchTestCase):
    def test_pure_name_match_has_no_meal(self):
        self.meal_id(self.pid, "PE", dish="Peruvian ceviche")
        self.assertEqual(self.matches(self.pid, "peru"), [{"code": "PE", "meal": None}])

    def test_meal_hint_shape(self):
        self.meal_id(self.pid, "PE", dish="Ceviche", place="La Mar", notes="Great\nreally")
        self.assertEqual(self.matches(self.pid, "peru ceviche"),
                         [{"code": "PE", "meal": {"dish": "Ceviche", "place": "La Mar", "notes_excerpt": None}}])
        self.assertEqual(self.hint(self.pid, "mar", "PE"),
                         {"dish": "Ceviche", "place": "La Mar", "notes_excerpt": None})

    def test_stored_strings_returned(self):
        self.meal_id(self.pid, "FR", dish="Crème Brûlée 🍮", place="Café <b>&</b>")
        self.assertEqual(self.hint(self.pid, "creme", "FR"),
                         {"dish": "Crème Brûlée 🍮", "place": "Café <b>&</b>", "notes_excerpt": None})

    def test_most_terms_then_newest(self):
        self.meal_id(self.pid, "PE", eaten_on="2026-10-01", dish="Ceviche", place="Lomo house")
        self.meal_id(self.pid, "PE", eaten_on="2026-10-10", dish="Ceviche")
        self.meal_id(self.pid, "PE", self.bob_token, eaten_on="2026-10-05", dish="Lomo saltado")
        self.assertEqual(self.hint(self.pid, "ceviche lomo", "PE")["place"], "Lomo house", "two terms win")
        self.assertEqual(self.hint(self.pid, "ceviche", "PE"),
                         {"dish": "Ceviche", "place": "", "notes_excerpt": None}, "newest of the ties")
        self.assertEqual(self.hint(self.pid, "lomo", "PE")["dish"], "Lomo saltado")
        # same date: the higher id wins
        self.meal_id(self.pid, "JP", eaten_on=DATE, dish="Ramen one")
        self.meal_id(self.pid, "JP", eaten_on=DATE, dish="Ramen two")
        self.assertEqual(self.hint(self.pid, "ramen", "JP")["dish"], "Ramen two")

    def test_name_terms_dont_count_for_the_hint(self):
        # "peru" matches the name, so only "ceviche" counts when picking the meal.
        self.meal_id(self.pid, "PE", eaten_on="2026-10-01", dish="Ceviche", notes="peru peru")
        self.meal_id(self.pid, "PE", eaten_on="2026-10-10", dish="Ceviche")
        hint = self.hint(self.pid, "peru ceviche", "PE")
        self.assertEqual((hint["dish"], hint["notes_excerpt"]), ("Ceviche", None))
        self.assertEqual(self.hint(self.pid, "peru ceviche", "PE"), self.hint(self.pid, "ceviche", "PE"))

    def test_notes_excerpt(self):
        notes = "Line one\nline two " + "x" * 80
        self.meal_id(self.pid, "PE", dish="Ceviche", place="La Mar", notes=notes)
        hint = self.hint(self.pid, "two", "PE")
        expected = notes[:80].replace("\n", " ") + "…"
        self.assertEqual(hint, {"dish": "Ceviche", "place": "La Mar", "notes_excerpt": expected})
        self.assertEqual(len(hint["notes_excerpt"]), 81)
        self.assertNotIn("\n", hint["notes_excerpt"])
        self.assertIsNone(self.hint(self.pid, "ceviche two", "PE")["notes_excerpt"], "dish matched too")

    def test_notes_excerpt_boundaries(self):
        exactly = "a\n" + "b" * 77 + "c"  # 80 code points
        self.meal_id(self.pid, "PE", notes=exactly)
        self.assertEqual(self.hint(self.pid, "bc", "PE")["notes_excerpt"], "a " + "b" * 77 + "c", "no ellipsis")
        self.meal_id(self.pid, "JP", notes="🍜" * 81)
        self.assertEqual(self.hint(self.pid, "🍜", "JP")["notes_excerpt"], "🍜" * 80 + "…")
        self.meal_id(self.pid, "FR", notes="Short")
        self.assertEqual(self.hint(self.pid, "short", "FR")["notes_excerpt"], "Short")

    def test_hint_never_from_another_passport(self):
        other = self.create_id("Other", [ref(self.bob, "bob")])
        self.meal_id(self.pid, "PE", eaten_on="2026-01-01", dish="Ceviche A")
        self.meal_id(other, "PE", eaten_on="2026-10-11", dish="Ceviche B")
        self.meal_id(other, "PE", self.bob_token, eaten_on="2026-10-11", dish="Ceviche B2")
        self.assertEqual(self.hint(self.pid, "ceviche", "PE")["dish"], "Ceviche A")
        self.assertEqual(self.hint(other, "ceviche", "PE")["dish"], "Ceviche B2")
        self.assertEqual(self.codes(self.pid, "ceviche b"), [])

    def test_hint_per_country(self):
        self.meal_id(self.pid, "PE", dish="Ceviche")
        self.meal_id(self.pid, "EC", place="Ceviche shack")
        result = {m["code"]: m["meal"] for m in self.matches(self.pid, "ceviche")}
        self.assertEqual(result, {"PE": {"dish": "Ceviche", "place": "", "notes_excerpt": None},
                                  "EC": {"dish": "", "place": "Ceviche shack", "notes_excerpt": None}})

    def test_query_counts_are_bounded(self):
        statements = []
        for code in [c.code for c in countries.COUNTRIES][:60]:
            self.insert_meals(self.pid, 1, [(code, DATE, "Dish", "Place", "alpha beta")] * 3)
        original = db.connect

        def connect(path):
            conn = original(path)
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(db, "connect", connect):
            self.assertEqual(len(self.codes(self.pid, "alpha beta gamma delta")), 0)
            stopped_early = len(statements)
            statements.clear()
            self.assertEqual(len(self.codes(self.pid, "alpha beta place dish a b c d e h")), 60)
        meal_queries = [s for s in statements if "FROM meals" in s]
        self.assertEqual(len(meal_queries), 12, "10 term queries plus the two hint queries")
        self.assertFalse(any("search_text" in s.split("FROM")[0] and "instr" not in s.split("FROM")[0]
                             for s in meal_queries), "search_text never selected")
        self.assertLess(stopped_early, len(statements))


# --- Keeping search_text in sync (§3) ---

class SyncTests(SearchTestCase):
    def stored(self, meal_id):
        return self.query("SELECT search_text FROM meals WHERE id = ?", (meal_id,))[0][0]

    def test_create_and_edit(self):
        meal_id = self.meal_id(self.pid, "PE", dish=" Crème ", place="Zuma", notes="one\r\ntwo")
        self.assertEqual(self.stored(meal_id), "creme\nzuma\none two")
        self.assertEqual(self.codes(self.pid, "creme"), ["PE"])
        resp = self.patch(self.pid, meal_id, {"eaten_on": DATE, "dish": "Pho"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.stored(meal_id), "pho\n\n")
        self.assertEqual(self.codes(self.pid, "creme"), [])
        self.assertEqual(self.codes(self.pid, "zuma"), [])
        self.assertEqual(self.codes(self.pid, "pho"), ["PE"])
        self.assertEqual(self.delete_meal(self.pid, meal_id).status, 200)
        self.assertEqual(self.codes(self.pid, "pho"), [])

    def test_failed_edit_leaves_it(self):
        meal_id = self.meal_id(self.pid, "PE", dish="Ceviche")
        self.patch(self.pid, meal_id, {"eaten_on": DATE, "dish": "Other"}, token=self.bob_token)
        self.patch(self.pid, meal_id, {"eaten_on": DATE, "dish": "x" * 101})
        self.assertEqual(self.stored(meal_id), "ceviche\n\n")

    def test_never_in_a_response(self):
        meal_id = self.meal_id(self.pid, "PE", dish="Ceviche", place="La Mar", notes="n")
        responses = [
            self.log_meal(self.pid, "PE", dish="Two", place="Zuma"),
            self.patch(self.pid, meal_id, {"eaten_on": DATE, "dish": "Ceviche", "place": "La Mar", "notes": "n"}),
            self.api("GET", f"/api/passports/{self.pid}/countries/PE"),
            self.api("GET", f"/api/passports/{self.pid}/countries"),
            self.search(self.pid, "ceviche"),
            self.search(self.pid, "zuma"),
        ]
        stored = {row[0] for row in self.query("SELECT search_text FROM meals")}
        self.assertEqual(stored, {search.meal_search_text("Ceviche", "La Mar", "n"),
                                  search.meal_search_text("Two", "Zuma", "")})
        for resp in responses:
            self.assertIn(resp.status, (200, 201), resp)
            keys, strings = set(), set()
            walk(resp.json(), keys, strings)
            self.assertNotIn("search_text", keys)
            self.assertFalse(stored & strings, "a stored search_text value in a response")


def walk(value, keys, strings):
    """Collects every object key and every string value in a decoded JSON document."""
    if isinstance(value, dict):
        keys.update(value)
        for item in value.values():
            walk(item, keys, strings)
    elif isinstance(value, list):
        for item in value:
            walk(item, keys, strings)
    elif isinstance(value, str):
        strings.add(value)


# --- done_count (§4) ---

class DoneCountTests(SearchTestCase):
    def done_counts(self, pid, token="default"):
        return {c["code"]: c["done_count"] for c in self.countries_of(pid, token)["countries"]}

    def test_key_set(self):
        for item in self.countries_of(self.pid)["countries"]:
            self.assertEqual(set(item), SUMMARY_KEYS)
            self.assertNotIn("search_text", item)

    def test_active_participants_only(self):
        carol, carol_token = self.user("carol")
        self.add(self.pid, carol, "carol", "participant")
        self.meal_id(self.pid, "PE")
        self.meal_id(self.pid, "PE")
        self.meal_id(self.pid, "PE", self.bob_token)
        self.meal_id(self.pid, "PE", carol_token)
        self.meal_id(self.pid, "JP", carol_token)
        counts = self.done_counts(self.pid)
        self.assertEqual((counts["PE"], counts["JP"], counts["FR"]), (3, 1, 0))
        self.assertEqual(self.done_counts(self.pid, self.sam_token), counts, "viewer sees the same")
        self.assertEqual(self.admin_action(carol, "disable").status, 200)
        counts = self.done_counts(self.pid)
        self.assertEqual((counts["PE"], counts["JP"]), (2, 0), "a disabled participant's meals don't count")
        listing = self.countries_of(self.pid, self.sam_token)
        self.assertEqual(listing["progress"]["active_participants"], 2)
        self.assertTrue(all(0 <= c["done_count"] <= 2 for c in listing["countries"]))
        self.assertEqual(self.admin_action(carol, "enable").status, 200)
        self.assertEqual(self.done_counts(self.pid)["JP"], 1)

    def test_personal_passport(self):
        self.assertEqual(set(self.done_counts(self.personal).values()), {0})
        self.meal_id(self.personal, "PE")
        self.meal_id(self.personal, "PE")
        counts = self.done_counts(self.personal)
        self.assertEqual((counts["PE"], sum(counts.values())), (1, 1))
        # a viewer of a personal passport whose owner is disabled: no active participants, so 0
        bob_personal = self.personal_id(self.bob)
        self.add(bob_personal, self.sam, "sam", "viewer", token=self.bob_token)
        self.meal_id(bob_personal, "PE", self.bob_token)
        self.assertEqual(self.done_counts(bob_personal, self.sam_token)["PE"], 1)
        self.set_disabled(self.bob)
        self.assertEqual(self.done_counts(bob_personal, self.sam_token)["PE"], 0)
        self.set_disabled(self.bob, 0)


# --- Busy and time budget (review M1) ---

class LimitTests(SearchTestCase):
    def setUp(self):
        super().setUp()
        self.app.search_wait = 0.05
        self.meal_id(self.pid, "PE", dish="Ceviche")

    def assert_all_slots_free(self):
        slots = self.app.search_slots
        taken = [slots.acquire(blocking=False) for _ in range(4)]
        self.assertEqual(taken, [True] * 4)
        self.assertFalse(slots.acquire(blocking=False), "exactly four slots")
        for _ in range(4):
            slots.release()

    def test_defaults(self):
        self.assertEqual((server.SEARCH_SLOTS, server.SEARCH_WAIT, server.SEARCH_BUDGET), (4, 1, 5))
        self.assertEqual((search.PROGRESS_STEPS, search.MAX_TERMS, search.MAX_TERM, search.MAX_QUERY),
                         (10000, 10, 50, 200))
        self.assert_all_slots_free()

    def test_slots_held(self):
        slots = self.app.search_slots
        for _ in range(4):
            self.assertTrue(slots.acquire(blocking=False))
        try:
            resp = self.search(self.pid, "ceviche")
            self.assertEqual((resp.status, resp.json()), (503, {"error": BUSY}))
            # validation still comes first
            self.assertEqual(self.search(self.pid, "").status, 400)
            slots.release()
            self.assertEqual(self.codes(self.pid, "ceviche"), ["PE"], "one free slot is enough")
            slots.acquire()
        finally:
            for _ in range(4):
                slots.release()
        self.assert_all_slots_free()

    def test_slots_released(self):
        self.assertEqual(self.search(self.pid, "ceviche").status, 200)
        self.assertEqual(self.search(99999, "ceviche").status, 404)
        self.assertEqual(self.search(self.pid, "ceviche", token=self.eve_token).status, 404)
        self.assertEqual(self.search(self.pid, "").status, 400)
        self.app.search_budget = -1
        self.insert_meals(self.pid, 1, [("JP", DATE, "Dish", "", "x" * 500)] * 3000)
        self.assertEqual(self.search(self.pid, "zzz").status, 503)
        self.assert_all_slots_free()

    def test_budget(self):
        self.insert_meals(self.pid, 1, [("JP", DATE, "Dish", "", "x" * 500)] * 3000)
        self.app.search_budget = -1  # already spent: the first progress callback interrupts
        resp = self.search(self.pid, "zzz")
        self.assertEqual((resp.status, resp.json()), (503, {"error": BUSY}))
        self.assertNotIn("Traceback", self.log.getvalue())
        self.assertNotIn("zzz", self.log.getvalue())
        self.assertNotIn("interrupt", self.log.getvalue())
        self.app.search_budget = 5
        self.assertEqual(self.codes(self.pid, "ceviche"), ["PE"], "the server carries on")
        self.assertEqual(self.codes(self.pid, "dish"), ["JP"])

    def test_budget_handler_cleared(self):
        self.insert_meals(self.pid, 1, [("JP", DATE, "Dish", "", "x" * 500)] * 3000)
        conn = db.connect(self.app.db_path)
        try:
            limit = search.Budget(-1)
            with self.assertRaises(sqlite3.OperationalError):
                with db.read(conn), search.budget(conn, limit):
                    search.find(conn, self.pid, ["zzz"])
            self.assertTrue(limit.expired)
            self.assertFalse(conn.in_transaction)
            with db.read(conn):
                self.assertEqual(search.find(conn, self.pid, ["dish"]), [{"code": "JP", "meal": {
                    "dish": "Dish", "place": "", "notes_excerpt": None}}])
        finally:
            conn.close()

    def test_unrelated_database_error_is_not_busy(self):
        # Only an interrupted search is a 503; other database errors stay 500s.
        def broken(conn, passport_id, terms):
            raise sqlite3.OperationalError("disk I/O error")

        with mock.patch.object(search, "find", broken):
            resp = self.search(self.pid, "ceviche")
        self.assertEqual((resp.status, resp.error), (500, "Internal error"))
        self.assert_all_slots_free()


class PerformanceTests(SearchTestCase):
    def test_ten_terms_over_5000_meals(self):
        codes = [c.code for c in countries.COUNTRIES][:50]
        words = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
        rows = [(codes[i % 50], DATE, f"Dish {i}", f"Place {i}", f"{words} note {i}\nmore") for i in range(5000)]
        self.insert_meals(self.pid, 1, rows)
        start = time.monotonic()
        result = self.matches(self.pid, words)
        elapsed = time.monotonic() - start
        self.assertEqual(len(result), 50)
        self.assertTrue(all(m["meal"]["notes_excerpt"] is not None for m in result))
        self.assertLess(elapsed, 2)

    def test_worst_case_data(self):
        # Maximal expansion: 5,000 meals of 2,000 U+FDFA notes, searched with near-miss words.
        dish = "a" * 100
        notes = "\ufdfa" * 2000
        self.insert_meals(self.pid, 1, [("PE", DATE, dish, dish, notes)] * 5000)
        stored = self.query("SELECT length(search_text) FROM meals LIMIT 1")[0][0]
        self.assertEqual(stored, 100 + 1 + 100 + 1 + 4000, "notes cut at the 4,000 cap")
        # The longest query the API accepts: four near-miss words, 200 code points in all.
        q = " ".join(["a" * 49 + "b", "a" * 48 + "bb", "a" * 47 + "bbb", "a" * 46 + "c"])
        self.assertEqual(len(q), 200)
        start = time.monotonic()
        resp = self.search(self.pid, q)
        elapsed = time.monotonic() - start
        self.assertIn(resp.status, (200, 503), resp)
        if resp.status == 200:
            self.assertEqual(resp.json(), {"matches": []})
        self.assertLess(elapsed, 10)
        # The contract's case, 10 near-miss 50-character words (more than a request can carry), run directly
        # under the real budget: finishes or is interrupted, never hangs.
        terms = ["a" * (49 - i) + "b" * (i + 1) for i in range(10)]
        conn = db.connect(self.app.db_path)
        try:
            limit = search.Budget(server.SEARCH_BUDGET)
            start = time.monotonic()
            try:
                with db.read(conn), search.budget(conn, limit):
                    self.assertEqual(search.find(conn, self.pid, terms), [])
            except sqlite3.OperationalError:
                self.assertTrue(limit.expired)
            self.assertLess(time.monotonic() - start, server.SEARCH_BUDGET + 2)
        finally:
            conn.close()
        self.assertNotIn("Traceback", self.log.getvalue())


class LoggingTests(SearchTestCase):
    def test_query_not_logged(self):
        self.meal_id(self.pid, "PE", dish="Zanzibarite")
        for q in ("zanzibarite", "Zanzibarite peru", "zanzibarite " + "x" * 60, "zanzibarite" * 20):
            self.search(self.pid, q)
        self.search(99999, "zanzibarite")
        self.app.search_budget = -1
        self.insert_meals(self.pid, 1, [("JP", DATE, "Dish", "", "x" * 500)] * 3000)
        self.search(self.pid, "zanzibarite")
        log = self.log.getvalue()
        self.assertNotIn("zanzibar", log.lower())
        self.assertNotIn("Peru", log)
        self.assertIn(f"POST /api/passports/{self.pid}/search 200", log)
        self.assertIn(f"POST /api/passports/{self.pid}/search 400", log)
        self.assertIn(f"POST /api/passports/{self.pid}/search 503", log)


# --- The server on a feature 3 (version 4) database ---

class MigrationServerTests(ServerTestCase):
    def prepare_data(self):
        path = f"{self.data_dir}/{db.DB_NAME}"
        db.init_schema(path, db.MIGRATIONS[:4])
        conn = db.connect(path)
        try:
            password_hash = fast_hasher().hash(PASSWORD)
            conn.executescript("""
                INSERT INTO users (id, username, display_name, password_hash, is_admin, created_at)
                    VALUES (1, 'admin', 'Admin', 'x', 1, 100);
                INSERT INTO passports (id, name, owner_id, is_personal, created_at)
                    VALUES (1, 'My passport', 1, 1, 100);
                INSERT INTO passport_members (passport_id, user_id, role, created_at)
                    VALUES (1, 1, 'participant', 100);
            """)
            conn.execute("UPDATE users SET password_hash = ?", (password_hash,))
            conn.execute("INSERT INTO meals (passport_id, user_id, country_code, eaten_on, dish, place, notes, "
                         "created_at, updated_at) VALUES (1, 1, 'FR', '2026-10-01', 'Crème Brûlée', 'Chez Ødile', "
                         "'Très\nbon', 1, 1)")
            now = self.base_time
            conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, last_used_at, expires_at) "
                         "VALUES (?, 1, ?, ?, ?)", (sha256_hex("live-token"), now, now, now + 1000))
        finally:
            conn.close()

    def test_old_meals_are_searchable(self):
        with self.db() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5)
        resp = self.request("POST", "/api/passports/1/search", {"q": "creme odile tres bon"}, token="live-token")
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(json.loads(resp.body), {"matches": [
            {"code": "FR", "meal": {"dish": "Crème Brûlée", "place": "Chez Ødile", "notes_excerpt": None}}]})
        resp = self.request("POST", "/api/passports/1/search", {"q": "bon"}, token="live-token")
        france = next(m for m in resp.json()["matches"] if m["code"] == "FR")
        self.assertEqual(france["meal"]["notes_excerpt"], "Très bon")
