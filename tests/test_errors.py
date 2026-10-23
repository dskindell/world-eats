"""Change 10: error codes and params (contract 10 §5, §12).

Every code in errors.MESSAGES has a test named test_<code> that reaches it over
HTTP and checks status, code, params and the English text; CatalogueTests
checks that no code is left without one.
"""

import re
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import PASSWORD, ServerTestCase, auth, passports, server
from tests.jpeg import CHROME_PHOTO
from tests.test_passports import PassportTestCase, ref

import errors  # noqa: E402  (app/ is on sys.path via tests.helpers)
import meals  # noqa: E402
import search  # noqa: E402

CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")
PARAM_KEY_RE = re.compile(r"[a-z][a-zA-Z0-9]*")
DATE = "2026-01-15"
TEMP = "temporary password 1"


class ErrorAssertions:
    def assert_code(self, resp, status, code, params=None, extra=None):
        """The whole error body: error == render(code, params), code, params (absent when empty), extra."""
        self.assertEqual(resp.status, status, resp)
        body = resp.json()
        expected = {"error": errors.render(code, params), "code": code}
        if params:
            expected["params"] = params
        expected.update(extra or {})
        self.assertEqual(body, expected)
        self.assert_well_formed(body)

    def assert_well_formed(self, body):
        self.assertIsInstance(body["error"], str)
        self.assertRegex(body["code"], CODE_RE)
        self.assertTrue(CODE_RE.fullmatch(body["code"]))
        self.assertEqual(body["error"], errors.render(body["code"], body.get("params")))
        if "params" in body:
            self.assertTrue(body["params"])
            for key, value in body["params"].items():
                self.assertTrue(PARAM_KEY_RE.fullmatch(key), key)
                self.assertIs(type(value), int, key)


# --- The catalogue itself ---

class CatalogueTests(unittest.TestCase):
    def test_codes_and_templates(self):
        self.assertEqual(len(set(errors.MESSAGES.values())), len(errors.MESSAGES), "one code per distinct text")
        for code, template in errors.MESSAGES.items():
            with self.subTest(code=code):
                self.assertTrue(CODE_RE.fullmatch(code))
                # No literal braces: removing placeholders leaves none.
                self.assertNotRegex(errors.PLACEHOLDER_RE.sub("", template), r"[{}]")
                self.assertNotIn("{{", template)
                self.assertNotIn("}}", template)

    def test_every_code_is_tested(self):
        tested = {name[len("test_"):] for cls in (CodeTests, NoUserCodeTests) for name in dir(cls)
                  if name.startswith("test_")}
        self.assertEqual(sorted(set(errors.MESSAGES) - tested), [])
        self.assertEqual(sorted(tested - set(errors.MESSAGES)), [])

    def test_send_error_codes(self):
        # The codes the stdlib's send_error() can produce; each is also in CodeTests.
        self.assertEqual(server.STDLIB_ERRORS, {400: "bad_request", 431: "headers_too_large", 505: "http_version"})

    def test_render(self):
        self.assertEqual(errors.render("people_limit", {"max": 20}), "A passport can have at most 20 people")
        self.assertEqual(errors.render("not_found"), "Not found")
        self.assertEqual(errors.render("not_found", {}), "Not found")
        self.assertEqual(errors.render("password_length", {"min": 12, "max": 128}),
                         "Password must be 12-128 characters")
        self.assertEqual(errors.render("passport_limit", {"max": 12345}), "You can own at most 12345 passports")

    def test_render_rejects_bad_params(self):
        for code, params in (("people_limit", None), ("people_limit", {}), ("people_limit", {"max": "20"}),
                             ("people_limit", {"max": True}), ("people_limit", {"max": 2.0}),
                             ("people_limit", {"max": 20, "min": 1}), ("not_found", {"max": 1}),
                             ("no_such_code", None)):
            with self.subTest(code=code, params=params):
                with self.assertRaises(ValueError):
                    errors.render(code, params)

    def test_render_is_one_pass(self):
        # Grammar check with a template that has braces (the catalogue has none).
        with mock.patch.dict(errors.MESSAGES, {"x_test": "{{a}} {n} {{ {n}}}"}):
            self.assertEqual(errors.render("x_test", {"n": 7}), "{a} 7 { 7}")

    def test_body(self):
        self.assertEqual(errors.body("not_found"), {"error": "Not found", "code": "not_found"})
        self.assertEqual(errors.body("people_limit", {"max": 20}),
                         {"error": "A passport can have at most 20 people", "code": "people_limit",
                          "params": {"max": 20}})
        self.assertEqual(list(errors.body("password_change_required", None, {"password_change_required": True})),
                         ["error", "code", "password_change_required"])
        for key in ("error", "code", "params"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    errors.body("not_found", None, {key: "x"})
                with self.assertRaises(ValueError):
                    server.ApiError(400, "not_found", extra={key: "x"})

    def test_exceptions_carry_the_text(self):
        e = server.ApiError(409, "people_limit", {"max": 20})
        self.assertEqual((e.status, e.code, str(e)), (409, "people_limit", "A passport can have at most 20 people"))
        e = auth.Invalid("password_length", auth.PASSWORD_PARAMS)
        self.assertEqual(str(e), "Password must be 12-128 characters")
        self.assertEqual(e.body["params"], {"min": 12, "max": 128})
        with self.assertRaises(ValueError):
            auth.Invalid("Password must be 12-128 characters")  # a message is not a code

    def test_params_follow_the_constants(self):
        self.assertEqual(auth.USERNAME_PARAMS, {"min": 3, "max": 32})
        self.assertEqual(auth.NAME_PARAMS, {"min": 1, "max": 50})
        self.assertEqual(auth.PASSWORD_PARAMS, {"min": 12, "max": 128})
        self.assertEqual(meals.DATE_PARAMS, {"year": meals.FIRST_DATE.year})
        self.assertEqual(meals.TEXT_PARAMS, {"max": meals.MAX_TEXT})
        self.assertEqual(meals.RATING_PARAMS, {"min": 1, "max": 5})
        self.assertEqual(auth.USERNAME_RE.pattern, "[A-Za-z0-9._-]{3,32}")

    def test_literal_numbers_match_the_constants(self):
        # Numbers with a thousands separator are literal in the template (review L1).
        for code, value in (("notes_length", meals.MAX_NOTES), ("meal_limit_passport", meals.MAX_PER_PASSPORT),
                            ("meal_limit_account", meals.MAX_PER_ACCOUNT)):
            with self.subTest(code=code):
                self.assertIn(f"{value:,}", errors.MESSAGES[code])
                self.assertEqual(errors.placeholders(errors.MESSAGES[code]), [])

    def test_no_message_strings_left_in_the_server(self):
        # Every ApiError/Invalid takes a code (contract 10 §5); no f-string or English message.
        app_dir = Path(server.__file__).parent
        pattern = re.compile(r"\b(?:ApiError|Invalid)\(\s*(?:\d+\s*,\s*)?(f?)([\"'])")
        for path in app_dir.glob("*.py"):
            if path.name == "errors.py":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for match in pattern.finditer(line):
                    with self.subTest(file=path.name, line=number):
                        self.assertEqual(match.group(1), "", "no f-string messages")
                        self.assertRegex(line[match.start(2):], r"^[\"'][a-z][a-z0-9_]*[\"']")
            self.assertNotIn("str(e)", path.read_text(encoding="utf-8"), path.name)


# --- Every code over HTTP ---

class NoUserCodeTests(ErrorAssertions, ServerTestCase):
    def test_setup_required(self):
        self.assert_code(self.login(), 409, "setup_required")


class CodeTests(ErrorAssertions, PassportTestCase):
    """Admin "admin" (id 1, self.token, personal passport 1)."""

    def raw_post(self, path, extra_headers=(), body=b""):
        headers = [("Origin", self.origin), ("Content-Type", "application/json")] + list(extra_headers)
        return self.raw_request("POST", path, headers=headers, body=body)

    def shared(self):
        """A passport owned by admin with bob as participant and vic as viewer: (pid, bob, bob_token, vic_token)."""
        bob, bob_token = self.user("bob")
        vic, vic_token = self.user("vic")
        pid = self.create_id("Shared", participants=[ref(bob, "bob")], viewers=[ref(vic, "vic")])
        return pid, bob, bob_token, vic_token

    def log_meal(self, pid, code="PE", token="default", **fields):
        body = {"eaten_on": DATE, **fields}
        return self.api("POST", f"/api/passports/{pid}/countries/{code}/meals", body, token=token)

    def seed(self, pid, user_id, code, count):
        with self.db() as conn:
            conn.executemany(
                "INSERT INTO meals (passport_id, user_id, country_code, eaten_on, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, 1)", [(pid, user_id, code, DATE)] * count)

    def create_user(self, **fields):
        body = {"username": "carol", "display_name": "Carol", "password": TEMP, **fields}
        return self.api("POST", "/api/admin/users", body)

    # Request handling

    def test_bad_request(self):
        self.assert_code(self.raw_request("GET", "/api/session", headers=[("Content-Length", "3")], body=b"abc"),
                         400, "bad_request")
        self.assert_code(self.raw_request("GET", "/", headers=[("Host", self.host)]), 400, "bad_request")
        self.assert_code(self.raw(b"GARBAGE\r\n\r\n"), 400, "bad_request")  # stdlib send_error

    def test_headers_too_large(self):
        raw = b"GET / HTTP/1.0\r\n" + b"".join(b"X-%d: 1\r\n" % i for i in range(51)) + b"\r\n"
        self.assert_code(self.raw(raw), 431, "headers_too_large")

    def test_http_version(self):
        self.assert_code(self.raw(b"GET / HTTP/2.0\r\n\r\n"), 505, "http_version")

    def test_unknown_host(self):
        self.assert_code(self.raw_request("GET", "/api/session", host="evil.example"), 421, "unknown_host")

    def test_method_not_allowed(self):
        self.assert_code(self.raw_request("DELETE", "/api/session"), 405, "method_not_allowed")
        self.assert_code(self.api("POST", "/api/session"), 405, "method_not_allowed")
        self.assert_code(self.api("POST", "/style.css"), 405, "method_not_allowed")

    def test_length_required(self):
        self.assert_code(self.raw_request("GET", "/", headers=[("Transfer-Encoding", "chunked")]),
                         411, "length_required")
        self.assert_code(self.raw_post("/api/logout"), 411, "length_required")

    def test_invalid_content_length(self):
        self.assert_code(self.raw_post("/api/logout", [("Content-Length", "-1")]), 400, "invalid_content_length")

    def test_cross_origin(self):
        self.assert_code(self.api("POST", "/api/logout", origin="http://evil.example"), 403, "cross_origin")

    def test_content_type_json(self):
        self.assert_code(self.api("POST", "/api/logout", content_type="text/plain"), 415, "content_type_json")

    def test_too_large(self):
        self.assert_code(self.raw_post("/api/logout", [("Content-Length", "16385")]), 413, "too_large")

    def test_invalid_json(self):
        self.assert_code(self.api("POST", "/api/logout", raw_body=b"{"), 400, "invalid_json")
        self.assert_code(self.api("POST", "/api/logout", raw_body=b"[]"), 400, "invalid_json")

    def test_not_found(self):
        self.assert_code(self.api("GET", "/api/nope"), 404, "not_found")
        self.assert_code(self.api("GET", "/nope.js"), 404, "not_found")

    def test_internal_error(self):
        def boom(req):
            raise ValueError("secret detail")  # not a 400: str() of an exception never reaches a response
        self.add_route("/api/boom", {"GET": server.Route(boom, public=True)})
        resp = self.api("GET", "/api/boom")
        self.assert_code(resp, 500, "internal_error")
        self.assertNotIn(b"secret", resp.body)

    def test_busy(self):
        self.app.hasher.timeout = 0.2
        sem = self.app.hasher.semaphore
        sem.acquire()
        sem.acquire()
        try:
            resp = self.login()
        finally:
            sem.release()
            sem.release()
        self.assert_code(resp, 503, "busy")
        slots = self.app.search_slots
        for _ in range(server.SEARCH_SLOTS):
            slots.acquire()
        try:
            self.assert_code(self.api("POST", "/api/passports/1/search", {"q": "peru"}), 503, "busy")
        finally:
            for _ in range(server.SEARCH_SLOTS):
                slots.release()

    # Sessions, setup, login and account settings

    def test_not_logged_in(self):
        self.assert_code(self.api("GET", "/api/passports", token=None), 401, "not_logged_in")

    def test_password_change_required(self):
        self.assertEqual(self.create_user().status, 201)
        token = self.login_token("carol", TEMP)
        self.assert_code(self.api("GET", "/api/passports", token=token), 403, "password_change_required",
                         extra={"password_change_required": True})

    def test_admins_only(self):
        _, bob_token = self.user("bob")
        self.assert_code(self.api("GET", "/api/admin/users", token=bob_token), 403, "admins_only")

    def test_setup_complete(self):
        body = {"username": "other", "display_name": "Other", "password": PASSWORD}
        self.assert_code(self.api("POST", "/api/setup", body, token=None), 409, "setup_complete")

    def test_too_many_attempts(self):
        for _ in range(5):
            self.assertEqual(self.login(password="wrong password!").status, 401)
        self.assert_code(self.login(), 429, "too_many_attempts")

    def test_wrong_login(self):
        self.assert_code(self.login(password="wrong password!"), 401, "wrong_login")
        self.assert_code(self.login(username="nobody"), 401, "wrong_login")

    def test_nothing_to_update(self):
        self.assert_code(self.api("PATCH", "/api/account", {}), 400, "nothing_to_update")
        self.assert_code(self.api("PATCH", "/api/passports/1", {}), 400, "nothing_to_update")
        self.assert_code(self.api("PATCH", "/api/admin/users/1", {}), 400, "nothing_to_update")

    def test_current_password_wrong(self):
        body = {"current_password": "wrong password!", "new_password": "a brand new password"}
        self.assert_code(self.api("POST", "/api/account/password", body), 403, "current_password_wrong")

    def test_password_unchanged(self):
        body = {"current_password": PASSWORD, "new_password": PASSWORD}
        self.assert_code(self.api("POST", "/api/account/password", body), 400, "password_unchanged")

    def test_password_wrong(self):
        self.assert_code(self.api("POST", "/api/account/delete", {"password": "wrong password!"}),
                         403, "password_wrong")

    def test_only_admin_delete(self):
        self.assert_code(self.api("POST", "/api/account/delete", {"password": PASSWORD}), 409, "only_admin_delete")

    def test_username_invalid(self):
        for value in ("ab", "a" * 33, 5, None):
            with self.subTest(value=value):
                self.assert_code(self.create_user(username=value), 400, "username_invalid", {"min": 3, "max": 32})

    def test_display_name_length(self):
        for value in ("", " ", "x" * 51, 7):
            with self.subTest(value=value):
                self.assert_code(self.api("PATCH", "/api/account", {"display_name": value}),
                                 400, "display_name_length", {"min": 1, "max": 50})

    def test_display_name_chars(self):
        self.assert_code(self.api("PATCH", "/api/account", {"display_name": "a​b"}), 400, "display_name_chars")

    def test_password_length(self):
        for value in ("x" * 11, "x" * 129, None):
            with self.subTest(value=value):
                self.assert_code(self.create_user(password=value), 400, "password_length", {"min": 12, "max": 128})

    # Admin screen

    def test_user_not_found(self):
        self.assert_code(self.api("PATCH", "/api/admin/users/999", {"display_name": "X"}), 404, "user_not_found")
        self.assert_code(self.api("POST", "/api/admin/users/999/unlock"), 404, "user_not_found")

    def test_is_admin_invalid(self):
        self.assert_code(self.create_user(is_admin="yes"), 400, "is_admin_invalid")

    def test_username_taken(self):
        self.assert_code(self.create_user(username="ADMIN"), 409, "username_taken")

    def test_reset_own_password(self):
        self.assert_code(self.api("POST", "/api/admin/users/1/password", {"new_password": TEMP}),
                         403, "reset_own_password")

    def test_disable_self(self):
        self.assert_code(self.api("POST", "/api/admin/users/1/disable"), 403, "disable_self")

    def test_last_admin_disable(self):
        # Defensive: the caller is an admin and can't target themselves, so only reachable this way.
        bob, _ = self.user("bob")
        with mock.patch.object(server, "is_last_usable_admin", return_value=True):
            self.assert_code(self.api("POST", f"/api/admin/users/{bob}/disable"), 409, "last_admin_disable")

    def test_only_admin_demote(self):
        self.assert_code(self.api("POST", "/api/admin/users/1/demote"), 409, "only_admin_demote")

    def test_delete_self(self):
        self.assert_code(self.api("POST", "/api/admin/users/1/delete", {"confirm_username": "admin"}),
                         403, "delete_self")

    def test_confirm_username(self):
        bob, _ = self.user("bob")
        self.assert_code(self.api("POST", f"/api/admin/users/{bob}/delete", {"confirm_username": "bobby"}),
                         400, "confirm_username")

    def test_last_admin_delete(self):
        # Defensive, as for last_admin_disable.
        bob, _ = self.user("bob")
        with mock.patch.object(server, "is_last_usable_admin", return_value=True):
            self.assert_code(self.api("POST", f"/api/admin/users/{bob}/delete", {"confirm_username": "bob"}),
                             409, "last_admin_delete")

    # Passports

    def test_passport_name_length(self):
        for value in ("", "x" * 51, None):
            with self.subTest(value=value):
                self.assert_code(self.create(value), 400, "passport_name_length", {"min": 1, "max": 50})

    def test_passport_name_chars(self):
        self.assert_code(self.create("a​b"), 400, "passport_name_chars")

    def test_choose_people(self):
        self.assert_code(self.create(participants=["bob"]), 400, "choose_people")
        self.assert_code(self.create(participants=[ref(999, "nobody")]), 400, "choose_people")

    def test_choose_passport(self):
        self.assert_code(self.set_default("1"), 400, "choose_passport")
        self.assert_code(self.set_default(999), 400, "choose_passport")

    def test_passport_not_found(self):
        self.assert_code(self.get(999), 404, "passport_not_found")

    def test_owner_only(self):
        pid, _, bob_token, _ = self.shared()
        self.assert_code(self.api("PATCH", f"/api/passports/{pid}", {"name": "Mine"}, token=bob_token),
                         403, "owner_only")

    def test_person_repeated(self):
        self.assert_code(self.create(participants=[ref(1, "admin")]), 400, "person_repeated")

    def test_passport_limit(self):
        for i in range(passports.MAX_OWNED - 1):
            self.insert_passport(1, f"P{i}")
        self.assert_code(self.create(), 409, "passport_limit", {"max": 50})

    def test_people_limit(self):
        people = [ref(100 + i, f"user{i}") for i in range(passports.MAX_PEOPLE)]
        self.assert_code(self.create(participants=people[:19], viewers=people[19:]), 409, "people_limit",
                         {"max": 20})

    def test_personal_passport_delete(self):
        self.assert_code(self.delete(1, "My passport"), 409, "personal_passport_delete")

    def test_confirm_passport_name(self):
        pid = self.create_id()
        self.assert_code(self.delete(pid, "date nights"), 400, "confirm_passport_name")

    def test_role_invalid(self):
        bob, _ = self.user("bob")
        self.assert_code(self.add(self.create_id(), bob, "bob", "owner"), 400, "role_invalid")

    def test_personal_passport_participants(self):
        bob, _ = self.user("bob")
        self.assert_code(self.add(1, bob, "bob", "participant"), 409, "personal_passport_participants")

    def test_already_member(self):
        pid, bob, _, _ = self.shared()
        self.assert_code(self.add(pid, bob, "bob", "viewer"), 409, "already_member")

    def test_owner_remove(self):
        self.assert_code(self.remove(self.create_id(), 1), 409, "owner_remove")

    def test_not_member(self):
        self.assert_code(self.remove(self.create_id(), 999), 404, "not_member")

    def test_owner_leave(self):
        self.assert_code(self.leave(self.create_id()), 409, "owner_leave")

    # Countries and meals

    def test_country_not_found(self):
        self.assert_code(self.api("GET", "/api/passports/1/countries/ZZ"), 404, "country_not_found")

    def test_meal_not_found(self):
        self.assert_code(self.api("PATCH", "/api/passports/1/meals/999", {"eaten_on": DATE}), 404, "meal_not_found")

    def test_participants_only(self):
        pid, _, _, vic_token = self.shared()
        self.assert_code(self.log_meal(pid, token=vic_token), 403, "participants_only")

    def test_own_meals_only(self):
        pid, _, bob_token, _ = self.shared()
        meal_id = self.log_meal(pid).json()["meal"]["id"]
        self.assert_code(self.api("PATCH", f"/api/passports/{pid}/meals/{meal_id}", {"eaten_on": DATE},
                                  token=bob_token), 403, "own_meals_only")

    def test_date_range(self):
        for value in (None, "1899-12-31", "2026-02-30", "15/01/2026"):
            with self.subTest(value=value):
                self.assert_code(self.log_meal(1, eaten_on=value), 400, "date_range", {"year": 1900})

    def test_dish_length(self):
        self.assert_code(self.log_meal(1, dish="x" * 101), 400, "dish_length", {"max": 100})

    def test_dish_chars(self):
        self.assert_code(self.log_meal(1, dish="a​b"), 400, "dish_chars")

    def test_place_length(self):
        self.assert_code(self.log_meal(1, place=5), 400, "place_length", {"max": 100})

    def test_place_chars(self):
        self.assert_code(self.log_meal(1, place="a\x00b"), 400, "place_chars")

    def test_rating_range(self):
        for value in (0, 6, True, 4.5):
            with self.subTest(value=value):
                self.assert_code(self.log_meal(1, rating=value), 400, "rating_range", {"min": 1, "max": 5})

    def test_notes_length(self):
        self.assert_code(self.log_meal(1, notes="x" * 2001), 400, "notes_length")

    def test_notes_chars(self):
        self.assert_code(self.log_meal(1, notes="a b"), 400, "notes_chars")

    def test_meal_limit_country(self):
        self.seed(1, 1, "PE", meals.MAX_PER_COUNTRY)
        self.assert_code(self.log_meal(1), 409, "meal_limit_country", {"max": 100})

    def test_meal_limit_passport(self):
        for code in ("FR", "JP", "IT", "MX", "TH"):
            self.seed(1, 1, code, 1000)
        self.assert_code(self.log_meal(1), 409, "meal_limit_passport")

    def test_meal_limit_account(self):
        other = self.create_id()
        self.seed(1, 1, "FR", meals.MAX_PER_ACCOUNT)
        self.assert_code(self.log_meal(other), 409, "meal_limit_account")

    # Search

    def search(self, q):
        return self.api("POST", "/api/passports/1/search", {"q": q})

    def test_search_empty(self):
        for value in ("", "   ", None, 5):
            with self.subTest(value=value):
                self.assert_code(self.search(value), 400, "search_empty")

    def test_search_length(self):
        self.assert_code(self.search("x" * 201), 400, "search_length", {"max": search.MAX_QUERY})

    def test_search_term_length(self):
        self.assert_code(self.search("x" * 51), 400, "search_term_length", {"max": search.MAX_TERM})

    # Next stops

    def next_stop(self, pid, body, code="PE", token="default"):
        return self.api("POST", f"/api/passports/{pid}/countries/{code}/next-stops", body, token=token)

    def test_next_stop_list_invalid(self):
        self.assert_code(self.next_stop(1, {"list": "Mine", "on": True}), 400, "next_stop_list_invalid")

    def test_next_stop_on_invalid(self):
        self.assert_code(self.next_stop(1, {"list": "mine", "on": 1}), 400, "next_stop_on_invalid")

    def test_personal_no_shared(self):
        self.assert_code(self.next_stop(1, {"list": "ours", "on": True}), 409, "personal_no_shared")

    def test_next_stop_done(self):
        self.log_meal(1)
        self.assert_code(self.next_stop(1, {"list": "mine", "on": True}), 409, "next_stop_done")

    def test_next_stop_complete(self):
        pid, _, bob_token, _ = self.shared()
        self.log_meal(pid)
        self.log_meal(pid, token=bob_token)
        self.assert_code(self.next_stop(pid, {"list": "ours", "on": True}), 409, "next_stop_complete")

    # Profile photos

    def photo_upload(self, data=CHROME_PHOTO, content_type="image/jpeg"):
        return self.request("POST", "/api/account/photo", raw_body=data, token=self.token, content_type=content_type)

    def test_content_type_jpeg(self):
        self.assert_code(self.photo_upload(content_type="text/plain"), 415, "content_type_jpeg")

    def test_invalid_image(self):
        self.assert_code(self.photo_upload(b"\xff\xd8 not a jpeg"), 400, "invalid_image")
        self.assert_code(self.photo_upload(b""), 400, "invalid_image")

    def test_photo_not_found(self):
        self.assert_code(self.api("GET", "/api/photos/" + "A" * 43), 404, "photo_not_found")

    def test_too_many_uploads(self):
        for _ in range(server.UPLOAD_LIMIT):
            self.assertEqual(self.photo_upload().status, 200)
        self.assert_code(self.photo_upload(), 429, "too_many_uploads")


# --- The new static file (contract 10 §2) ---

class StringsFileServingTests(ServerTestCase):
    def test_served_like_app_js(self):
        Path(self.static_dir, "strings-en.js").write_bytes(b"'use strict';\n")
        resp = self.request("GET", "/strings-en.js")
        app_js = self.request("GET", "/app.js")
        self.assertEqual((resp.status, resp.body), (200, b"'use strict';\n"))
        self.assertEqual(resp.headers["Content-Type"], "text/javascript; charset=utf-8")
        for name in ("Content-Type", "Cache-Control", *dict(server.SECURITY_HEADERS)):
            self.assertEqual(resp.headers[name], app_js.headers[name], name)
        self.assertEqual(server.STATIC_FILES["/strings-en.js"], ("strings-en.js", "text/javascript; charset=utf-8"))

    def test_missing_file(self):
        resp = self.request("GET", "/strings-en.js")
        self.assertEqual((resp.status, resp.json()), (404, {"error": "Not found", "code": "not_found"}))

    def test_no_other_names(self):
        Path(self.static_dir, "strings-fr.js").write_bytes(b"x")
        for path in ("/strings-fr.js", "/strings-en.js/", "/STRINGS-EN.JS", "/static/strings-en.js"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path).status, 404)


if __name__ == "__main__":
    unittest.main()
