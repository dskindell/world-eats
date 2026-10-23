"""Change 11b: next stops lists (contract 11b §2-§4, §6, §7)."""

from tests.helpers import PASSWORD
from tests.test_errors import ErrorAssertions
from tests.test_meals import SUMMARY_KEYS, MealTestCase
from tests.test_passports import NOT_FOUND, PENDING, ref

import meals  # noqa: E402  (app/ is on sys.path via tests.helpers)

RESPONSE_KEYS = {"next_stops", "my_next_stop", "summary"}


class NextStopTestCase(ErrorAssertions, MealTestCase):
    """Admin (id 1, owner), bob (participant), sam (viewer), eve (outsider), shared passport "Trip"."""

    def stop(self, pid, code, which, on, token="default", **extra):
        return self.api("POST", f"/api/passports/{pid}/countries/{code}/next-stops",
                        {"list": which, "on": on, **extra}, token=token)

    def set_stop(self, pid, code, which, on, token="default"):
        resp = self.stop(pid, code, which, on, token)
        self.assertEqual(resp.status, 200, resp)
        body = resp.json()
        self.assertEqual(set(body), RESPONSE_KEYS)
        return body

    def person(self, user_id, inactive=False):
        username, display_name, seed, photo = self.query(
            "SELECT username, display_name, avatar_seed, photo_token FROM users WHERE id = ?", (user_id,))[0]
        return {"id": user_id, "username": username, "display_name": display_name, "inactive": inactive,
                "avatar_seed": seed, "photo": photo}

    def own_rows(self, pid=None):
        if pid is None:
            return self.query("SELECT passport_id, user_id, country_code, created_at FROM next_stops ORDER BY id")
        return self.query("SELECT user_id, country_code, created_at FROM next_stops WHERE passport_id = ? "
                          "ORDER BY id", (pid,))

    def shared_rows(self, pid=None):
        if pid is None:
            return self.query("SELECT passport_id, country_code, added_by, created_at FROM shared_next_stops "
                              "ORDER BY id")
        return self.query("SELECT country_code, added_by, created_at FROM shared_next_stops WHERE passport_id = ? "
                          "ORDER BY id", (pid,))

    def insert_own(self, pid, user_id, code):
        with self.db() as conn:
            conn.execute("INSERT INTO next_stops (passport_id, user_id, country_code, created_at) "
                         "VALUES (?, ?, ?, 1)", (pid, user_id, code))

    def insert_shared(self, pid, code, added_by):
        with self.db() as conn:
            conn.execute("INSERT INTO shared_next_stops (passport_id, country_code, added_by, created_at) "
                         "VALUES (?, ?, ?, 1)", (pid, code, added_by))

    def stops(self, pid, code, token="default"):
        detail = self.country(pid, code, token)
        return detail["my_next_stop"], detail["next_stops"]

    def enable(self, user_id):
        self.assertEqual(self.admin_action(user_id, "enable").status, 200)

    def disable(self, user_id):
        self.assertEqual(self.admin_action(user_id, "disable").status, 200)


# --- Validation (§4, §7.3) ---

class ValidationTests(NextStopTestCase):
    def test_list(self):
        for value in (None, "Mine", "MINE", " mine", "mine ", "", "both", "any", ["mine"], {}, {"mine": True},
                      1, True, 0.5):
            with self.subTest(value=value):
                self.assert_code(self.stop(self.pid, "PE", value, True), 400, "next_stop_list_invalid")
        resp = self.api("POST", f"/api/passports/{self.pid}/countries/PE/next-stops", {"on": True})
        self.assert_code(resp, 400, "next_stop_list_invalid")
        self.assertEqual(self.own_rows() + self.shared_rows(), [])

    def test_on(self):
        for value in (1, 0, "true", "false", None, [], {}, [True], 1.0):
            with self.subTest(value=value):
                self.assert_code(self.stop(self.pid, "PE", "mine", value), 400, "next_stop_on_invalid")
                self.assert_code(self.stop(self.pid, "PE", "ours", value), 400, "next_stop_on_invalid")
        resp = self.api("POST", f"/api/passports/{self.pid}/countries/PE/next-stops", {"list": "mine"})
        self.assert_code(resp, 400, "next_stop_on_invalid")
        self.assertEqual(self.own_rows() + self.shared_rows(), [])

    def test_list_checked_first(self):
        self.assert_code(self.stop(self.pid, "PE", "x", 1), 400, "next_stop_list_invalid")
        self.assert_code(self.api("POST", f"/api/passports/{self.pid}/countries/PE/next-stops", {}),
                         400, "next_stop_list_invalid")

    def test_validated_before_the_database(self):
        # A non-member, a missing passport and an unknown country all get the 400 first.
        self.assert_code(self.stop(self.pid, "PE", "x", True, token=self.eve_token), 400, "next_stop_list_invalid")
        self.assert_code(self.stop(999, "PE", "mine", 1), 400, "next_stop_on_invalid")
        self.assert_code(self.stop(self.pid, "XX", "ours", "yes"), 400, "next_stop_on_invalid")

    def test_other_keys_ignored(self):
        self.clock_offset = 50
        body = self.set_stop(self.pid, "PE", "mine", True)
        self.assertTrue(body["my_next_stop"])
        resp = self.stop(self.pid, "JP", "ours", True, user_id=self.bob, added_by=self.bob, created_at=5,
                         passport_id=self.personal, country_code="FR")
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(self.own_rows(self.pid), [(1, "PE", self.base_time + 50)])
        self.assertEqual(self.shared_rows(self.pid), [("JP", 1, self.base_time + 50)])
        self.assertEqual(self.own_rows(self.personal) + self.shared_rows(self.personal), [])


# --- Access and request checks (§4) ---

class AccessTests(NextStopTestCase):
    def test_non_member_and_missing_passport(self):
        for which in ("mine", "ours"):
            with self.subTest(which=which):
                resp = self.stop(self.pid, "PE", which, True, token=self.eve_token)
                self.assertEqual((resp.status, resp.json()), (404, NOT_FOUND))
                resp = self.stop(999, "PE", which, True)
                self.assertEqual((resp.status, resp.json()), (404, NOT_FOUND))
                # Membership comes before the country.
                resp = self.stop(self.pid, "XX", which, True, token=self.eve_token)
                self.assertEqual((resp.status, resp.json()), (404, NOT_FOUND))
        self.assertEqual(self.own_rows() + self.shared_rows(), [])

    def test_admin_non_member(self):
        bob_only = self.create_id("Bob's", token=self.bob_token)
        resp = self.stop(bob_only, "PE", "mine", True)
        self.assertEqual((resp.status, resp.json()), (404, NOT_FOUND))

    def test_unknown_country(self):
        for code in ("XX", "ZZ", "UK"):
            with self.subTest(code=code):
                self.assert_code(self.stop(self.pid, code, "mine", True), 404, "country_not_found")
                # The country is checked before the role.
                self.assert_code(self.stop(self.pid, code, "mine", True, token=self.sam_token),
                                 404, "country_not_found")

    def test_bad_paths(self):
        for path in ("/api/passports/%d/countries/pe/next-stops", "/api/passports/%d/countries/PER/next-stops",
                     "/api/passports/%d/countries/P/next-stops", "/api/passports/%d/countries/PE/next-stop",
                     "/api/passports/%d/countries/PE/next-stops/"):
            with self.subTest(path=path):
                self.assert_code(self.api("POST", path % self.pid, {"list": "mine", "on": True}), 404, "not_found")
        self.assert_code(self.api("POST", "/api/passports/0/countries/PE/next-stops", {"list": "mine", "on": True}),
                         404, "not_found")

    def test_viewer(self):
        for which in ("mine", "ours"):
            for on in (True, False):
                with self.subTest(which=which, on=on):
                    self.assert_code(self.stop(self.pid, "PE", which, on, token=self.sam_token),
                                     403, "participants_only")
        self.assertEqual(self.own_rows() + self.shared_rows(), [])

    def test_viewer_of_personal(self):
        self.assertEqual(self.add(self.personal, self.sam, "sam", "viewer").status, 200)
        # The role is checked before the personal-passport rule.
        self.assert_code(self.stop(self.personal, "PE", "ours", True, token=self.sam_token), 403, "participants_only")
        self.assert_code(self.stop(self.personal, "PE", "mine", True, token=self.sam_token), 403, "participants_only")

    def test_personal_ours(self):
        for on in (True, False):
            with self.subTest(on=on):
                self.assert_code(self.stop(self.personal, "PE", "ours", on), 409, "personal_no_shared")
        self.assertEqual(self.shared_rows(), [])

    def test_get_and_patch_not_allowed(self):
        path = f"/api/passports/{self.pid}/countries/PE/next-stops"
        self.assert_code(self.api("GET", path), 405, "method_not_allowed")
        self.assert_code(self.api("PATCH", path, {"list": "mine", "on": True}), 405, "method_not_allowed")
        self.assertEqual(self.own_rows(), [])

    def test_write_request_checks(self):
        path = f"/api/passports/{self.pid}/countries/PE/next-stops"
        body = {"list": "mine", "on": True}
        self.assert_code(self.api("POST", path, body, content_type="text/plain"), 415, "content_type_json")
        self.assert_code(self.api("POST", path, body, origin="http://evil.example"), 403, "cross_origin")
        self.assert_code(self.api("POST", path, body, origin=None), 403, "cross_origin")
        self.assert_code(self.api("POST", path, raw_body=b'["mine", true]'), 400, "invalid_json")
        self.assert_code(self.api("POST", path, raw_body=b'"mine"'), 400, "invalid_json")
        self.assert_code(self.api("POST", path, raw_body=b'{"list": "mine", "on": tru'), 400, "invalid_json")
        self.assert_code(self.api("POST", path, body, token=None), 401, "not_logged_in")
        self.assert_code(self.api("POST", path, body, token="not-a-session"), 401, "not_logged_in")
        self.assertEqual(self.own_rows(), [])

    def test_pending(self):
        self.assertEqual(self.admin_action(self.bob, "password", {"new_password": "temporary password 1"}).status,
                         200)
        token = self.login_token("bob", "temporary password 1")
        resp = self.stop(self.pid, "PE", "mine", True, token=token)
        self.assertEqual((resp.status, resp.json()), (403, PENDING))
        self.assertEqual(self.own_rows(), [])

    def test_disabled_session_gone(self):
        self.disable(self.bob)
        self.assert_code(self.stop(self.pid, "PE", "mine", True, token=self.bob_token), 401, "not_logged_in")


# --- My next stops ---

class MineTests(NextStopTestCase):
    def test_add_and_remove(self):
        self.clock_offset = 7
        body = self.set_stop(self.pid, "PE", "mine", True)
        self.assertEqual(body["next_stops"], {"people": [self.person(1)], "shared": None})
        self.assertIs(body["my_next_stop"], True)
        self.assertEqual(body["summary"], {"code": "PE", "status": "not_started", "done_by_me": False,
                                           "meal_count": 0, "avg_rating": None, "completed_on": None,
                                           "done_count": 0, "my_next_stop": True, "next_stop_user_ids": [1],
                                           "shared_next_stop": False})
        self.assertEqual(body["summary"], self.summary(self.pid, "PE"))
        self.assertEqual(self.own_rows(self.pid), [(1, "PE", self.base_time + 7)])

        body = self.set_stop(self.pid, "PE", "mine", False)
        self.assertEqual(body["next_stops"], {"people": [], "shared": None})
        self.assertIs(body["my_next_stop"], False)
        self.assertEqual((body["summary"]["my_next_stop"], body["summary"]["next_stop_user_ids"]), (False, []))
        self.assertEqual(self.own_rows(), [])

    def test_idempotent(self):
        self.set_stop(self.pid, "PE", "mine", True)
        self.clock_offset = 100
        body = self.set_stop(self.pid, "PE", "mine", True)
        self.assertTrue(body["my_next_stop"])
        self.assertEqual(self.own_rows(self.pid), [(1, "PE", self.base_time)], "the first entry is kept")
        self.set_stop(self.pid, "PE", "mine", False)
        body = self.set_stop(self.pid, "PE", "mine", False)
        self.assertFalse(body["my_next_stop"])
        self.assertEqual(self.own_rows(), [])

    def test_done(self):
        self.meal_id(self.pid, "PE")
        self.assert_code(self.stop(self.pid, "PE", "mine", True), 409, "next_stop_done")
        self.assertEqual(self.own_rows(), [])
        # Bob hasn't eaten there, so he can.
        self.assertTrue(self.set_stop(self.pid, "PE", "mine", True, token=self.bob_token)["my_next_stop"])
        # Removing is always allowed, even when done.
        self.insert_own(self.pid, 1, "PE")
        self.assertFalse(self.set_stop(self.pid, "PE", "mine", False)["my_next_stop"])
        self.assertEqual(self.own_rows(self.pid), [(self.bob, "PE", self.base_time)])

    def test_done_is_per_passport(self):
        self.meal_id(self.personal, "PE")
        self.assertTrue(self.set_stop(self.pid, "PE", "mine", True)["my_next_stop"])

    def test_lists_are_per_person_in_membership_order(self):
        carol, carol_token = self.user("carol", display_name="Carol")
        self.assertEqual(self.add(self.pid, carol, "carol", "participant").status, 200)
        self.set_stop(self.pid, "PE", "mine", True, token=carol_token)
        self.set_stop(self.pid, "PE", "mine", True, token=self.bob_token)
        body = self.set_stop(self.pid, "PE", "mine", True)
        self.assertEqual(body["summary"]["next_stop_user_ids"], [1, self.bob, carol])
        self.assertEqual(body["next_stops"]["people"], [self.person(1), self.person(self.bob), self.person(carol)])
        summary = self.summary(self.pid, "PE", token=self.bob_token)
        self.assertEqual((summary["my_next_stop"], summary["next_stop_user_ids"]), (True, [1, self.bob, carol]))
        # Removing mine leaves the others'.
        body = self.set_stop(self.pid, "PE", "mine", False, token=self.bob_token)
        self.assertEqual(body["summary"]["next_stop_user_ids"], [1, carol])
        self.assertFalse(body["my_next_stop"])
        self.assertTrue(self.summary(self.pid, "PE")["my_next_stop"])

    def test_viewer_sees_lists(self):
        self.set_stop(self.pid, "PE", "mine", True, token=self.bob_token)
        self.set_stop(self.pid, "PE", "ours", True)
        summary = self.summary(self.pid, "PE", token=self.sam_token)
        self.assertEqual((summary["my_next_stop"], summary["next_stop_user_ids"], summary["shared_next_stop"]),
                         (None, [self.bob], True))
        mine, stops = self.stops(self.pid, "PE", token=self.sam_token)
        self.assertIsNone(mine)
        self.assertEqual(stops, {"people": [self.person(self.bob)],
                                 "shared": {"added_by": self.person(1), "created_at": self.base_time}})
        for item in self.countries_of(self.pid, self.sam_token)["countries"]:
            self.assertIsNone(item["my_next_stop"])

    def test_personal(self):
        body = self.set_stop(self.personal, "PE", "mine", True)
        self.assertEqual(body["next_stops"], {"people": [self.person(1)], "shared": None})
        self.assertEqual(body["summary"]["next_stop_user_ids"], [1])
        self.assertIs(body["summary"]["shared_next_stop"], False)
        self.assertEqual(body["summary"], self.summary(self.personal, "PE"))
        # A viewer of the personal passport sees it.
        self.assertEqual(self.add(self.personal, self.sam, "sam", "viewer").status, 200)
        summary = self.summary(self.personal, "PE", token=self.sam_token)
        self.assertEqual((summary["my_next_stop"], summary["next_stop_user_ids"]), (None, [1]))
        self.assertEqual(self.stops(self.personal, "PE", token=self.sam_token),
                         (None, {"people": [self.person(1)], "shared": None}))

    def test_fresh_listing(self):
        for pid in (self.pid, self.personal):
            for item in self.countries_of(pid)["countries"]:
                self.assertEqual(set(item), SUMMARY_KEYS)
                self.assertEqual((item["my_next_stop"], item["next_stop_user_ids"], item["shared_next_stop"]),
                                 (False, [], False))
            self.assertEqual(self.stops(pid, "PE"), (False, {"people": [], "shared": None}))


# --- Our next stops ---

class OursTests(NextStopTestCase):
    def test_add_and_remove(self):
        self.clock_offset = 3
        body = self.set_stop(self.pid, "PE", "ours", True, token=self.bob_token)
        self.assertEqual(body["next_stops"], {"people": [], "shared": {"added_by": self.person(self.bob),
                                                                       "created_at": self.base_time + 3}})
        self.assertIs(body["my_next_stop"], False)
        self.assertIs(body["summary"]["shared_next_stop"], True)
        self.assertEqual(body["summary"], self.summary(self.pid, "PE", token=self.bob_token))
        self.assertTrue(self.summary(self.pid, "PE")["shared_next_stop"])
        self.assertEqual(self.shared_rows(self.pid), [("PE", self.bob, self.base_time + 3)])
        # Anyone can remove an entry someone else added.
        body = self.set_stop(self.pid, "PE", "ours", False)
        self.assertEqual(body["next_stops"], {"people": [], "shared": None})
        self.assertIs(body["summary"]["shared_next_stop"], False)
        self.assertEqual(self.shared_rows(), [])
        self.assertFalse(self.set_stop(self.pid, "PE", "ours", False)["summary"]["shared_next_stop"])

    def test_existing_entry_keeps_its_adder(self):
        self.set_stop(self.pid, "PE", "ours", True)
        self.clock_offset = 60
        body = self.set_stop(self.pid, "PE", "ours", True, token=self.bob_token)
        self.assertEqual(body["next_stops"]["shared"], {"added_by": self.person(1), "created_at": self.base_time})
        self.assertEqual(self.shared_rows(self.pid), [("PE", 1, self.base_time)])

    def test_complete(self):
        self.meal_id(self.pid, "PE")
        self.meal_id(self.pid, "PE", token=self.bob_token)
        self.assertEqual(self.summary(self.pid, "PE")["status"], "complete")
        for token in (self.token, self.bob_token):
            self.assert_code(self.stop(self.pid, "PE", "ours", True, token=token), 409, "next_stop_complete")
        self.assertEqual(self.shared_rows(), [])
        # Removing is always allowed.
        self.insert_shared(self.pid, "PE", 1)
        self.assertFalse(self.set_stop(self.pid, "PE", "ours", False)["summary"]["shared_next_stop"])

    def test_some_is_allowed(self):
        self.meal_id(self.pid, "PE")
        body = self.set_stop(self.pid, "PE", "ours", True)
        self.assertEqual((body["summary"]["status"], body["summary"]["shared_next_stop"]), ("some", True))

    def test_complete_ignores_inactive(self):
        """A disabled participant who hasn't eaten there doesn't count (country_state rules)."""
        carol, _ = self.user("carol")
        self.assertEqual(self.add(self.pid, carol, "carol", "participant").status, 200)
        self.meal_id(self.pid, "PE")
        self.meal_id(self.pid, "PE", token=self.bob_token)
        self.assertTrue(self.set_stop(self.pid, "PE", "ours", True)["summary"]["shared_next_stop"])
        self.disable(carol)  # PE is now complete, so the disable removed it
        self.assertEqual(self.shared_rows(), [])
        self.assert_code(self.stop(self.pid, "PE", "ours", True), 409, "next_stop_complete")

    def test_shared_and_own_independent(self):
        self.set_stop(self.pid, "PE", "mine", True)
        body = self.set_stop(self.pid, "PE", "ours", True)
        self.assertEqual(body["next_stops"]["people"], [self.person(1)])
        self.assertTrue(body["my_next_stop"])
        body = self.set_stop(self.pid, "PE", "mine", False)
        self.assertTrue(body["summary"]["shared_next_stop"])
        self.assertEqual(body["next_stops"]["shared"]["added_by"], self.person(1))

    def test_personal_rows_never_shown(self):
        """§7.1: shared is null and shared_next_stop false on a personal passport, whatever the rows."""
        with self.db() as conn:
            conn.execute("INSERT INTO shared_next_stops (passport_id, country_code, added_by, created_at) "
                         "VALUES (?, 'PE', 1, 1)", (self.personal,))
        try:
            self.assertFalse(self.summary(self.personal, "PE")["shared_next_stop"])
            self.assertEqual(self.stops(self.personal, "PE"), (False, {"people": [], "shared": None}))
            body = self.set_stop(self.personal, "PE", "mine", True)
            self.assertIsNone(body["next_stops"]["shared"])
            self.assertFalse(body["summary"]["shared_next_stop"])
        finally:
            with self.db() as conn:
                conn.execute("DELETE FROM shared_next_stops WHERE passport_id = ?", (self.personal,))


# --- added_by (§7.1) and disabled participants ---

class AddedByTests(NextStopTestCase):
    def shared_added_by(self, code="PE", token="default"):
        return self.stops(self.pid, code, token)[1]["shared"]["added_by"]

    def test_removed_adder(self):
        self.set_stop(self.pid, "PE", "ours", True, token=self.bob_token)
        self.assertEqual(self.remove(self.pid, self.bob).status, 200)
        self.assertEqual(self.shared_rows(self.pid), [("PE", None, self.base_time)])
        self.assertEqual(self.stops(self.pid, "PE")[1]["shared"], {"added_by": None, "created_at": self.base_time})
        self.assertTrue(self.summary(self.pid, "PE")["shared_next_stop"])

    def test_non_participant_id_in_the_column(self):
        """Even if the column holds an id, only a current participant is named."""
        for user_id in (self.eve, self.sam, 999):  # outsider, viewer, no such user
            with self.subTest(user_id=user_id):
                with self.db() as conn:
                    conn.execute("PRAGMA foreign_keys=OFF")
                    conn.execute("DELETE FROM shared_next_stops")
                    conn.execute("INSERT INTO shared_next_stops (passport_id, country_code, added_by, created_at) "
                                 "VALUES (?, 'PE', ?, 1)", (self.pid, user_id))
                self.assertIsNone(self.shared_added_by())
                body = self.set_stop(self.pid, "PE", "ours", True)
                self.assertEqual(body["next_stops"]["shared"], {"added_by": None, "created_at": 1})
        with self.db() as conn:
            conn.execute("DELETE FROM shared_next_stops")

    def test_disabled_adder(self):
        self.set_stop(self.pid, "PE", "ours", True, token=self.bob_token)
        self.disable(self.bob)
        self.assertEqual(self.shared_added_by(), self.person(self.bob, inactive=True))
        self.assertEqual(self.shared_added_by(token=self.sam_token), self.person(self.bob, inactive=True))
        self.enable(self.bob)
        self.assertEqual(self.shared_added_by(), self.person(self.bob))

    def test_viewer_rows_hidden(self):
        """§7.1: own entries count only through a participant membership (a role set by hand here)."""
        self.set_stop(self.pid, "PE", "mine", True, token=self.bob_token)
        with self.db() as conn:
            conn.execute("UPDATE passport_members SET role = 'viewer' WHERE passport_id = ? AND user_id = ?",
                         (self.pid, self.bob))
        try:
            self.assertEqual(self.summary(self.pid, "PE")["next_stop_user_ids"], [])
            self.assertEqual(self.stops(self.pid, "PE")[1]["people"], [])
            self.assertEqual(self.set_stop(self.pid, "PE", "mine", True)["summary"]["next_stop_user_ids"], [1])
        finally:
            with self.db() as conn:
                conn.execute("UPDATE passport_members SET role = 'participant' WHERE passport_id = ? "
                             "AND user_id = ?", (self.pid, self.bob))

    def test_disabled_participant_entries_hidden(self):
        self.set_stop(self.pid, "PE", "mine", True, token=self.bob_token)
        self.set_stop(self.pid, "JP", "mine", True, token=self.bob_token)
        self.disable(self.bob)
        for token in (self.token, self.sam_token):
            for item in self.countries_of(self.pid, token)["countries"]:
                self.assertEqual(item["next_stop_user_ids"], [], item["code"])
            self.assertEqual(self.stops(self.pid, "PE", token)[1]["people"], [])
        body = self.set_stop(self.pid, "PE", "mine", True)
        self.assertEqual(body["next_stops"]["people"], [self.person(1)])
        self.assertEqual(body["summary"]["next_stop_user_ids"], [1])
        self.assertEqual(body["summary"], self.summary(self.pid, "PE"))
        self.assertEqual(len(self.own_rows(self.pid)), 3, "kept while disabled")
        self.enable(self.bob)
        self.assertEqual(self.summary(self.pid, "PE")["next_stop_user_ids"], [1, self.bob])
        self.assertEqual(self.summary(self.pid, "JP")["next_stop_user_ids"], [self.bob])
        self.assertEqual(self.stops(self.pid, "PE")[1]["people"], [self.person(1), self.person(self.bob)])


class SummaryEqualityTests(NextStopTestCase):
    """§7.4: the POST's summary equals the GET listing's item."""

    def check(self, code, which, on, token="default"):
        body = self.set_stop(self.pid, code, which, on, token)
        self.assertEqual(body["summary"], self.summary(self.pid, code, token))
        self.assertEqual(body["my_next_stop"], body["summary"]["my_next_stop"])
        mine, stops = self.stops(self.pid, code, token)
        self.assertEqual((body["my_next_stop"], body["next_stops"]), (mine, stops))
        return body

    def test_cases(self):
        self.meal_id(self.pid, "PE", rating=4)
        self.meal_id(self.pid, "PE", token=self.bob_token, rating=3)
        self.meal_id(self.pid, "JP")
        self.set_stop(self.pid, "JP", "mine", True, token=self.bob_token)
        for code, which, on, token in (("JP", "ours", True, self.token), ("JP", "mine", False, self.bob_token),
                                       ("FR", "mine", True, self.token), ("PE", "ours", False, self.bob_token),
                                       ("FR", "ours", True, self.bob_token)):
            with self.subTest(code=code, which=which, on=on):
                self.check(code, which, on, token)

    def test_after_disable(self):
        carol, carol_token = self.user("carol")
        self.assertEqual(self.add(self.pid, carol, "carol", "participant").status, 200)
        self.meal_id(self.pid, "PE")
        self.set_stop(self.pid, "PE", "mine", True, token=carol_token)
        self.set_stop(self.pid, "FR", "mine", True, token=carol_token)
        self.disable(carol)
        body = self.check("FR", "mine", True)
        self.assertEqual(body["summary"]["next_stop_user_ids"], [1])
        body = self.check("PE", "ours", True, self.bob_token)
        self.assertEqual((body["summary"]["status"], body["summary"]["done_count"]), ("some", 1))


# --- Cascades (§4 "Changed endpoints", §7.5) ---

class CascadeTests(NextStopTestCase):
    def test_meal_removes_own_entry(self):
        self.set_stop(self.pid, "PE", "mine", True)
        self.set_stop(self.pid, "PE", "mine", True, token=self.bob_token)
        self.set_stop(self.personal, "PE", "mine", True)
        meal_id = self.meal_id(self.pid, "PE")
        summary = self.summary(self.pid, "PE")
        self.assertEqual((summary["my_next_stop"], summary["next_stop_user_ids"]), (False, [self.bob]))
        self.assertEqual(self.own_rows(self.personal), [(1, "PE", self.base_time)], "other passports untouched")
        # Deleting the meal never restores it.
        self.assertEqual(self.delete_meal(self.pid, meal_id).status, 200)
        self.assertFalse(self.summary(self.pid, "PE")["my_next_stop"])

    def test_meal_that_completes_removes_shared_entry(self):
        self.set_stop(self.pid, "PE", "ours", True)
        self.meal_id(self.pid, "PE")
        self.assertTrue(self.summary(self.pid, "PE")["shared_next_stop"], "only some, so kept")
        meal_id = self.meal_id(self.pid, "PE", token=self.bob_token)
        summary = self.summary(self.pid, "PE")
        self.assertEqual((summary["status"], summary["shared_next_stop"]), ("complete", False))
        self.assertEqual(self.shared_rows(), [])
        self.assertEqual(self.delete_meal(self.pid, meal_id, token=self.bob_token).status, 200)
        self.assertFalse(self.summary(self.pid, "PE")["shared_next_stop"], "not restored")

    def test_meal_prunes_only_its_country(self):
        """§7.5: meal create checks only that country."""
        self.seed(self.pid, 1, "FR", 1)
        self.seed(self.pid, self.bob, "FR", 1)
        self.insert_shared(self.pid, "FR", 1)  # complete but still listed (only possible by hand)
        self.set_stop(self.pid, "PE", "ours", True)
        self.meal_id(self.pid, "PE")
        self.meal_id(self.pid, "PE", token=self.bob_token)
        self.assertEqual([row[0] for row in self.shared_rows(self.pid)], ["FR"])
        with self.db() as conn:
            conn.execute("DELETE FROM shared_next_stops")

    def test_meal_update_changes_nothing(self):
        meal_id = self.meal_id(self.pid, "PE")
        self.insert_own(self.pid, 1, "PE")
        self.assertEqual(self.patch(self.pid, meal_id, {"eaten_on": "2026-10-01"}).status, 200)
        self.assertEqual(self.own_rows(self.pid), [(1, "PE", 1)])

    def setup_lists(self):
        """Bob: own FR and JP, added shared JP. Admin has eaten IT; IT on the shared list (bob hasn't)."""
        self.set_stop(self.pid, "FR", "mine", True, token=self.bob_token)
        self.set_stop(self.pid, "JP", "mine", True, token=self.bob_token)
        self.set_stop(self.pid, "JP", "ours", True, token=self.bob_token)
        self.set_stop(self.pid, "FR", "mine", True)
        self.meal_id(self.pid, "IT")
        self.set_stop(self.pid, "IT", "ours", True)

    def assert_bob_gone(self):
        self.assertEqual(self.own_rows(self.pid), [(1, "FR", self.base_time)])
        self.assertEqual(self.shared_rows(self.pid), [("JP", None, self.base_time)], "IT complete, so pruned")
        listing = {c["code"]: c for c in self.countries_of(self.pid)["countries"]}
        self.assertEqual(listing["FR"]["next_stop_user_ids"], [1])
        self.assertEqual(listing["JP"]["next_stop_user_ids"], [])
        self.assertTrue(listing["JP"]["shared_next_stop"])
        self.assertEqual(self.stops(self.pid, "JP")[1]["shared"], {"added_by": None, "created_at": self.base_time})
        self.assertEqual((listing["IT"]["status"], listing["IT"]["shared_next_stop"]), ("complete", False))

    def test_remove_participant(self):
        self.setup_lists()
        self.assertEqual(self.remove(self.pid, self.bob).status, 200)
        self.assert_bob_gone()

    def test_leave(self):
        self.setup_lists()
        self.assertEqual(self.leave(self.pid, token=self.bob_token).status, 200)
        self.assert_bob_gone()

    def test_remove_viewer_changes_nothing(self):
        self.setup_lists()
        before = (self.own_rows(), self.shared_rows())
        self.assertEqual(self.remove(self.pid, self.sam).status, 200)
        self.assertEqual((self.own_rows(), self.shared_rows()), before)

    def test_participant_to_viewer(self):
        self.setup_lists()
        self.assertEqual(self.remove(self.pid, self.bob).status, 200)
        self.assertEqual(self.add(self.pid, self.bob, "bob", "viewer").status, 200)
        self.assertEqual(self.query("SELECT COUNT(*) FROM next_stops WHERE user_id = ?", (self.bob,)), [(0,)])
        summary = self.summary(self.pid, "FR", token=self.bob_token)
        self.assertEqual((summary["my_next_stop"], summary["next_stop_user_ids"]), (None, [1]))
        self.assertIsNone(self.stops(self.pid, "JP", token=self.bob_token)[1]["shared"]["added_by"])
        # Back as a participant: nothing is restored.
        self.assertEqual(self.remove(self.pid, self.bob).status, 200)
        self.assertEqual(self.add(self.pid, self.bob, "bob", "participant").status, 200)
        self.assertFalse(self.summary(self.pid, "FR", token=self.bob_token)["my_next_stop"])
        self.assertIsNone(self.stops(self.pid, "JP")[1]["shared"]["added_by"])

    def test_disable_prunes(self):
        self.setup_lists()
        self.disable(self.bob)
        self.assertEqual(self.shared_rows(self.pid), [("JP", self.bob, self.base_time)], "IT pruned")
        self.assertEqual(len(self.own_rows(self.pid)), 3, "own entries kept")
        # Re-enabling makes IT "some" again but restores nothing.
        self.enable(self.bob)
        self.assertEqual(self.summary(self.pid, "IT")["status"], "some")
        self.assertEqual(self.shared_rows(self.pid), [("JP", self.bob, self.base_time)])

    def test_disable_prunes_handed_over_passport(self):
        """The prune runs after the handover, in a passport the disabled user owned."""
        bob_pid = self.create_id("Bob's", [ref(1, "admin")], token=self.bob_token)
        self.meal_id(bob_pid, "IT")
        self.set_stop(bob_pid, "IT", "ours", True, token=self.bob_token)
        self.set_stop(bob_pid, "FR", "ours", True)
        self.disable(self.bob)
        self.assertEqual(self.owner_of(bob_pid), 1)
        self.assertEqual(self.shared_rows(bob_pid), [("FR", 1, self.base_time)])

    def test_zero_active_participants_prunes_nothing(self):
        """§7.2: with no active participants nothing is complete, so nothing is removed."""
        carol, carol_token = self.user("carol")
        dave, _ = self.user("dave")
        cpid = self.create_id("Carol's", [ref(dave, "dave")], token=carol_token)
        self.meal_id(cpid, "IT", token=carol_token)
        self.set_stop(cpid, "IT", "ours", True, token=carol_token)
        self.disable(carol)  # handed over to dave, who hasn't eaten there: not complete
        self.disable(dave)  # no active participants left
        self.assertEqual(self.shared_rows(cpid), [("IT", carol, self.base_time)])
        with self.db() as conn:
            meals.prune_shared(conn, cpid)
        self.assertEqual(self.shared_rows(cpid), [("IT", carol, self.base_time)])

    def test_admin_delete(self):
        self.setup_lists()
        self.admin_delete(self.bob, "bob")
        self.assert_bob_gone()

    def test_self_delete(self):
        self.setup_lists()
        self.self_delete(self.bob, self.bob_token)
        self.assert_bob_gone()

    def test_delete_prunes_handed_over_and_deletes_owned(self):
        handed = self.create_id("Bob's", [ref(1, "admin")], token=self.bob_token)
        alone = self.create_id("Bob alone", token=self.bob_token)
        self.meal_id(handed, "IT")
        self.set_stop(handed, "IT", "ours", True, token=self.bob_token)
        self.set_stop(handed, "FR", "ours", True, token=self.bob_token)
        self.set_stop(alone, "IT", "ours", True, token=self.bob_token)
        self.set_stop(alone, "IT", "mine", True, token=self.bob_token)
        self.set_stop(self.personal_id(self.bob), "IT", "mine", True, token=self.bob_token)
        self.admin_delete(self.bob, "bob")
        self.assertEqual(self.owner_of(handed), 1)
        self.assertEqual(self.shared_rows(handed), [("FR", None, self.base_time)])
        self.assertEqual(self.shared_rows(alone) + self.own_rows(alone), [])
        self.assertEqual(self.query("SELECT COUNT(*) FROM next_stops WHERE user_id = ?", (self.bob,)), [(0,)])

    def test_passport_deletion(self):
        self.setup_lists()
        self.assertEqual(self.delete(self.pid, "Trip").status, 200)
        self.assertEqual(self.own_rows() + self.shared_rows(), [])

    def test_enable_and_add_member_change_nothing(self):
        self.setup_lists()
        before = (self.own_rows(), self.shared_rows())
        carol, _ = self.user("carol")
        self.assertEqual(self.add(self.pid, carol, "carol", "participant").status, 200)
        self.disable(carol)
        self.enable(carol)
        self.assertEqual((self.own_rows(), self.shared_rows()), before)

    def test_id_reuse_inherits_nothing(self):
        """users.id is reused (1b L5): a new account with a deleted user's id gets no entries or name."""
        zed, zed_token = self.user("zed", display_name="Zed")
        self.assertEqual(zed, max(row[0] for row in self.query("SELECT id FROM users")))
        self.assertEqual(self.add(self.pid, zed, "zed", "participant").status, 200)
        self.set_stop(self.pid, "FR", "mine", True, token=zed_token)
        self.set_stop(self.pid, "JP", "ours", True, token=zed_token)
        self.set_stop(self.personal_id(zed), "FR", "mine", True, token=zed_token)
        self.admin_delete(zed, "zed")
        resp = self.api("POST", "/api/admin/users", {"username": "newbie", "display_name": "Newbie",
                                                     "password": "temporary password 1"})
        self.assertEqual(resp.status, 201, resp)
        self.assertEqual(resp.json()["user"]["id"], zed, "the id was reused")
        self.assertEqual(self.query("SELECT COUNT(*) FROM next_stops WHERE user_id = ?", (zed,)), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM shared_next_stops WHERE added_by = ?", (zed,)), [(0,)])
        self.assertEqual(self.add(self.pid, zed, "newbie", "participant").status, 200)
        self.assertEqual(self.summary(self.pid, "FR")["next_stop_user_ids"], [])
        self.assertIsNone(self.stops(self.pid, "JP")[1]["shared"]["added_by"])


# --- prune_shared directly ---

class PruneTests(NextStopTestCase):
    def test_all_and_one_country(self):
        for code in ("FR", "IT", "PE"):
            self.seed(self.pid, 1, code, 1)
            self.seed(self.pid, self.bob, code, 1)
        self.seed(self.pid, 1, "JP", 1)
        for code in ("FR", "IT", "PE", "JP", "DE"):
            self.insert_shared(self.pid, code, 1)
        with self.db() as conn:
            meals.prune_shared(conn, self.pid, "FR")
        self.assertEqual([row[0] for row in self.shared_rows(self.pid)], ["IT", "PE", "JP", "DE"])
        with self.db() as conn:
            meals.prune_shared(conn, self.pid)
        self.assertEqual([row[0] for row in self.shared_rows(self.pid)], ["JP", "DE"])

    def test_personal_is_harmless(self):
        """On a personal passport it removes nothing: not own entries, not other passports' shared rows."""
        self.seed(self.personal, 1, "FR", 1)
        self.insert_own(self.personal, 1, "FR")  # done but listed (only possible by hand)
        self.insert_own(self.personal, 1, "JP")
        self.seed(self.pid, 1, "FR", 1)
        self.seed(self.pid, self.bob, "FR", 1)
        self.insert_shared(self.pid, "FR", 1)  # complete in Trip, so a prune of Trip would remove it
        self.insert_shared(self.pid, "JP", self.bob)
        own, shared = self.own_rows(), self.shared_rows()
        for code in (None, "FR"):
            with self.subTest(code=code):
                with self.db() as conn:
                    meals.prune_shared(conn, self.personal, code)
                self.assertEqual((self.own_rows(), self.shared_rows()), (own, shared))
        with self.db() as conn:
            conn.execute("DELETE FROM shared_next_stops")


class SelfDeleteTests(NextStopTestCase):
    def test_self_delete_needs_password(self):
        # Unchanged behaviour: a wrong password deletes nothing, lists included.
        self.set_stop(self.pid, "FR", "mine", True, token=self.bob_token)
        resp = self.api("POST", "/api/account/delete", {"password": PASSWORD + "x"}, token=self.bob_token)
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.own_rows(self.pid), [(self.bob, "FR", self.base_time)])
