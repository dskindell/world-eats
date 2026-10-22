"""Change 11a: generated avatar seeds (contract 11a sections 2, 3, 5 and 6)."""

import re
from unittest import mock

from tests.helpers import PASSWORD, ServerTestCase, cookie_attributes, cookie_value, db, fast_hasher, server, sha256_hex
from tests.test_meals import DATE, MealTestCase
from tests.test_passports import PENDING, PassportTestCase

SEED_RE = re.compile(r"[0-9a-f]{32}")
CLIENT_SEED = "0123456789abcdef0123456789abcdef"  # a valid-looking seed the server must never store
NEW_PASSWORD = "a brand new passphrase"


class SeedAssertions:
    def stored_seeds(self):
        with self.db() as conn:
            return {row[0]: row[1] for row in conn.execute("SELECT id, avatar_seed FROM users")}

    def assert_all_seeds_valid(self):
        seeds = self.stored_seeds()
        for user_id, seed in seeds.items():
            self.assertIsInstance(seed, str)
            self.assertTrue(SEED_RE.fullmatch(seed), f"user {user_id} has seed {seed!r}")
        self.assertEqual(len(set(seeds.values())), len(seeds), "seeds are distinct")

    def assert_seed(self, obj, user_id):
        """The object carries the user's stored seed."""
        self.assertIn("avatar_seed", obj)
        self.assertTrue(SEED_RE.fullmatch(obj["avatar_seed"]))
        self.assertEqual(obj["avatar_seed"], self.seed_of(user_id))


# --- Seeds at account creation (§2) ---

class SetupSeedTests(SeedAssertions, ServerTestCase):
    def test_setup_gives_a_seed(self):
        resp = self.request("POST", "/api/setup", {"username": "admin", "display_name": "Admin",
                                                    "password": PASSWORD})
        self.assertEqual(resp.status, 201, resp)
        self.assert_seed(resp.json()["user"], 1)
        self.assert_all_seeds_valid()

    def test_setup_ignores_a_client_seed(self):
        resp = self.request("POST", "/api/setup", {"username": "admin", "display_name": "Admin",
                                                    "password": PASSWORD, "avatar_seed": CLIENT_SEED})
        self.assertEqual(resp.status, 201, resp)
        self.assertNotEqual(self.seed_of(1), CLIENT_SEED)
        self.assert_seed(resp.json()["user"], 1)


class AdminCreateSeedTests(SeedAssertions, PassportTestCase):
    def admin_create(self, username, **extra):
        return self.api("POST", "/api/admin/users", {"username": username, "display_name": username.title(),
                                                     "password": PASSWORD, **extra})

    def test_created_users_get_distinct_seeds(self):
        seen = {self.seed_of(1)}
        for name in ("bob", "sam", "dan", "eve", "kim"):
            resp = self.admin_create(name)
            self.assertEqual(resp.status, 201, resp)
            user = resp.json()["user"]
            self.assert_seed(user, user["id"])
            seen.add(user["avatar_seed"])
        self.assertEqual(len(seen), 6)
        self.assert_all_seeds_valid()

    def test_admin_create_ignores_a_client_seed(self):
        resp = self.admin_create("bob", avatar_seed=CLIENT_SEED)
        self.assertEqual(resp.status, 201, resp)
        user = resp.json()["user"]
        self.assertNotEqual(self.seed_of(user["id"]), CLIENT_SEED)
        self.assert_seed(user, user["id"])

    def test_admin_patch_ignores_a_client_seed(self):
        bob = self.admin_create("bob").json()["user"]["id"]
        before = self.seed_of(bob)
        resp = self.api("PATCH", f"/api/admin/users/{bob}", {"display_name": "Bobby", "avatar_seed": CLIENT_SEED})
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(self.seed_of(bob), before)
        self.assert_seed(resp.json()["user"], bob)

    def test_id_reuse_gets_a_new_seed(self):
        """1b L5: users.id is reused after the highest-id user is deleted; the seed isn't inherited."""
        bob = self.admin_create("bob").json()["user"]["id"]
        old_seed = self.seed_of(bob)
        resp = self.api("POST", f"/api/admin/users/{bob}/delete", {"confirm_username": "bob"})
        self.assertEqual(resp.status, 200, resp)
        resp = self.admin_create("bob")
        self.assertEqual(resp.status, 201, resp)
        user = resp.json()["user"]
        self.assertEqual(user["id"], bob, "the id was reused")
        self.assertNotEqual(user["avatar_seed"], old_seed)
        self.assert_seed(user, bob)


# --- avatar_seed in every object (§3) ---

class AccountObjectTests(SeedAssertions, PassportTestCase):
    def test_session_and_login(self):
        self.assert_seed(self.session_user(self.token), 1)
        resp = self.login()
        self.assertEqual(resp.status, 200)
        self.assert_seed(resp.json()["user"], 1)

    def test_account_patch(self):
        for body in ({"display_name": "Boss"}, {"default_passport_id": None}):
            with self.subTest(body=body):
                resp = self.api("PATCH", "/api/account", body)
                self.assertEqual(resp.status, 200, resp)
                self.assert_seed(resp.json()["user"], 1)

    def test_account_patch_ignores_a_client_seed(self):
        before = self.seed_of(1)
        resp = self.api("PATCH", "/api/account", {"display_name": "Boss", "avatar_seed": CLIENT_SEED})
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(self.seed_of(1), before)
        self.assertEqual(resp.json()["user"]["avatar_seed"], before)

    def test_account_patch_returns_the_stored_seed(self):
        """M2: a seed changed after the session loaded shows up in the PATCH response."""
        original = server.Handler.load_session
        new_seed = "f" * 32

        def load_session(handler, renew):
            original(handler, renew)
            if handler.command == "PATCH":
                with self.db() as conn:
                    conn.execute("UPDATE users SET avatar_seed = ?, display_name = 'Elsewhere' WHERE id = 1",
                                 (new_seed,))

        with mock.patch.object(server.Handler, "load_session", load_session):
            resp = self.api("PATCH", "/api/account", {"default_passport_id": None})
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(resp.json()["user"]["avatar_seed"], new_seed)
        self.assertEqual(resp.json()["user"]["display_name"], "Elsewhere")

    def test_admin_objects(self):
        bob, _ = self.user("bob")
        resp = self.api("GET", "/api/admin/users")
        self.assertEqual(resp.status, 200)
        users = resp.json()["users"]
        self.assertEqual(len(users), 2)
        for user in users:
            self.assert_seed(user, user["id"])
        for name in ("disable", "enable", "promote", "demote", "unlock"):
            with self.subTest(action=name):
                resp = self.api("POST", f"/api/admin/users/{bob}/{name}", {})
                self.assertEqual(resp.status, 200, resp)
                self.assert_seed(resp.json()["user"], bob)


class PersonObjectTests(SeedAssertions, MealTestCase):
    """Admin owns "Trip" with bob participating and sam viewing; eve is outside it."""

    def test_users_list(self):
        resp = self.api("GET", "/api/users")
        self.assertEqual(resp.status, 200)
        users = resp.json()["users"]
        self.assertEqual(len(users), 3)
        for user in users:
            self.assert_seed(user, user["id"])

    def test_passport_list_owner(self):
        resp = self.api("GET", "/api/passports", token=self.bob_token)
        self.assertEqual(resp.status, 200)
        for item in resp.json()["passports"]:
            self.assert_seed(item["owner"], item["owner"]["id"])
        owners = {item["owner"]["id"] for item in resp.json()["passports"]}
        self.assertEqual(owners, {1, self.bob})

    def test_passport_detail(self):
        resp = self.get(self.pid, token=self.sam_token)
        self.assertEqual(resp.status, 200)
        body = resp.json()
        self.assert_seed(body["passport"]["owner"], 1)
        people = body["participants"] + body["viewers"]
        self.assertEqual({p["id"] for p in people}, {1, self.bob, self.sam})
        for person in people:
            self.assert_seed(person, person["id"])

    def test_meals_and_country_participants(self):
        resp = self.log_meal(self.pid, "PE", token=self.bob_token, dish="Ceviche")
        self.assertEqual(resp.status, 201, resp)
        self.assert_seed(resp.json()["meal"]["user"], self.bob)
        meal_id = resp.json()["meal"]["id"]
        resp = self.patch(self.pid, meal_id, {"eaten_on": DATE, "dish": "Lomo"}, token=self.bob_token)
        self.assertEqual(resp.status, 200, resp)
        self.assert_seed(resp.json()["meal"]["user"], self.bob)
        self.meal_id(self.pid, "PE")
        resp = self.api("GET", f"/api/passports/{self.pid}/countries/PE", token=self.sam_token)
        self.assertEqual(resp.status, 200)
        body = resp.json()
        self.assertEqual(len(body["meals"]), 2)
        for meal in body["meals"]:
            self.assert_seed(meal["user"], meal["user"]["id"])
        self.assertEqual({p["id"] for p in body["participants"]}, {1, self.bob})
        for person in body["participants"]:
            self.assert_seed(person, person["id"])

    def test_delete_preview_new_owner(self):
        resp = self.api("GET", "/api/account/delete-preview")
        self.assertEqual(resp.status, 200)
        handed_over = resp.json()["handed_over"]
        self.assertEqual([p["new_owner"]["id"] for p in handed_over], [self.bob])
        self.assert_seed(handed_over[0]["new_owner"], self.bob)

    def test_inactive_people_keep_their_seed(self):
        self.assertEqual(self.api("POST", f"/api/admin/users/{self.bob}/disable", {}).status, 200)
        body = self.get(self.pid).json()
        bob = next(p for p in body["participants"] if p["id"] == self.bob)
        self.assertTrue(bob["inactive"])
        self.assert_seed(bob, self.bob)


# --- POST /api/account/avatar (§3) ---

class NewAvatarTests(SeedAssertions, PassportTestCase):
    def setUp(self):
        super().setUp()
        self.bob, self.bob_token = self.user("bob")

    def new_avatar(self, body=None, token="default", **kwargs):
        return self.api("POST", "/api/account/avatar", {} if body is None else body, token=token, **kwargs)

    def test_new_seed(self):
        before = self.stored_seeds()
        session_before = self.session_user(self.token)
        resp = self.new_avatar()
        self.assertEqual(resp.status, 200, resp)
        body = resp.json()
        self.assertEqual(set(body), {"user"})
        user = body["user"]
        self.assert_seed(user, 1)
        self.assertNotEqual(user["avatar_seed"], before[1])
        self.assertEqual(user, {**session_before, "avatar_seed": user["avatar_seed"]}, "otherwise the same object")
        self.assertEqual(self.session_user(self.token), user)
        after = self.stored_seeds()
        self.assertEqual(after[self.bob], before[self.bob], "only the caller's seed changes")
        self.assert_all_seeds_valid()

    def test_every_call_rerolls(self):
        seeds = {self.seed_of(1)}
        for _ in range(5):
            resp = self.new_avatar()
            self.assertEqual(resp.status, 200)
            seeds.add(resp.json()["user"]["avatar_seed"])
        self.assertEqual(len(seeds), 6)

    def test_other_users_seed_unchanged(self):
        before = self.seed_of(1)
        resp = self.new_avatar(token=self.bob_token)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["user"]["id"], self.bob)
        self.assertEqual(self.seed_of(1), before)
        self.assert_seed(resp.json()["user"], self.bob)

    def test_body_ignored(self):
        before = self.stored_seeds()
        for body in ({}, {"avatar_seed": CLIENT_SEED}, {"user_id": self.bob, "id": self.bob}):
            with self.subTest(body=body):
                resp = self.new_avatar(body)
                self.assertEqual(resp.status, 200, resp)
                self.assertNotEqual(self.seed_of(1), CLIENT_SEED)
                self.assert_seed(resp.json()["user"], 1)
        self.assertEqual(self.seed_of(self.bob), before[self.bob])
        resp = self.request("POST", "/api/account/avatar", raw_body=b"", token=self.token)
        self.assertEqual(resp.status, 200, "an empty body is {}")

    def test_needs_login(self):
        before = self.stored_seeds()
        for token in (None, "not-a-session"):
            with self.subTest(token=token):
                resp = self.new_avatar(token=token)
                self.assertEqual(resp.status, 401)
                self.assertEqual(resp.json()["code"], "not_logged_in")
        self.assertEqual(self.stored_seeds(), before)

    def test_pending_password_change(self):
        with self.db() as conn:
            conn.execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (self.bob,))
        before = self.seed_of(self.bob)
        resp = self.new_avatar(token=self.bob_token)
        self.assertEqual(resp.status, 403)
        self.assertEqual(resp.json(), PENDING)
        self.assertEqual(self.seed_of(self.bob), before)

    def test_write_request_checks(self):
        before = self.stored_seeds()
        cases = [
            ({"content_type": "text/plain"}, 415, "content_type_json"),
            ({"content_type": None}, 415, "content_type_json"),
            ({"origin": "http://evil.test"}, 403, "cross_origin"),
            ({"origin": "http://evil.test", "content_type": "text/plain"}, 403, "cross_origin"),
            ({"origin": None}, 403, "cross_origin"),
        ]
        for kwargs, status, code in cases:
            with self.subTest(**kwargs):
                resp = self.new_avatar(**kwargs)
                self.assertEqual((resp.status, resp.json()["code"]), (status, code))
        for raw in (b"[]", b'"x"', b"1", b"null", b"{", b"\xff"):
            with self.subTest(raw=raw):
                resp = self.request("POST", "/api/account/avatar", raw_body=raw, token=self.token)
                self.assertEqual((resp.status, resp.json()["code"]), (400, "invalid_json"))
        self.assertEqual(self.stored_seeds(), before)

    def test_methods(self):
        for method in ("GET", "PATCH", "DELETE"):
            with self.subTest(method=method):
                resp = self.api(method, "/api/account/avatar")
                self.assertEqual(resp.status, 405)

    def test_renews_the_session(self):
        with self.db() as conn:
            before = conn.execute("SELECT last_used_at FROM sessions WHERE user_id = ?", (self.bob,)).fetchone()[0]
        self.clock_offset = 3601
        resp = self.new_avatar(token=self.bob_token)
        self.assertEqual(resp.status, 200)
        cookies = resp.set_cookies(self.cookie_name)
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookie_value(cookies[0]), self.bob_token)
        self.assertIn("Max-Age=2592000", cookie_attributes(cookies[0]))
        with self.db() as conn:
            after = conn.execute("SELECT last_used_at FROM sessions WHERE user_id = ?", (self.bob,)).fetchone()[0]
        self.assertGreaterEqual(after, before + 3600)

    def test_session_revoked_mid_request(self):
        """The session is re-checked inside the write transaction."""
        original = server.Handler.load_session

        def load_session(handler, renew):
            original(handler, renew)
            if handler.raw_path == "/api/account/avatar":
                with self.db() as conn:
                    conn.execute("DELETE FROM sessions WHERE user_id = ?", (self.bob,))

        before = self.seed_of(self.bob)
        with mock.patch.object(server.Handler, "load_session", load_session):
            resp = self.new_avatar(token=self.bob_token)
        self.assertEqual((resp.status, resp.json()["code"]), (401, "not_logged_in"))
        self.assertEqual(self.seed_of(self.bob), before)

    def test_no_log_line(self):
        self.log.truncate(0)
        self.log.seek(0)
        resp = self.new_avatar()
        self.assertEqual(resp.status, 200)
        self.assertNotIn(resp.json()["user"]["avatar_seed"], self.log.getvalue())
        # Positive check: nothing but the request path mentions avatars.
        self.assertNotIn("avatar", self.log.getvalue().lower().replace("/api/account/avatar", ""))
        # access lines only (written after the response, so maybe not yet), no extra log entry
        for line in self.log.getvalue().splitlines():
            self.assertRegex(line, r"^[A-Z]+ /\S* \d{3}$")

    def test_password_change_then_new_pattern(self):
        """A user who finished a forced password change can re-roll."""
        with self.db() as conn:
            conn.execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (self.bob,))
        resp = self.api("POST", "/api/account/password", {"current_password": PASSWORD,
                                                          "new_password": NEW_PASSWORD}, token=self.bob_token)
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(self.new_avatar(token=self.bob_token).status, 200)


# --- The server on an upgraded version-5 database (§2) ---

class MigrationServerTests(SeedAssertions, ServerTestCase):
    def prepare_data(self):
        path = f"{self.data_dir}/{db.DB_NAME}"
        db.init_schema(path, db.MIGRATIONS[:5])
        conn = db.connect(path)
        try:
            password_hash = fast_hasher().hash(PASSWORD)
            conn.executemany("INSERT INTO users (id, username, display_name, password_hash, is_admin, created_at) "
                             "VALUES (?, ?, ?, ?, ?, 100)",
                             [(1, "admin", "Admin", password_hash, 1), (2, "bob", "Bob", password_hash, 0)])
            conn.executescript("""
                INSERT INTO passports (id, name, owner_id, is_personal, created_at)
                    VALUES (1, 'My passport', 1, 1, 100), (2, 'My passport', 2, 1, 100), (3, 'Trip', 1, 0, 100);
                INSERT INTO passport_members (passport_id, user_id, role, created_at)
                    VALUES (1, 1, 'participant', 100), (2, 2, 'participant', 100),
                           (3, 1, 'participant', 100), (3, 2, 'participant', 100);
            """)
            now = self.base_time
            conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, last_used_at, expires_at) "
                         "VALUES (?, 1, ?, ?, ?)", (sha256_hex("live-token"), now, now, now + 1000))
        finally:
            conn.close()

    def test_old_accounts_have_seeds(self):
        with self.db() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.MIGRATIONS[-1][0])
        self.assert_all_seeds_valid()
        self.assert_seed(self.session_user("live-token"), 1)
        detail = self.request("GET", "/api/passports/3", token="live-token").json()
        for person in detail["participants"]:
            self.assert_seed(person, person["id"])
        resp = self.request("POST", "/api/account/avatar", {}, token="live-token")
        self.assertEqual(resp.status, 200, resp)
        self.assert_seed(resp.json()["user"], 1)
        self.assert_all_seeds_valid()
        resp = self.login("bob")
        self.assertEqual(resp.status, 200, resp)
        self.assert_seed(resp.json()["user"], 2)
