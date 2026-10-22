"""Feature 3: meals and per-country progress."""

import datetime
import json
from unittest import mock

from tests.helpers import server
from tests.test_passports import NOT_FOUND, PENDING, PERSON_KEYS, PassportTestCase, race, ref

import countries  # noqa: E402  (app/ is on sys.path via tests.helpers)
import db  # noqa: E402

SUMMARY_KEYS = {"code", "status", "done_by_me", "meal_count", "avg_rating", "completed_on", "done_count"}
PROGRESS_KEYS = {"total", "complete", "mine", "active_participants"}
DETAIL_KEYS = {"country", "status", "completed_on", "avg_rating", "participants", "meals"}
PARTICIPANT_KEYS = PERSON_KEYS | {"is_owner", "done", "completed_on"}
MEAL_KEYS = {"id", "user", "eaten_on", "dish", "place", "rating", "notes", "mine"}

DATE_MSG = "Choose a date between 1900 and today"
DISH_LEN = '"What you ate" can be up to 100 characters'
DISH_CHARS = '"What you ate" contains characters that aren\'t allowed'
PLACE_LEN = '"Where" can be up to 100 characters'
PLACE_CHARS = '"Where" contains characters that aren\'t allowed'
STARS_MSG = "Stars must be 1 to 5"
NOTES_LEN = "Notes can be up to 2,000 characters"
NOTES_CHARS = "Notes contain characters that aren't allowed"
PARTICIPANTS_ONLY = "Only participants can log meals"
NOT_YOURS = "You can only change your own meals"
MEAL_NOT_FOUND = {"error": "Meal not found", "code": "meal_not_found"}
COUNTRY_NOT_FOUND = {"error": "Country not found", "code": "country_not_found"}
PER_COUNTRY = "You can log at most 100 meals for one country"
PER_PASSPORT = "You can log at most 5,000 meals in one passport"
PER_ACCOUNT = "You can log at most 20,000 meals"

DATE = "2026-10-11"


class MealTestCase(PassportTestCase):
    """Admin (id 1, owner), bob (participant), sam (viewer), eve (outsider) and the shared passport "Trip"."""

    def setUp(self):
        super().setUp()
        self.bob, self.bob_token = self.user("bob", display_name="Bob")
        self.sam, self.sam_token = self.user("sam", display_name="Sam")
        self.eve, self.eve_token = self.user("eve", display_name="Eve")
        self.pid = self.create_id("Trip", [ref(self.bob, "bob")], [ref(self.sam, "sam")])
        self.personal = self.personal_id(1)

    def today(self):
        return datetime.datetime.fromtimestamp(self.base_time + self.clock_offset, datetime.timezone.utc).date()

    # --- Requests ---

    def countries_of(self, pid, token="default"):
        resp = self.api("GET", f"/api/passports/{pid}/countries", token=token)
        self.assertEqual(resp.status, 200, resp)
        return resp.json()

    def summary(self, pid, code, token="default"):
        return next(c for c in self.countries_of(pid, token)["countries"] if c["code"] == code)

    def country(self, pid, code, token="default"):
        resp = self.api("GET", f"/api/passports/{pid}/countries/{code}", token=token)
        self.assertEqual(resp.status, 200, resp)
        return resp.json()

    def log_meal(self, pid, code, token="default", **fields):
        body = {"eaten_on": DATE, **fields}
        return self.api("POST", f"/api/passports/{pid}/countries/{code}/meals", body, token=token)

    def meal_id(self, pid, code, token="default", **fields):
        resp = self.log_meal(pid, code, token, **fields)
        self.assertEqual(resp.status, 201, resp)
        return resp.json()["meal"]["id"]

    def patch(self, pid, meal_id, body, token="default"):
        return self.api("PATCH", f"/api/passports/{pid}/meals/{meal_id}", body, token=token)

    def delete_meal(self, pid, meal_id, token="default"):
        return self.api("POST", f"/api/passports/{pid}/meals/{meal_id}/delete", {}, token=token)

    # --- Direct database access ---

    def meals_rows(self, sql_where="1", params=()):
        return self.query("SELECT id, passport_id, user_id, country_code, eaten_on, dish, place, rating, notes "
                          "FROM meals WHERE " + sql_where + " ORDER BY id", params)

    def seed(self, pid, user_id, code, count, eaten_on=DATE):
        with self.db() as conn:
            conn.executemany(
                "INSERT INTO meals (passport_id, user_id, country_code, eaten_on, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, 1)", [(pid, user_id, code, eaten_on)] * count)

    def meal_count(self, sql_where="1", params=()):
        return self.query("SELECT COUNT(*) FROM meals WHERE " + sql_where, params)[0][0]


# --- Access (§4) ---

class AccessTests(MealTestCase):
    def setUp(self):
        super().setUp()
        self.admin_meal = self.meal_id(self.pid, "PE")
        self.personal_meal = self.meal_id(self.personal, "FR")

    def calls(self, pid, meal_id):
        valid = {"eaten_on": DATE, "dish": "X"}
        return [
            ("GET", f"/api/passports/{pid}/countries", None),
            ("GET", f"/api/passports/{pid}/countries/PE", None),
            ("GET", f"/api/passports/{pid}/countries/ZZ", None),
            ("POST", f"/api/passports/{pid}/countries/PE/meals", valid),
            ("POST", f"/api/passports/{pid}/countries/ZZ/meals", valid),
            ("PATCH", f"/api/passports/{pid}/meals/{meal_id}", valid),
            ("PATCH", f"/api/passports/{pid}/meals/99999", valid),
            ("POST", f"/api/passports/{pid}/meals/{meal_id}/delete", {}),
            ("POST", f"/api/passports/{pid}/meals/99999/delete", {}),
        ]

    def snapshot(self):
        return self.meals_rows()

    def test_non_member_same_as_missing(self):
        before = self.snapshot()
        missing = [(r.status, r.body) for r in
                   (self.api(m, p, b, token=self.eve_token) for m, p, b in self.calls(99999, self.admin_meal))]
        for pid, meal_id in ((self.pid, self.admin_meal), (self.personal, self.personal_meal)):
            for (method, path, body), expected in zip(self.calls(pid, meal_id), missing):
                with self.subTest(method=method, path=path):
                    resp = self.api(method, path, body, token=self.eve_token)
                    self.assertEqual((resp.status, resp.json()), (404, NOT_FOUND))
                    self.assertEqual((resp.status, resp.body), expected, "byte-identical to a missing passport")
        self.assertEqual(self.snapshot(), before)

    def test_admin_non_member(self):
        bob_personal = self.personal_id(self.bob)
        bob_meal = self.meal_id(bob_personal, "PE", token=self.bob_token)
        before = self.snapshot()
        for method, path, body in self.calls(bob_personal, bob_meal):
            with self.subTest(method=method, path=path):
                resp = self.api(method, path, body)
                self.assertEqual((resp.status, resp.json()), (404, NOT_FOUND))
        self.assertEqual(self.snapshot(), before)

    def test_logged_out(self):
        for method, path, body in self.calls(self.pid, self.admin_meal) + [("GET", "/api/countries", None)]:
            with self.subTest(method=method, path=path):
                resp = self.api(method, path, body, token=None)
                self.assertEqual((resp.status, resp.error), (401, "Not logged in"))

    def test_pending(self):
        bob_meal = self.meal_id(self.pid, "PE", token=self.bob_token)
        with self.db() as conn:
            conn.execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (self.bob,))
        before = self.snapshot()
        for method, path, body in self.calls(self.pid, bob_meal) + [("GET", "/api/countries", None)]:
            with self.subTest(method=method, path=path):
                resp = self.api(method, path, body, token=self.bob_token)
                self.assertEqual((resp.status, resp.json()), (403, PENDING))
        self.assertEqual(self.snapshot(), before)

    def test_viewer(self):
        listing = self.countries_of(self.pid, token=self.sam_token)
        self.assertEqual(len(listing["countries"]), 197)
        detail = self.country(self.pid, "PE", token=self.sam_token)
        self.assertEqual([m["id"] for m in detail["meals"]], [self.admin_meal])
        self.assertFalse(detail["meals"][0]["mine"])
        before = self.snapshot()
        resp = self.log_meal(self.pid, "PE", token=self.sam_token)
        self.assertEqual((resp.status, resp.error), (403, PARTICIPANTS_ONLY))
        resp = self.log_meal(self.pid, "ZZ", token=self.sam_token)
        self.assertEqual((resp.status, resp.json()), (404, COUNTRY_NOT_FOUND), "country checked before role")
        resp = self.patch(self.pid, self.admin_meal, {"eaten_on": DATE}, token=self.sam_token)
        self.assertEqual((resp.status, resp.error), (403, NOT_YOURS))
        resp = self.delete_meal(self.pid, self.admin_meal, token=self.sam_token)
        self.assertEqual((resp.status, resp.error), (403, NOT_YOURS))
        self.assertEqual(self.snapshot(), before)

    def test_others_meals(self):
        before = self.snapshot()
        resp = self.patch(self.pid, self.admin_meal, {"eaten_on": DATE, "dish": "Mine"}, token=self.bob_token)
        self.assertEqual((resp.status, resp.error), (403, NOT_YOURS))
        resp = self.delete_meal(self.pid, self.admin_meal, token=self.bob_token)
        self.assertEqual((resp.status, resp.error), (403, NOT_YOURS))
        self.assertEqual(self.snapshot(), before)

    def test_meal_from_another_passport(self):
        other = self.create_id("Other", [ref(self.bob, "bob")])
        other_meal = self.meal_id(other, "PE")
        before = self.snapshot()
        for meal_id in (other_meal, self.personal_meal):
            with self.subTest(meal_id=meal_id):
                resp = self.patch(self.pid, meal_id, {"eaten_on": DATE, "dish": "Moved"})
                missing = self.patch(self.pid, 99999, {"eaten_on": DATE, "dish": "Moved"})
                self.assertEqual((resp.status, resp.json()), (404, MEAL_NOT_FOUND))
                self.assertEqual(resp.body, missing.body)
                resp = self.delete_meal(self.pid, meal_id)
                missing = self.delete_meal(self.pid, 99999)
                self.assertEqual((resp.status, resp.json()), (404, MEAL_NOT_FOUND))
                self.assertEqual(resp.body, missing.body)
        # bob is a participant in both; another's meal from Q via P is still "not found"
        resp = self.patch(self.pid, other_meal, {"eaten_on": DATE}, token=self.bob_token)
        self.assertEqual((resp.status, resp.json()), (404, MEAL_NOT_FOUND))
        self.assertEqual(self.snapshot(), before)

    def test_bad_codes(self):
        for code in ("pe", "Pe", "pE", "P", "PER", "P1", "1P", "P%C3%89", "%C3%89%C3%89", "P%20", "PE%00"):
            for method, suffix in (("GET", ""), ("POST", "/meals")):
                path = f"/api/passports/{self.pid}/countries/{code}{suffix}"
                with self.subTest(path=path):
                    resp = self.api(method, path, {"eaten_on": DATE} if method == "POST" else None)
                    self.assertEqual((resp.status, resp.error), (404, "Not found"))
        for raw in (b"\xc3\x89\xc3\x89", b"P\xc3\x89", b"\xef\xbc\xb0\xef\xbc\xa5"):  # ÉÉ, PÉ, full-width PE
            with self.subTest(raw=raw):
                head = (f"GET /api/passports/{self.pid}/countries/".encode() + raw + b" HTTP/1.0\r\n"
                        + f"Host: {self.host}\r\nCookie: {self.cookie_name}={self.token}\r\n\r\n".encode())
                resp = self.raw(head)
                self.assertEqual((resp.status, resp.error), (404, "Not found"))
        for path in (f"/api/passports/{self.pid}/countries/ＰＥ", f"/api/passports/{self.pid}/countries/ÉÉ",
                     f"/api/passports/{self.pid}/meals/٥", f"/api/passports/{self.pid}/countries/pe/meals"):
            with self.subTest(path=path):
                self.assertFalse(any(p.fullmatch(path) for p, _ in server.ROUTES))
        resp = self.api("GET", f"/api/passports/{self.pid}/countries/ZZ")
        self.assertEqual((resp.status, resp.json()), (404, COUNTRY_NOT_FOUND))
        resp = self.log_meal(self.pid, "ZZ")
        self.assertEqual((resp.status, resp.json()), (404, COUNTRY_NOT_FOUND))
        resp = self.api("GET", "/api/passports/99999/countries/ZZ")
        self.assertEqual((resp.status, resp.json()), (404, NOT_FOUND), "membership first")

    def test_bad_meal_ids(self):
        for bad in ("0", "01", "-1", "abc", "12345678901", "1.0"):
            for method, suffix in (("PATCH", ""), ("POST", "/delete")):
                path = f"/api/passports/{self.pid}/meals/{bad}{suffix}"
                with self.subTest(path=path):
                    self.assertEqual(self.api(method, path, {"eaten_on": DATE}).error, "Not found")

    def test_wrong_method(self):
        for method, path in (("POST", f"/api/passports/{self.pid}/countries"),
                             ("PATCH", f"/api/passports/{self.pid}/countries/PE"),
                             ("GET", f"/api/passports/{self.pid}/countries/PE/meals"),
                             ("GET", f"/api/passports/{self.pid}/meals/{self.admin_meal}"),
                             ("POST", f"/api/passports/{self.pid}/meals/{self.admin_meal}"),
                             ("GET", f"/api/passports/{self.pid}/meals/{self.admin_meal}/delete")):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.api(method, path).status, 405)
        resp = self.api("HEAD", f"/api/passports/{self.pid}/countries")
        self.assertEqual((resp.status, resp.body), (200, b""))

    def test_cross_site(self):
        bob_meal = self.meal_id(self.pid, "PE", token=self.bob_token)
        before = self.snapshot()
        for method, path, body in (("POST", f"/api/passports/{self.pid}/countries/PE/meals", {"eaten_on": DATE}),
                                   ("PATCH", f"/api/passports/{self.pid}/meals/{bob_meal}", {"eaten_on": DATE}),
                                   ("POST", f"/api/passports/{self.pid}/meals/{bob_meal}/delete", {})):
            with self.subTest(path=path):
                resp = self.api(method, path, body, token=self.bob_token, content_type="text/plain")
                self.assertEqual((resp.status, resp.error), (415, "Content-Type must be application/json"))
                resp = self.api(method, path, body, token=self.bob_token, origin="http://evil.example")
                self.assertEqual((resp.status, resp.error), (403, "Cross-origin request blocked"))
                resp = self.api(method, path, body, token=self.bob_token, origin=None)
                self.assertEqual((resp.status, resp.error), (403, "Cross-origin request blocked"))
        self.assertEqual(self.snapshot(), before)


# --- Validation (§5) ---

class ValidationTests(MealTestCase):
    def post_meal(self, **fields):
        return self.api("POST", f"/api/passports/{self.pid}/countries/PE/meals", fields)

    def assert_refused(self, message, **fields):
        before = self.meal_count()
        resp = self.post_meal(**fields)
        self.assertEqual((resp.status, resp.error), (400, message), fields)
        self.assertEqual(self.meal_count(), before)

    def stored(self, **fields):
        resp = self.post_meal(**fields)
        self.assertEqual(resp.status, 201, (resp, fields))
        meal = resp.json()["meal"]
        row = self.meals_rows("id = ?", (meal["id"],))[0]
        self.assertEqual((meal["eaten_on"], meal["dish"], meal["place"], meal["rating"], meal["notes"]), row[4:])
        return meal

    def test_dates(self):
        today = self.today()
        for value in ("1900-01-01", DATE, today.isoformat(), (today + datetime.timedelta(days=1)).isoformat()):
            with self.subTest(value=value):
                self.assertEqual(self.stored(eaten_on=value)["eaten_on"], value)
        for value in ("1899-12-31", (today + datetime.timedelta(days=2)).isoformat(), "2026-02-30", "2026-1-01",
                      "2026-01-1", "0000-01-01", "2026-10-11\n", "２０２６-１０-１１", "2026-10-11T00:00",
                      " 2026-10-11", "2026-10-11 ", "2026/10/11", "20261011", "2026-13-01", "2026-00-10",
                      "9999-12-31", "", 20261011, 2026.5, True, None, [DATE], {"d": DATE}):
            with self.subTest(value=value):
                self.assert_refused(DATE_MSG, eaten_on=value)
        self.assert_refused(DATE_MSG)  # missing

    def test_date_upper_bound_follows_the_app_clock_in_utc(self):
        # Move the fake clock to the last second of a UTC day, then to the first of the next.
        start = datetime.datetime.fromtimestamp(self.base_time, datetime.timezone.utc)
        midnight = datetime.datetime.combine(start.date() + datetime.timedelta(days=1), datetime.time(),
                                             datetime.timezone.utc)
        self.clock_offset = int(midnight.timestamp()) - self.base_time - 1
        two_ahead = (start.date() + datetime.timedelta(days=2)).isoformat()
        self.assert_refused(DATE_MSG, eaten_on=two_ahead)
        self.clock_offset += 1
        self.assertEqual(self.stored(eaten_on=two_ahead)["eaten_on"], two_ahead)

    def check_text(self, field, length_msg, chars_msg, limit=100):
        self.assertEqual(self.stored(eaten_on=DATE, **{field: "x" * limit})[field], "x" * limit)
        self.assert_refused(length_msg, eaten_on=DATE, **{field: "x" * (limit + 1)})
        # NFC first, then the length: 'e' + combining acute counts as one code point
        decomposed = "e\u0301"  # two code points; NFC makes it one, U+00E9
        self.assertEqual(self.stored(eaten_on=DATE, **{field: decomposed * limit})[field], "\u00e9" * limit)
        self.assert_refused(length_msg, eaten_on=DATE, **{field: decomposed * (limit + 1)})
        # trimmed before the length check
        self.assertEqual(self.stored(eaten_on=DATE, **{field: "  " + "x" * limit + "  "})[field], "x" * limit)
        self.assertEqual(self.stored(eaten_on=DATE, **{field: "   "})[field], "")
        self.assertEqual(self.stored(eaten_on=DATE, **{field: None})[field], "")
        family = "\U0001F468‍\U0001F469‍\U0001F467"
        self.assertEqual(self.stored(eaten_on=DATE, **{field: "Café " + family})[field], "Café " + family)
        for value, message in ((5, length_msg), (True, length_msg), (["x"], length_msg), ({}, length_msg)):
            with self.subTest(value=value):
                self.assert_refused(message, eaten_on=DATE, **{field: value})
        for bad in ("a\u0000b", "a\u0007b", "a\tb", "a\nb", "a\rb", "a​b", "a‮b", "a b",
                    "a b", "ab", "a\ud800b", "a͸b"):
            with self.subTest(bad=bad):
                self.assert_refused(chars_msg, eaten_on=DATE, **{field: bad})

    def test_dish(self):
        self.check_text("dish", DISH_LEN, DISH_CHARS)

    def test_place(self):
        self.check_text("place", PLACE_LEN, PLACE_CHARS)

    def test_rating(self):
        for value in (1, 5, None):
            with self.subTest(value=value):
                self.assertEqual(self.stored(eaten_on=DATE, rating=value)["rating"], value)
        self.assertIsNone(self.stored(eaten_on=DATE)["rating"])
        for value in (0, 6, -1, True, False, "5", 5.0, 4.5, [5], {"v": 5}, 10 ** 30):
            with self.subTest(value=value):
                self.assert_refused(STARS_MSG, eaten_on=DATE, rating=value)

    def test_notes(self):
        self.assertEqual(self.stored(eaten_on=DATE, notes="x" * 2000)["notes"], "x" * 2000)
        self.assert_refused(NOTES_LEN, eaten_on=DATE, notes="x" * 2001)
        self.assertEqual(self.stored(eaten_on=DATE, notes="é" * 2000)["notes"], "é" * 2000)
        self.assert_refused(NOTES_LEN, eaten_on=DATE, notes="é" * 2001)
        self.assertEqual(self.stored(eaten_on=DATE, notes="Line one\nLine two")["notes"], "Line one\nLine two")
        self.assertEqual(self.stored(eaten_on=DATE, notes="a\r\nb\rc\n\nd")["notes"], "a\nb\nc\n\nd")
        # \r\n counts as one character once converted
        self.assertEqual(len(self.stored(eaten_on=DATE, notes="x" * 1998 + "\r\n" + "y")["notes"]), 2000)
        self.assertEqual(self.stored(eaten_on=DATE, notes=" \n\t \r\n ")["notes"], "", "whitespace-only")
        self.assertEqual(self.stored(eaten_on=DATE, notes="\n  kept  \n")["notes"], "kept")
        self.assertEqual(self.stored(eaten_on=DATE, notes=None)["notes"], "")
        family = "\U0001F468‍\U0001F469‍\U0001F467"
        self.assertEqual(self.stored(eaten_on=DATE, notes=family)["notes"], family)
        for bad in ("a\tb", "a\u0000b", "a\u0007b", "a\u000bb", "a\u000cb", "a\u0085b", "a b", "a​b",
                    "a\ud800b"):
            with self.subTest(bad=bad):
                self.assert_refused(NOTES_CHARS, eaten_on=DATE, notes=bad)
        self.assert_refused(NOTES_LEN, eaten_on=DATE, notes=5)

    def test_field_order(self):
        self.assert_refused(DATE_MSG, eaten_on="bad", dish="x" * 101, place="x" * 101, rating=9, notes=5)
        self.assert_refused(DISH_LEN, eaten_on=DATE, dish="x" * 101, place="x" * 101, rating=9, notes=5)
        self.assert_refused(PLACE_CHARS, eaten_on=DATE, place="a\tb", rating=9, notes=5)
        self.assert_refused(STARS_MSG, eaten_on=DATE, rating=9, notes="x" * 2001)

    def test_validated_before_the_database(self):
        # §4/§5: body validation comes first, so even a non-member gets the 400 (it reveals nothing)
        resp = self.api("POST", "/api/passports/99999/countries/PE/meals", {"eaten_on": "bad"},
                        token=self.eve_token)
        self.assertEqual((resp.status, resp.error), (400, DATE_MSG))

    def test_server_fields_ignored(self):
        other = self.create_id("Other")
        resp = self.post_meal(eaten_on=DATE, id=1, user_id=self.bob, passport_id=other, country_code="FR",
                           created_at=5, updated_at=5, mine=False, user={"id": self.bob})
        self.assertEqual(resp.status, 201)
        meal = resp.json()["meal"]
        self.assertEqual((meal["user"]["id"], meal["mine"]), (1, True))
        self.assertEqual(self.query("SELECT passport_id, user_id, country_code, created_at, updated_at "
                                    "FROM meals WHERE id = ?", (meal["id"],)),
                         [(self.pid, 1, "PE", self.base_time, self.base_time)])
        meal_id = meal["id"]
        resp = self.patch(self.pid, meal_id, {"eaten_on": DATE, "id": 999, "user_id": self.bob,
                                              "passport_id": other, "country_code": "FR", "created_at": 5})
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.query("SELECT id, passport_id, user_id, country_code, created_at FROM meals "
                                    "WHERE id = ?", (meal_id,)), [(meal_id, self.pid, 1, "PE", self.base_time)])

    def test_maximal_meal_fits_the_body_limit(self):
        emoji = "\U0001F35C"  # 4 bytes in UTF-8, one code point
        body = {"eaten_on": DATE, "dish": emoji * 100, "place": emoji * 100, "rating": 5, "notes": emoji * 2000}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(raw), server.MAX_BODY)
        resp = self.api("POST", f"/api/passports/{self.pid}/countries/PE/meals", raw_body=raw)
        self.assertEqual(resp.status, 201, resp)
        meal = resp.json()["meal"]
        self.assertEqual((meal["dish"], meal["place"], meal["notes"]), (emoji * 100, emoji * 100, emoji * 2000))
        # sent as raw UTF-8, not \u escapes
        self.assertIn((emoji * 100).encode("utf-8"), resp.body)
        self.assertNotIn(b"\\u", resp.body)

    def test_patch_uses_the_same_rules(self):
        meal_id = self.meal_id(self.pid, "PE", dish="Ceviche")
        for fields, message in (({}, DATE_MSG), ({"eaten_on": "1899-12-31"}, DATE_MSG),
                                ({"eaten_on": DATE, "dish": "x" * 101}, DISH_LEN),
                                ({"eaten_on": DATE, "place": "a\nb"}, PLACE_CHARS),
                                ({"eaten_on": DATE, "rating": True}, STARS_MSG),
                                ({"eaten_on": DATE, "notes": "a\tb"}, NOTES_CHARS)):
            with self.subTest(fields=fields):
                resp = self.patch(self.pid, meal_id, fields)
                self.assertEqual((resp.status, resp.error), (400, message))
        self.assertEqual(self.meals_rows("id = ?", (meal_id,))[0][5], "Ceviche")


# --- Responses and derived values (§3, §6) ---

class DerivedTests(MealTestCase):
    def test_shapes(self):
        meal = self.log_meal(self.pid, "PE", dish="Ceviche", place="La Mar", rating=5, notes="a\nb").json()["meal"]
        self.assertEqual(set(meal), MEAL_KEYS)
        self.assertEqual(set(meal["user"]), PERSON_KEYS)
        self.assertEqual(meal, {"id": meal["id"], "user": {"id": 1, "username": "admin", "display_name": "Admin",
                                                           "inactive": False, "avatar_seed": self.seed_of(1)},
                                "eaten_on": DATE, "dish": "Ceviche", "place": "La Mar", "rating": 5,
                                "notes": "a\nb", "mine": True})
        listing = self.countries_of(self.pid)
        self.assertEqual(set(listing), {"progress", "countries"})
        self.assertEqual(set(listing["progress"]), PROGRESS_KEYS)
        self.assertEqual([c["code"] for c in listing["countries"]], [c.code for c in countries.COUNTRIES])
        for item in listing["countries"]:
            self.assertEqual(set(item), SUMMARY_KEYS)
        detail = self.country(self.pid, "PE")
        self.assertEqual(set(detail), DETAIL_KEYS)
        self.assertEqual(detail["country"], {"code": "PE", "name": "Peru", "region": "South America"})
        for p in detail["participants"]:
            self.assertEqual(set(p), PARTICIPANT_KEYS)
        self.assertEqual(detail["meals"], [meal])
        empty = self.country(self.pid, "FR")
        self.assertEqual((empty["status"], empty["completed_on"], empty["avg_rating"], empty["meals"]),
                         ("not_started", None, None, []))

    def test_status_with_two_participants(self):
        listing = self.countries_of(self.pid)
        self.assertEqual(listing["progress"], {"total": 197, "complete": 0, "mine": 0, "active_participants": 2})
        self.assertTrue(all(c["status"] == "not_started" and c["done_by_me"] is False and c["meal_count"] == 0
                            and c["avg_rating"] is None and c["completed_on"] is None and c["done_count"] == 0
                            for c in listing["countries"]))

        self.meal_id(self.pid, "PE", eaten_on="2026-10-11")
        pe = self.summary(self.pid, "PE")
        self.assertEqual(pe, {"code": "PE", "status": "some", "done_by_me": True, "meal_count": 1,
                              "avg_rating": None, "completed_on": None, "done_count": 1})
        self.assertFalse(self.summary(self.pid, "PE", token=self.bob_token)["done_by_me"])
        self.assertIsNone(self.summary(self.pid, "PE", token=self.sam_token)["done_by_me"])

        self.meal_id(self.pid, "PE", self.bob_token, eaten_on="2026-10-20")
        self.meal_id(self.pid, "PE", self.bob_token, eaten_on="2026-10-05")
        pe = self.summary(self.pid, "PE")
        self.assertEqual((pe["status"], pe["completed_on"], pe["meal_count"]), ("complete", "2026-10-11", 3))
        detail = self.country(self.pid, "PE", token=self.sam_token)
        self.assertEqual((detail["status"], detail["completed_on"]), ("complete", "2026-10-11"))
        self.assertEqual([(p["id"], p["is_owner"], p["done"], p["completed_on"]) for p in detail["participants"]],
                         [(1, True, True, "2026-10-11"), (self.bob, False, True, "2026-10-05")])
        self.assertNotIn(self.sam, [p["id"] for p in detail["participants"]], "viewers aren't participants")
        self.assertEqual([m["eaten_on"] for m in detail["meals"]], ["2026-10-20", "2026-10-11", "2026-10-05"])

        self.meal_id(self.pid, "FR", self.bob_token)
        self.assertEqual(self.countries_of(self.pid)["progress"],
                         {"total": 197, "complete": 1, "mine": 1, "active_participants": 2})
        self.assertEqual(self.countries_of(self.pid, token=self.bob_token)["progress"]["mine"], 2)
        viewer = self.countries_of(self.pid, token=self.sam_token)["progress"]
        self.assertEqual(viewer, {"total": 197, "complete": 1, "mine": None, "active_participants": 2})

    def test_meal_order_ties_by_id(self):
        first = self.meal_id(self.pid, "PE", eaten_on=DATE)
        second = self.meal_id(self.pid, "PE", self.bob_token, eaten_on=DATE)
        older = self.meal_id(self.pid, "PE", eaten_on="2026-01-01")
        newer = self.meal_id(self.pid, "PE", eaten_on="2026-12-01")
        detail = self.country(self.pid, "PE", token=self.bob_token)
        self.assertEqual([m["id"] for m in detail["meals"]], [newer, second, first, older])
        self.assertEqual([m["mine"] for m in detail["meals"]], [False, True, False, False])
        # the meal id and the user id are separate columns (both are "id" in the query's tables)
        self.assertEqual([m["user"]["id"] for m in detail["meals"]], [1, self.bob, 1, 1])

    def test_disabled_participant(self):
        carol, carol_token = self.user("carol", display_name="Carol")
        self.add(self.pid, carol, "carol", "participant")
        self.meal_id(self.pid, "PE", eaten_on="2026-10-11")
        self.meal_id(self.pid, "PE", self.bob_token, eaten_on="2026-10-12")
        self.meal_id(self.pid, "FR", carol_token, eaten_on="2026-12-01", rating=1)
        self.meal_id(self.pid, "FR", eaten_on="2026-10-01", rating=5)
        self.meal_id(self.pid, "FR", self.bob_token, eaten_on="2026-10-02")
        self.assertEqual(self.summary(self.pid, "PE")["status"], "some", "carol hasn't done PE")
        self.assertEqual(self.summary(self.pid, "FR")["completed_on"], "2026-12-01")

        self.assertEqual(self.admin_action(carol, "disable").status, 200)
        listing = self.countries_of(self.pid)
        self.assertEqual(listing["progress"]["active_participants"], 2)
        by_code = {c["code"]: c for c in listing["countries"]}
        self.assertEqual((by_code["PE"]["status"], by_code["PE"]["completed_on"]), ("complete", "2026-10-12"))
        # carol's later date no longer counts; her rating still does
        self.assertEqual((by_code["FR"]["status"], by_code["FR"]["completed_on"], by_code["FR"]["avg_rating"]),
                         ("complete", "2026-10-02", 3.0))
        detail = self.country(self.pid, "FR")
        carol_row = next(p for p in detail["participants"] if p["id"] == carol)
        self.assertEqual((carol_row["inactive"], carol_row["done"], carol_row["completed_on"]),
                         (True, True, "2026-12-01"))
        carol_meal = next(m for m in detail["meals"] if m["user"]["id"] == carol)
        self.assertEqual(carol_meal["user"], {"id": carol, "username": "carol", "display_name": "Carol",
                                              "inactive": True, "avatar_seed": self.seed_of(carol)})
        self.assertEqual(self.meal_count("user_id = ?", (carol,)), 1, "disabling keeps meals")

        self.assertEqual(self.admin_action(carol, "enable").status, 200)
        self.assertEqual(self.summary(self.pid, "PE")["status"], "some", "re-enabled: counts again")
        self.assertEqual(self.countries_of(self.pid)["progress"]["active_participants"], 3)

    def test_new_participant_drops_complete(self):
        self.meal_id(self.pid, "PE")
        self.meal_id(self.pid, "PE", self.bob_token)
        self.assertEqual(self.summary(self.pid, "PE")["status"], "complete")
        carol, carol_token = self.user("carol")
        self.add(self.pid, carol, "carol", "participant")
        pe = self.summary(self.pid, "PE")
        self.assertEqual((pe["status"], pe["completed_on"]), ("some", None))
        self.meal_id(self.pid, "PE", carol_token, eaten_on="2026-11-30")
        self.assertEqual(self.summary(self.pid, "PE")["completed_on"], "2026-11-30")
        # a new viewer changes nothing
        dan, _ = self.user("dan")
        self.add(self.pid, dan, "dan", "viewer")
        self.assertEqual(self.summary(self.pid, "PE")["status"], "complete")

    def test_all_participants_inactive(self):
        # A viewer of a passport whose only participant is disabled: meals exist, nobody active -> "some".
        bob_personal = self.personal_id(self.bob)
        self.add(bob_personal, self.sam, "sam", "viewer", token=self.bob_token)
        self.meal_id(bob_personal, "PE", self.bob_token)
        self.set_disabled(self.bob)
        listing = self.countries_of(bob_personal, token=self.sam_token)
        self.assertEqual(listing["progress"], {"total": 197, "complete": 0, "mine": None, "active_participants": 0})
        pe = next(c for c in listing["countries"] if c["code"] == "PE")
        self.assertEqual((pe["status"], pe["completed_on"]), ("some", None))
        self.set_disabled(self.bob, 0)

    def test_avg_rating(self):
        self.assertIsNone(self.summary(self.pid, "PE")["avg_rating"])
        self.meal_id(self.pid, "PE")
        self.assertIsNone(self.summary(self.pid, "PE")["avg_rating"], "unrated meals give no average")
        # 4, 4, 4, 5 -> 4.25: rounded half up (exact integer arithmetic) -> 4.3; Python round() would give 4.2
        for rating in (4, 4):
            self.meal_id(self.pid, "PE", rating=rating)
        for rating in (4, 5):
            self.meal_id(self.pid, "PE", self.bob_token, rating=rating)
        self.assertEqual(self.summary(self.pid, "PE")["avg_rating"], 4.3)
        self.assertEqual(self.country(self.pid, "PE")["avg_rating"], 4.3)
        self.assertIn(b'"avg_rating": 4.3,', self.api("GET", f"/api/passports/{self.pid}/countries/PE").body)
        # more boundaries: x.x5 exactly (1.95, 2.05, 4.75) rounds up; repeating decimals round to nearest
        for ratings, expected in (((1, 2, 2), 1.7), ((3, 3), 3.0), ((1, 2), 1.5), ((5, 5, 5, 4), 4.8),
                                  ((1, 1, 2), 1.3), ((2,) * 19 + (3,), 2.1), ((2,) * 19 + (1,), 2.0)):
            with self.subTest(ratings=ratings):
                code = "FR"
                with self.db() as conn:
                    conn.execute("DELETE FROM meals WHERE country_code = ?", (code,))
                for rating in ratings:
                    self.meal_id(self.pid, code, rating=rating)
                self.assertEqual(self.summary(self.pid, code)["avg_rating"], expected)
                self.assertEqual(self.country(self.pid, code)["avg_rating"], expected)

    def test_personal_passport(self):
        listing = self.countries_of(self.personal)
        self.assertEqual(listing["progress"], {"total": 197, "complete": 0, "mine": 0, "active_participants": 1})
        self.meal_id(self.personal, "PE", eaten_on="2026-10-11")
        self.meal_id(self.personal, "PE", eaten_on="2026-09-01")
        self.meal_id(self.personal, "JP")
        listing = self.countries_of(self.personal)
        self.assertEqual(listing["progress"], {"total": 197, "complete": 2, "mine": 2, "active_participants": 1})
        self.assertEqual({c["status"] for c in listing["countries"]}, {"not_started", "complete"})
        pe = next(c for c in listing["countries"] if c["code"] == "PE")
        self.assertEqual((pe["status"], pe["completed_on"], pe["done_by_me"]), ("complete", "2026-09-01", True))
        detail = self.country(self.personal, "PE")
        self.assertEqual([(p["id"], p["is_owner"], p["done"], p["completed_on"]) for p in detail["participants"]],
                         [(1, True, True, "2026-09-01")])

    def test_utf8_output(self):
        meal = self.log_meal(self.pid, "CI", dish="Attiéké \U0001F35C", notes="Très bon")
        self.assertEqual(meal.status, 201)
        resp = self.api("GET", f"/api/passports/{self.pid}/countries/CI")
        self.assertIn("Attiéké \U0001F35C".encode("utf-8"), resp.body)
        self.assertIn("Côte d'Ivoire".encode("utf-8"), resp.body)
        self.assertNotIn(b"\\u", resp.body)
        self.assertEqual(resp.headers["Content-Type"], "application/json; charset=utf-8")


# --- Edit and delete (§6) ---

class EditDeleteTests(MealTestCase):
    def test_edit_replaces_every_field(self):
        meal_id = self.meal_id(self.pid, "PE", dish="Ceviche", place="La Mar", rating=4, notes="Good")
        self.clock_offset = 60
        resp = self.patch(self.pid, meal_id, {"eaten_on": "2026-10-01", "dish": "Lomo", "place": "Home",
                                              "rating": 5, "notes": "Better\r\nstill"})
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(resp.json(), {"meal": {
            "id": meal_id, "user": {"id": 1, "username": "admin", "display_name": "Admin", "inactive": False,
                                    "avatar_seed": self.seed_of(1)},
            "eaten_on": "2026-10-01", "dish": "Lomo", "place": "Home", "rating": 5, "notes": "Better\nstill",
            "mine": True}})
        self.assertEqual(self.query("SELECT country_code, created_at, updated_at FROM meals WHERE id = ?",
                                    (meal_id,)), [("PE", self.base_time, self.base_time + 60)])
        # missing optional fields mean empty / none
        resp = self.patch(self.pid, meal_id, {"eaten_on": "2026-10-02"})
        self.assertEqual(resp.status, 200)
        meal = resp.json()["meal"]
        self.assertEqual((meal["dish"], meal["place"], meal["rating"], meal["notes"]), ("", "", None, ""))
        self.patch(self.pid, meal_id, {"eaten_on": DATE, "rating": 3})
        resp = self.patch(self.pid, meal_id, {"eaten_on": DATE, "rating": None})
        self.assertIsNone(resp.json()["meal"]["rating"])
        self.assertEqual(self.meals_rows("id = ?", (meal_id,))[0][7], None)

    def test_edit_changes_completion_date(self):
        meal_id = self.meal_id(self.pid, "PE", eaten_on="2026-10-11")
        self.patch(self.pid, meal_id, {"eaten_on": "2026-01-02"})
        self.assertEqual(self.country(self.pid, "PE")["participants"][0]["completed_on"], "2026-01-02")

    def test_delete(self):
        first = self.meal_id(self.pid, "PE")
        second = self.meal_id(self.pid, "PE")
        bob_meal = self.meal_id(self.pid, "PE", self.bob_token)
        resp = self.delete_meal(self.pid, first)
        self.assertEqual((resp.status, resp.json()), (200, {"ok": True, "still_done": True}))
        self.assertTrue(self.summary(self.pid, "PE")["done_by_me"])
        resp = self.delete_meal(self.pid, second)
        self.assertEqual((resp.status, resp.json()), (200, {"ok": True, "still_done": False}),
                         "bob's meal doesn't count for the caller")
        pe = self.summary(self.pid, "PE")
        self.assertEqual((pe["done_by_me"], pe["status"], pe["meal_count"]), (False, "some", 1))
        resp = self.delete_meal(self.pid, second)
        self.assertEqual((resp.status, resp.json()), (404, MEAL_NOT_FOUND))
        resp = self.patch(self.pid, second, {"eaten_on": DATE})
        self.assertEqual((resp.status, resp.json()), (404, MEAL_NOT_FOUND))
        self.assertEqual([r[0] for r in self.meals_rows()], [bob_meal])
        resp = self.delete_meal(self.pid, bob_meal, token=self.bob_token)
        self.assertEqual(resp.json(), {"ok": True, "still_done": False})
        self.assertEqual(self.summary(self.pid, "PE")["status"], "not_started")

    def test_delete_body(self):
        meal_id = self.meal_id(self.pid, "PE")
        resp = self.api("POST", f"/api/passports/{self.pid}/meals/{meal_id}/delete", raw_body=b"")
        self.assertEqual(resp.status, 200)
        meal_id = self.meal_id(self.pid, "PE")
        resp = self.api("POST", f"/api/passports/{self.pid}/meals/{meal_id}/delete", {"anything": 1})
        self.assertEqual(resp.status, 200)


class MidRequestTests(MealTestCase):
    """A change made after the caller's session loaded but before the handler's transaction."""

    def setUp(self):
        super().setUp()
        self.mid_request = None
        original = server.Handler.load_session

        def load_session(handler, renew):
            original(handler, renew)
            if self.mid_request is not None and (handler.raw_path or "").startswith("/api/passports/"):
                action, self.mid_request = self.mid_request, None
                action()

        patcher = mock.patch.object(server.Handler, "load_session", load_session)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bob_meal = self.meal_id(self.pid, "PE", self.bob_token, dish="Ceviche")

    def set_bob_role(self, role):
        with self.db() as conn:
            conn.execute("UPDATE passport_members SET role = ? WHERE passport_id = ? AND user_id = ?",
                         (role, self.pid, self.bob))

    def test_switched_to_viewer(self):
        # Breaks the viewer-has-no-meals invariant on purpose, to reach the role re-check; restored below.
        try:
            self.mid_request = lambda: self.set_bob_role("viewer")
            resp = self.patch(self.pid, self.bob_meal, {"eaten_on": DATE, "dish": "Changed"}, token=self.bob_token)
            self.assertIsNone(self.mid_request, "the change ran")
            self.assertEqual((resp.status, resp.error), (403, PARTICIPANTS_ONLY))
            self.assertEqual(self.meals_rows("id = ?", (self.bob_meal,))[0][5], "Ceviche")
            self.set_bob_role("participant")
            self.mid_request = lambda: self.set_bob_role("viewer")
            resp = self.log_meal(self.pid, "FR", token=self.bob_token)
            self.assertEqual((resp.status, resp.error), (403, PARTICIPANTS_ONLY))
            self.assertEqual(self.meal_count("country_code = 'FR'"), 0)
        finally:
            self.set_bob_role("participant")

    def test_removed(self):
        self.mid_request = lambda: self.remove(self.pid, self.bob)
        resp = self.patch(self.pid, self.bob_meal, {"eaten_on": DATE}, token=self.bob_token)
        self.assertEqual((resp.status, resp.json()), (404, NOT_FOUND))
        self.add(self.pid, self.bob, "bob", "participant")
        self.mid_request = lambda: self.remove(self.pid, self.bob)
        resp = self.log_meal(self.pid, "PE", token=self.bob_token)
        self.assertEqual((resp.status, resp.json()), (404, NOT_FOUND))
        self.assertEqual(self.meal_count("user_id = ?", (self.bob,)), 0)

    def test_meal_deleted(self):
        meal_id = self.meal_id(self.pid, "PE", self.bob_token)

        def gone():
            with self.db() as conn:
                conn.execute("DELETE FROM meals WHERE id = ?", (meal_id,))

        self.mid_request = gone
        resp = self.patch(self.pid, meal_id, {"eaten_on": DATE}, token=self.bob_token)
        self.assertEqual((resp.status, resp.json()), (404, MEAL_NOT_FOUND))

    def test_logged_out(self):
        def logout():
            with self.db() as conn:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (self.bob,))

        for call in (lambda: self.log_meal(self.pid, "FR", token=self.bob_token),
                     lambda: self.patch(self.pid, self.bob_meal, {"eaten_on": DATE}, token=self.bob_token),
                     lambda: self.delete_meal(self.pid, self.bob_meal, token=self.bob_token)):
            self.mid_request = logout
            resp = call()
            self.assertEqual((resp.status, resp.error), (401, "Not logged in"))
            self.bob_token = self.login_token("bob")
        self.assertEqual([r[0] for r in self.meals_rows()], [self.bob_meal])


# --- Cascades (§3) ---

class CascadeTests(MealTestCase):
    def setUp(self):
        super().setUp()
        self.bob_personal = self.personal_id(self.bob)
        self.meal_id(self.pid, "PE")
        self.meal_id(self.pid, "PE", self.bob_token)
        self.meal_id(self.pid, "FR", self.bob_token)
        self.meal_id(self.bob_personal, "PE", self.bob_token)

    def bob_meals(self):
        return self.query("SELECT passport_id, country_code FROM meals WHERE user_id = ? ORDER BY id", (self.bob,))

    def test_remove(self):
        self.assertEqual(self.remove(self.pid, self.bob).status, 200)
        self.assertEqual(self.bob_meals(), [(self.bob_personal, "PE")], "only that passport's meals")
        self.assertEqual(self.meal_count("user_id = 1"), 1)
        self.assertEqual(self.summary(self.pid, "PE")["status"], "complete")

    def test_leave(self):
        self.assertEqual(self.leave(self.pid, token=self.bob_token).status, 200)
        self.assertEqual(self.bob_meals(), [(self.bob_personal, "PE")])
        self.assertEqual(self.meal_count("user_id = 1"), 1)

    def test_remove_and_re_add_starts_from_nothing(self):
        self.remove(self.pid, self.bob)
        self.add(self.pid, self.bob, "bob", "participant")
        self.assertFalse(self.summary(self.pid, "FR", token=self.bob_token)["done_by_me"])

    def test_passport_delete(self):
        self.assertEqual(self.delete(self.pid, "Trip").status, 200)
        self.assertEqual(self.meal_count("passport_id = ?", (self.pid,)), 0)
        self.assertEqual(self.bob_meals(), [(self.bob_personal, "PE")])

    def test_disable_keeps_meals(self):
        before = self.meals_rows()
        self.assertEqual(self.admin_action(self.bob, "disable").status, 200)
        self.assertEqual(self.meals_rows(), before)

    def assert_no_trace(self, user_id):
        self.assertEqual(self.query("SELECT id FROM meals WHERE user_id = ?", (user_id,)), [])
        self.assertEqual(self.query("SELECT id FROM passport_members WHERE user_id = ?", (user_id,)), [])
        self.assertEqual(self.query("SELECT id FROM passports WHERE owner_id = ?", (user_id,)), [])

    def test_admin_delete(self):
        self.admin_delete(self.bob, "bob")
        self.assert_no_trace(self.bob)
        self.assertEqual(self.meal_count(), 1)
        self.assertEqual(self.summary(self.pid, "PE")["status"], "complete")

    def test_self_delete(self):
        self.self_delete(self.bob, self.bob_token)
        self.assert_no_trace(self.bob)
        self.assertEqual(self.meal_count(), 1)

    def test_handed_over_passport_on_delete(self):
        carol, carol_token = self.user("carol")
        shared = self.create_id("Bob's trip", [ref(carol, "carol")], token=self.bob_token)
        self.meal_id(shared, "JP", self.bob_token)
        self.meal_id(shared, "JP", carol_token)
        self.admin_delete(self.bob, "bob")
        self.assertEqual(self.owner_of(shared), carol)
        self.assertEqual(self.query("SELECT user_id FROM meals WHERE passport_id = ?", (shared,)), [(carol,)])
        self.assert_no_trace(self.bob)
        jp = self.summary(shared, "JP", token=carol_token)
        self.assertEqual((jp["status"], jp["meal_count"]), ("complete", 1))

    def test_id_reuse(self):
        dan, dan_token = self.user("dan")  # the highest id
        self.add(self.pid, dan, "dan", "participant")
        self.meal_id(self.pid, "IT", dan_token)
        self.meal_id(self.personal_id(dan), "IT", dan_token)
        self.admin_delete(dan, "dan")
        resp = self.api("POST", "/api/admin/users", {"username": "newbie", "display_name": "N",
                                                     "password": "temporary password 1"})
        new_id = resp.json()["user"]["id"]
        self.assertEqual(new_id, dan, "SQLite reused the id")
        self.assertEqual(self.query("SELECT id FROM meals WHERE user_id = ?", (new_id,)), [])
        self.assertEqual(self.summary(self.pid, "IT")["status"], "not_started")


# --- Limits (§1, §6) ---

class LimitTests(MealTestCase):
    def test_per_country(self):
        self.seed(self.pid, 1, "PE", 99)
        self.assertEqual(self.log_meal(self.pid, "PE").status, 201)
        resp = self.log_meal(self.pid, "PE")
        self.assertEqual((resp.status, resp.error), (409, PER_COUNTRY))
        self.assertEqual(self.meal_count("user_id = 1 AND country_code = 'PE'"), 100)
        # other countries, other passports and other people are unaffected
        self.assertEqual(self.log_meal(self.pid, "FR").status, 201)
        self.assertEqual(self.log_meal(self.personal, "PE").status, 201)
        self.assertEqual(self.log_meal(self.pid, "PE", token=self.bob_token).status, 201)
        # editing doesn't count against the limit
        meal_id = self.query("SELECT id FROM meals WHERE user_id = 1 AND country_code = 'PE' LIMIT 1")[0][0]
        self.assertEqual(self.patch(self.pid, meal_id, {"eaten_on": DATE}).status, 200)

    def test_per_passport(self):
        self.seed(self.pid, 1, "FR", 5000)
        resp = self.log_meal(self.pid, "PE")
        self.assertEqual((resp.status, resp.error), (409, PER_PASSPORT))
        self.assertEqual(self.log_meal(self.personal, "PE").status, 201)
        self.assertEqual(self.log_meal(self.pid, "PE", token=self.bob_token).status, 201, "only the caller's count")

    def test_per_account(self):
        other = self.create_id("Other")
        self.seed(other, 1, "FR", 20000)
        for pid in (self.pid, self.personal):
            resp = self.log_meal(pid, "PE")
            self.assertEqual((resp.status, resp.error), (409, PER_ACCOUNT))
        self.assertEqual(self.log_meal(self.pid, "PE", token=self.bob_token).status, 201, "only the caller's count")
        self.assertEqual(self.meal_count("user_id = 1"), 20000)

    def test_limit_order(self):
        self.seed(self.pid, 1, "PE", 100)
        self.seed(self.pid, 1, "FR", 4900)
        other = self.create_id("Other")
        self.seed(other, 1, "FR", 15000)
        self.assertEqual(self.log_meal(self.pid, "PE").error, PER_COUNTRY)
        self.assertEqual(self.log_meal(self.pid, "JP").error, PER_PASSPORT)
        self.assertEqual(self.log_meal(self.personal, "JP").error, PER_ACCOUNT)

    def test_concurrent_at_99(self):
        self.seed(self.pid, 1, "PE", 99)
        results = race([lambda: self.log_meal(self.pid, "PE") for _ in range(4)])
        self.assertEqual(sorted(results), [201, 409, 409, 409])
        self.assertEqual(self.meal_count("user_id = 1 AND country_code = 'PE'"), 100)

    def test_concurrent_at_4999(self):
        self.seed(self.pid, 1, "FR", 4999)
        codes = ["PE", "JP", "IT", "MX"]
        results = race([lambda code=code: self.log_meal(self.pid, code) for code in codes])
        self.assertEqual(sorted(results), [201, 409, 409, 409])
        self.assertEqual(self.meal_count("user_id = 1 AND passport_id = ?", (self.pid,)), 5000)


# --- Efficiency and logging ---

class EfficiencyTests(MealTestCase):
    def count_list_queries(self, token="default"):
        statements = []
        original = db.connect

        def connect(path):
            conn = original(path)
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(db, "connect", connect):
            resp = self.api("GET", f"/api/passports/{self.pid}/countries", token=token)
        self.assertEqual(resp.status, 200)
        return len(statements)

    def test_list_queries_are_bounded(self):
        empty = self.count_list_queries()
        self.assertGreaterEqual(empty, 4, "the trace hook saw the request's statements")
        for code in [c.code for c in countries.COUNTRIES][:60]:
            self.seed(self.pid, 1, code, 3)
            self.seed(self.pid, self.bob, code, 2)
        many = self.count_list_queries()
        self.assertEqual(many, empty, "same number of statements regardless of meals")
        self.assertLessEqual(many, 12)
        self.assertEqual(self.count_list_queries(token=self.sam_token), empty)


class LoggingTests(MealTestCase):
    def test_no_meal_details_logged(self):
        meal_id = self.meal_id(self.pid, "PE", dish="SecretDish", place="SecretPlace", notes="SecretNotes\nmore",
                               rating=5)
        self.patch(self.pid, meal_id, {"eaten_on": DATE, "dish": "SecretEdit", "notes": "SecretNotes2"})
        self.log_meal(self.pid, "PE", dish="x" * 101)
        self.log_meal(self.pid, "PE", token=self.sam_token, dish="SecretViewer")
        self.countries_of(self.pid)
        self.country(self.pid, "PE")
        self.country(self.pid, "CI")
        self.api("GET", "/api/countries")
        self.delete_meal(self.pid, meal_id)
        log = self.log.getvalue()
        for text in ("Secret", "Peru", "Ivoire", "South America", "Trip", "Ceviche"):
            self.assertNotIn(text, log)
        self.assertIn(f"POST /api/passports/{self.pid}/countries/PE/meals 201", log)
