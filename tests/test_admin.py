"""Feature 1b: admin screen."""

import threading
from unittest import mock

from tests.helpers import (PASSWORD, CountingSemaphore, ServerTestCase, auth, cookie_attributes, cookie_value,
                           db, fast_hasher, server, sha256_hex)

TEMP = "temporary password 1"
TEMP2 = "temporary password 2"
NEW = "my own chosen password"
WEEK = 604800
LOCKED = "Too many failed attempts. Try again in a few minutes."
ADMIN_KEYS = {"id", "username", "display_name", "is_admin", "disabled", "locked", "must_change_password",
              "temp_password_expired", "created_at", "last_active_at", "avatar_seed", "photo"}
PENDING = {"error": "You must change your password first", "code": "password_change_required",
           "password_change_required": True}

# Every admin endpoint, with a body that would succeed for user 2 ("bob").
ENDPOINTS = [
    ("GET", "/api/admin/users", None),
    ("POST", "/api/admin/users", {"username": "newbie", "display_name": "N", "password": TEMP}),
    ("PATCH", "/api/admin/users/{id}", {"display_name": "Robert"}),
    ("POST", "/api/admin/users/{id}/password", {"new_password": TEMP2}),
    ("POST", "/api/admin/users/{id}/disable", {}),
    ("POST", "/api/admin/users/{id}/enable", {}),
    ("POST", "/api/admin/users/{id}/promote", {}),
    ("POST", "/api/admin/users/{id}/demote", {}),
    ("POST", "/api/admin/users/{id}/unlock", {}),
    ("POST", "/api/admin/users/{id}/delete", {"confirm_username": "bob"}),
]


class AdminTestCase(ServerTestCase):
    """An admin "admin" (id 1, logged in as self.token) and helpers for the admin API."""

    def setUp(self):
        super().setUp()
        self.token = self.setup_admin()

    def admin(self, method, path, body=None, token="default", **kwargs):
        if body is None and method in ("POST", "PATCH"):
            body = {}
        return self.request(method, path, body, token=self.token if token == "default" else token, **kwargs)

    def create(self, username, password=TEMP, is_admin=..., display_name=None, token="default"):
        body = {"username": username, "display_name": display_name or username.title(), "password": password}
        if is_admin is not ...:
            body["is_admin"] = is_admin
        return self.admin("POST", "/api/admin/users", body, token=token)

    def create_id(self, username, **kwargs):
        resp = self.create(username, **kwargs)
        self.assertEqual(resp.status, 201, resp)
        return resp.json()["user"]["id"]

    def action(self, user_id, name, body=None, token="default"):
        return self.admin("POST", f"/api/admin/users/{user_id}/{name}", body, token=token)

    def users(self, token="default"):
        resp = self.admin("GET", "/api/admin/users", token=token)
        self.assertEqual(resp.status, 200, resp)
        return {u["username"]: u for u in resp.json()["users"]}

    def row(self, user_id):
        with self.db() as conn:
            return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def user_count(self):
        with self.db() as conn:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def change_own_password(self, token, current, new):
        return self.request("POST", "/api/account/password", {"current_password": current, "new_password": new},
                            token=token)

    def user_with_own_password(self, username, is_admin=False):
        """A user who has chosen their own password (PASSWORD); returns (id, token)."""
        user_id = self.insert_user(username, is_admin=is_admin)
        return user_id, self.login_token(username)

    def set_disabled(self, user_id, value=1):
        with self.db() as conn:
            conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (value, user_id))

    def set_pending(self, user_id, value=1):
        with self.db() as conn:
            conn.execute("UPDATE users SET must_change_password = ? WHERE id = ?", (value, user_id))

    def set_admin(self, user_id, value):
        with self.db() as conn:
            conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (value, user_id))


class MigrationServerTests(ServerTestCase):
    """The server starting on a 1a (schema version 1) database, including an expired session."""

    def prepare_data(self):
        path = f"{self.data_dir}/{db.DB_NAME}"
        db.init_schema(path, db.MIGRATIONS[:1])
        conn = db.connect(path)
        try:
            conn.execute("INSERT INTO users (id, username, display_name, password_hash, is_admin, created_at) "
                         "VALUES (1, 'admin', 'Admin', ?, 1, 100)", (fast_hasher().hash(PASSWORD),))
            now = self.base_time
            conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, last_used_at, expires_at) "
                         "VALUES (?, 1, ?, ?, ?)", (sha256_hex("live-token"), now - 600, now - 500, now + 1000))
            conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, last_used_at, expires_at) "
                         "VALUES (?, 1, ?, ?, ?)", (sha256_hex("old-token"), now - 9000, now - 8000, now - 1))
        finally:
            conn.close()

    def test_upgraded_database_works(self):
        with self.db() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.MIGRATIONS[-1][0])
            row = conn.execute("SELECT disabled, must_change_password, last_active_at, temp_password_set_at "
                               "FROM users WHERE id = 1").fetchone()
        self.assertEqual(tuple(row), (0, 0, self.base_time - 500, None))
        # the existing session still works and the admin isn't forced to change their password
        self.assertEqual(self.session_user("live-token"), {
            "id": 1, "username": "admin", "display_name": "Admin", "is_admin": True,
            "must_change_password": False, "avatar_seed": self.seed_of(1), "photo": None})
        # the expired one doesn't, and its cookie is cleared
        resp = self.request("GET", "/api/session", token="old-token")
        self.assertIsNone(resp.json()["user"])
        self.assertIn("Max-Age=0", cookie_attributes(resp.set_cookies(self.cookie_name)[0]))
        # login works, and the admin screen works on the migrated data
        token = self.login_token()
        resp = self.request("GET", "/api/admin/users", token=token)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["users"][0]["last_active_at"], self.base_time)


class AuthorizationTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.bob_id, self.bob = self.user_with_own_password("bob")

    def endpoints(self, user_id):
        return [(m, p.format(id=user_id), b) for m, p, b in ENDPOINTS]

    def test_without_session(self):
        for method, path, body in self.endpoints(self.bob_id) + self.endpoints(999):
            with self.subTest(method=method, path=path):
                resp = self.admin(method, path, body, token=None)
                self.assertEqual(resp.status, 401)
                self.assertEqual(resp.error, "Not logged in")

    def test_non_admin(self):
        # an existing id, a missing one (no probing: 403, not 404) and the caller's own
        for user_id in (1, 999, self.bob_id):
            for method, path, body in self.endpoints(user_id):
                with self.subTest(method=method, path=path):
                    resp = self.admin(method, path, body, token=self.bob)
                    self.assertEqual(resp.status, 403)
                    self.assertEqual(resp.json(), {"error": "Admins only", "code": "admins_only"})
        self.assertEqual(self.user_count(), 2)
        self.assertEqual(self.row(1)["is_admin"], 1)

    def test_non_admin_unlock_of_missing_user(self):
        resp = self.action(999, "unlock", token=self.bob)
        self.assertEqual((resp.status, resp.error), (403, "Admins only"))

    def test_admin_with_pending_password(self):
        self.set_pending(1)
        for method, path, body in self.endpoints(self.bob_id):
            with self.subTest(method=method, path=path):
                resp = self.admin(method, path, body)
                self.assertEqual(resp.status, 403)
                self.assertEqual(resp.json(), PENDING)
        self.assertEqual(self.user_count(), 2)
        self.assertEqual(self.row(self.bob_id)["display_name"], "bob")

    def test_route_options(self):
        pending = set()
        for pattern, methods in server.ROUTES:
            for method, route in methods.items():
                if pattern.pattern.startswith(server.literal("/api/admin/")):
                    self.assertTrue(route.admin, pattern.pattern)
                    self.assertFalse(route.allow_pending, pattern.pattern)
                    self.assertFalse(route.public, pattern.pattern)
                else:
                    self.assertFalse(route.admin, pattern.pattern)
                if route.allow_pending:
                    pending.add((method, pattern.pattern))
        self.assertEqual(pending, {("POST", server.literal("/api/account/password")),
                                   ("POST", server.literal("/api/account/logout-everywhere"))})


class PendingPasswordTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.bob_id = self.create_id("bob")
        resp = self.login("bob", TEMP)
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.json()["user"]["must_change_password"])
        self.bob = cookie_value(resp.set_cookies(self.cookie_name)[0])

    def test_blocked_endpoints(self):
        self.assertTrue(self.session_user(self.bob)["must_change_password"])
        resp = self.request("PATCH", "/api/account", {"display_name": "Bobby"}, token=self.bob)
        self.assertEqual((resp.status, resp.json()), (403, PENDING))
        resp = self.request("POST", "/api/account/delete", {"password": TEMP}, token=self.bob)
        self.assertEqual((resp.status, resp.json()), (403, PENDING))
        self.assertIsNotNone(self.row(self.bob_id))
        self.assertEqual(self.row(self.bob_id)["display_name"], "Bob")

    def test_logout_everywhere_allowed(self):
        resp = self.request("POST", "/api/account/logout-everywhere", {}, token=self.bob)
        self.assertEqual(resp.status, 200)
        self.assertIsNone(self.session_user(self.bob))

    def test_same_password_rejected(self):
        resp = self.change_own_password(self.bob, TEMP, TEMP)
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.error, "New password must be different from the current one")
        row = self.row(self.bob_id)
        self.assertEqual(row["must_change_password"], 1)
        self.assertIsNotNone(row["temp_password_set_at"])
        self.assertTrue(self.session_user(self.bob)["must_change_password"])
        self.assertEqual(self.login("bob", TEMP).status, 200)

    def test_wrong_temporary_password(self):
        resp = self.change_own_password(self.bob, "not the temp password", NEW)
        self.assertEqual((resp.status, resp.error), (403, "Current password is wrong"))
        self.assertEqual(self.row(self.bob_id)["must_change_password"], 1)

    def test_change_clears_flag(self):
        other = self.login_token("bob", TEMP)
        resp = self.change_own_password(self.bob, TEMP, NEW)
        self.assertEqual(resp.status, 200)
        self.assertFalse(self.session_user(self.bob)["must_change_password"])
        self.assertIsNone(self.session_user(other), "other devices are logged out")
        row = self.row(self.bob_id)
        self.assertEqual((row["must_change_password"], row["temp_password_set_at"]), (0, None))
        resp = self.request("PATCH", "/api/account", {"display_name": "Bobby"}, token=self.bob)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["user"], {"id": self.bob_id, "username": "bob", "display_name": "Bobby",
                                               "is_admin": False, "must_change_password": False,
                                               "avatar_seed": self.seed_of(self.bob_id), "photo": None})
        self.assertEqual(self.login("bob", TEMP).status, 401)
        self.assertEqual(self.login("bob", NEW).status, 200)

    def test_same_password_rule_for_normal_users_too(self):
        resp = self.change_own_password(self.token, PASSWORD, PASSWORD)
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.error, "New password must be different from the current one")


class RoutingTests(AdminTestCase):
    def test_malformed_ids(self):
        for user_id in ("0", "01", "-1", "+1", "1/", "abc", "12345678901", "1.0", "%31", "1%20", " 1", "", "1e3"):
            for suffix in ("", "/disable"):
                path = f"/api/admin/users/{user_id}{suffix}"
                with self.subTest(path=path):
                    resp = self.raw_request("POST", path.replace(" ", "%20"),
                                            headers=self.post_headers(2), body=b"{}")
                    self.assertEqual(resp.status, 404)
                    self.assertEqual(resp.error, "Not found")

    def test_unicode_digits(self):
        # U+0665 (Arabic-Indic five) and U+FF11 (fullwidth one) as raw UTF-8 in the request line
        for raw in (b"\xd9\xa5", b"\xef\xbc\x91", b"1\xd9\xa5"):
            with self.subTest(raw=raw):
                head = (f"POST /api/admin/users/".encode() + raw + b"/disable HTTP/1.0\r\n"
                        + f"Host: {self.host}\r\nOrigin: {self.origin}\r\nContent-Type: application/json\r\n"
                          f"Content-Length: 2\r\nCookie: {self.cookie_name}={self.token}\r\n\r\n{{}}".encode())
                resp = self.raw(head)
                self.assertEqual(resp.status, 404)
                self.assertEqual(resp.error, "Not found")
        # and the patterns themselves, as decoded text
        for path in ("/api/admin/users/٥", "/api/admin/users/１/disable", "/api/admin/users/¹",
                     "/api/admin/users/1٥", "/api/admin/users/1１/disable", "/api/admin/users/1๑"):
            with self.subTest(path=path):
                self.assertFalse(any(p.fullmatch(path) for p, _ in server.ROUTES))

    def test_largest_id_matches(self):
        resp = self.admin("PATCH", "/api/admin/users/9999999999", {"display_name": "X"})
        self.assertEqual((resp.status, resp.error), (404, "User not found"))

    def test_missing_user(self):
        for method, path, body in ENDPOINTS[2:]:
            path = path.format(id=999)
            with self.subTest(method=method, path=path):
                resp = self.admin(method, path, body)
                self.assertEqual(resp.status, 404)
                self.assertEqual(resp.error, "User not found")

    def test_wrong_method(self):
        for method, path in (("GET", "/api/admin/users/1"), ("POST", "/api/admin/users/1"),
                             ("GET", "/api/admin/users/1/disable"), ("PATCH", "/api/admin/users/1/delete"),
                             ("PATCH", "/api/admin/users"), ("HEAD", "/api/admin/users/1/unlock")):
            with self.subTest(method=method, path=path):
                resp = self.admin(method, path)
                self.assertEqual(resp.status, 405)

    def test_unknown_admin_paths(self):
        for path in ("/api/admin", "/api/admin/", "/api/admin/users/", "/api/admin/Users",
                     "/api/admin/users/1/Disable", "/api/admin/users/1/disable/", "/api/admin/users/1/nope",
                     "/api/admin//users/1"):
            with self.subTest(path=path):
                resp = self.admin("POST", path)
                self.assertEqual((resp.status, resp.error), (404, "Not found"))

    def test_query_string_on_parameterised_route(self):
        bob = self.create_id("bob")
        resp = self.admin("POST", f"/api/admin/users/{bob}/promote?x=1&id=1")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["user"]["id"], bob)
        self.assertTrue(resp.json()["user"]["is_admin"])

    def test_head_on_list(self):
        resp = self.admin("HEAD", "/api/admin/users")
        self.assertEqual((resp.status, resp.body), (200, b""))


class CrossSiteTests(AdminTestCase):
    def check_refused(self, method, path, body):
        resp = self.admin(method, path, body, origin=None)
        self.assertEqual((resp.status, resp.error), (403, "Cross-origin request blocked"))
        resp = self.admin(method, path, body, origin="http://evil.example")
        self.assertEqual((resp.status, resp.error), (403, "Cross-origin request blocked"))
        resp = self.admin(method, path, body, content_type="text/plain")
        self.assertEqual((resp.status, resp.error), (415, "Content-Type must be application/json"))

    def test_create(self):
        self.check_refused("POST", "/api/admin/users", {"username": "evil", "display_name": "E", "password": TEMP})
        self.assertEqual(self.user_count(), 1)

    def test_delete(self):
        bob = self.create_id("bob")
        self.check_refused("POST", f"/api/admin/users/{bob}/delete", {"confirm_username": "bob"})
        self.assertIsNotNone(self.row(bob))

    def test_disable(self):
        bob = self.create_id("bob")
        self.check_refused("POST", f"/api/admin/users/{bob}/disable", {})
        self.assertEqual(self.row(bob)["disabled"], 0)


class ListTests(AdminTestCase):
    def test_list(self):
        self.create_id("zed")
        self.create_id("Bob", display_name="<img src=x onerror=alert(1)>")
        self.create_id("carol", is_admin=True)
        resp = self.admin("GET", "/api/admin/users")
        self.assertEqual(resp.status, 200)
        users = resp.json()["users"]
        self.assertEqual([u["username"] for u in users], ["admin", "Bob", "carol", "zed"])
        for user in users:
            self.assertEqual(set(user), ADMIN_KEYS)
        self.assertEqual(users[0], {
            "id": 1, "username": "admin", "display_name": "Admin", "is_admin": True, "disabled": False,
            "locked": False, "must_change_password": False, "temp_password_expired": False,
            "created_at": self.base_time, "last_active_at": self.base_time,  # setup logs in (contract 1b §2)
            "avatar_seed": self.seed_of(1), "photo": None})
        self.assertEqual(users[1]["display_name"], "<img src=x onerror=alert(1)>")
        self.assertIsNone(users[1]["last_active_at"])
        self.assertTrue(users[2]["is_admin"])
        self.assertNotIn(b"scrypt$", resp.body)
        self.assertNotIn(b"password_hash", resp.body)


class CreateTests(AdminTestCase):
    def test_success(self):
        self.clock_offset = 50
        resp = self.create("Bob", display_name="  Bob B  ")
        self.assertEqual(resp.status, 201)
        user = resp.json()["user"]
        self.assertEqual(user, {
            "id": 2, "username": "Bob", "display_name": "Bob B", "is_admin": False, "disabled": False,
            "locked": False, "must_change_password": True, "temp_password_expired": False,
            "created_at": self.base_time + 50, "last_active_at": None, "avatar_seed": self.seed_of(2), "photo": None})
        row = self.row(2)
        self.assertEqual((row["must_change_password"], row["temp_password_set_at"], row["disabled"]),
                         (1, self.base_time + 50, 0))
        self.assertTrue(row["password_hash"].startswith("scrypt$"))
        self.assertIn("User Bob created by admin", self.log.getvalue())
        self.assertNotIn(TEMP, self.log.getvalue())
        self.assertNotIn("Bob B", self.log.getvalue())

    def test_is_admin(self):
        self.assertTrue(self.create("carol", is_admin=True).json()["user"]["is_admin"])
        self.assertFalse(self.create("dave", is_admin=False).json()["user"]["is_admin"])
        self.assertEqual(self.row(2)["is_admin"], 1)
        self.assertEqual(self.row(3)["is_admin"], 0)

    def test_is_admin_must_be_boolean(self):
        for value in (1, 0, "true", "yes", None, [], {}, 1.0):
            with self.subTest(value=value):
                resp = self.create("bob", is_admin=value)
                self.assertEqual((resp.status, resp.error), (400, "is_admin must be true or false"))
        self.assertEqual(self.user_count(), 1)

    def test_server_fields_not_read(self):
        resp = self.admin("POST", "/api/admin/users", {
            "username": "bob", "display_name": "Bob", "password": TEMP, "id": 50, "disabled": True,
            "must_change_password": False, "created_at": 5, "last_active_at": 6, "temp_password_set_at": 7,
            "password_hash": "scrypt$1$1$1$AA==$AA=="})
        self.assertEqual(resp.status, 201)
        row = self.row(2)
        self.assertEqual((row["disabled"], row["must_change_password"], row["created_at"], row["last_active_at"],
                          row["temp_password_set_at"]), (0, 1, self.base_time, None, self.base_time))
        self.assertIsNone(self.row(50))
        self.assertEqual(self.login("bob", TEMP).status, 200)

    def test_validation(self):
        cases = [
            ({"username": "ab", "display_name": "", "password": "short"},
             "Username must be 3-32 characters: letters, digits, . _ -"),
            ({"username": "böb", "display_name": "B", "password": TEMP},
             "Username must be 3-32 characters: letters, digits, . _ -"),
            ({"display_name": "B", "password": TEMP}, "Username must be 3-32 characters: letters, digits, . _ -"),
            ({"username": "bob", "display_name": "", "password": "short"}, "Display name must be 1-50 characters"),
            ({"username": "bob", "display_name": "a‮b", "password": TEMP},
             "Display name contains characters that aren't allowed"),
            ({"username": "bob", "display_name": "Bob", "password": "short", "is_admin": "x"},
             "Password must be 12-128 characters"),
            ({"username": "bob", "display_name": "Bob", "password": "p" * 129}, "Password must be 12-128 characters"),
            ({"username": "bob", "display_name": "Bob"}, "Password must be 12-128 characters"),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                resp = self.admin("POST", "/api/admin/users", body)
                self.assertEqual((resp.status, resp.error), (400, message))
        self.assertEqual(self.user_count(), 1)

    def test_boundaries(self):
        self.assertEqual(self.create("abc", password="p" * 12).status, 201)
        self.assertEqual(self.create("a" * 32, password="p" * 128, display_name="d" * 50).status, 201)
        self.assertEqual(self.create("a" * 33).status, 400)
        self.assertEqual(self.create("abd", password="p" * 11).status, 400)

    def test_duplicate_without_hashing(self):
        self.create_id("Bob")
        counter = CountingSemaphore(self.app.hasher.semaphore)
        self.app.hasher.semaphore = counter
        for name in ("bob", "BOB", "admin", "ADMIN"):
            with self.subTest(name=name):
                resp = self.create(name)
                self.assertEqual((resp.status, resp.error), (409, "That username is taken"))
        self.assertEqual(counter.acquired, 0)
        self.assertEqual(self.user_count(), 2)

    def test_concurrent_creates(self):
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(4)

        def worker(name):
            barrier.wait()
            status = self.create(name).status
            with lock:
                results.append(status)

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("same", "SAME", "Same", "same")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(results), [201, 409, 409, 409])
        self.assertEqual(self.user_count(), 2)

    def test_created_user_is_forced_to_change(self):
        self.create_id("bob")
        resp = self.login("bob", TEMP)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["user"]["must_change_password"], True)
        token = cookie_value(resp.set_cookies(self.cookie_name)[0])
        resp = self.request("PATCH", "/api/account", {"display_name": "X"}, token=token)
        self.assertEqual((resp.status, resp.json()), (403, PENDING))

    def test_created_admin_is_forced_to_change_before_admin_screen(self):
        self.create_id("carol", is_admin=True)
        token = self.login_token("carol", TEMP)
        resp = self.request("GET", "/api/admin/users", token=token)
        self.assertEqual((resp.status, resp.json()), (403, PENDING))
        self.assertEqual(self.change_own_password(token, TEMP, NEW).status, 200)
        self.assertEqual(self.request("GET", "/api/admin/users", token=token).status, 200)


class UpdateTests(AdminTestCase):
    def test_update_other(self):
        bob = self.create_id("bob")
        resp = self.admin("PATCH", f"/api/admin/users/{bob}", {"display_name": "  René "})
        self.assertEqual(resp.status, 200)
        user = resp.json()["user"]
        self.assertEqual(set(user), ADMIN_KEYS)
        self.assertEqual(user["display_name"], "René")
        self.assertEqual(self.row(bob)["display_name"], "René")
        self.assertNotIn("Ren", self.log.getvalue())

    def test_update_self(self):
        resp = self.admin("PATCH", "/api/admin/users/1", {"display_name": "Boss"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["user"]["display_name"], "Boss")
        self.assertEqual(self.session_user(self.token)["display_name"], "Boss")

    def test_validation(self):
        bob = self.create_id("bob")
        for body, message in (({}, "Nothing to update"), ({"username": "x"}, "Nothing to update"),
                              ({"display_name": ""}, "Display name must be 1-50 characters"),
                              ({"display_name": None}, "Display name must be 1-50 characters"),
                              ({"display_name": "d" * 51}, "Display name must be 1-50 characters"),
                              ({"display_name": "a\u0000"}, "Display name contains characters that aren't allowed")):
            with self.subTest(body=body):
                resp = self.admin("PATCH", f"/api/admin/users/{bob}", body)
                self.assertEqual((resp.status, resp.error), (400, message))
        self.assertEqual(self.row(bob)["display_name"], "Bob")

    def test_username_not_changed(self):
        bob = self.create_id("bob")
        resp = self.admin("PATCH", f"/api/admin/users/{bob}", {"display_name": "B", "username": "robert",
                                                               "is_admin": True, "disabled": True})
        self.assertEqual(resp.status, 200)
        row = self.row(bob)
        self.assertEqual((row["username"], row["is_admin"], row["disabled"]), ("bob", 0, 0))


class ResetTests(AdminTestCase):
    env = {"WORLD_EATS_TRUSTED_PROXIES": "127.0.0.1"}

    def reset(self, user_id, password=TEMP2):
        return self.action(user_id, "password", {"new_password": password})

    def test_self(self):
        resp = self.reset(1)
        self.assertEqual((resp.status, resp.error), (403, "Change your own password in Account settings"))
        self.assertEqual(self.login().status, 200)

    def test_validation_before_lookup(self):
        for value in ("short", None, 5, "p" * 129):
            with self.subTest(value=value):
                resp = self.reset(999, value)
                self.assertEqual((resp.status, resp.error), (400, "Password must be 12-128 characters"))

    def test_missing_user_without_hashing(self):
        counter = CountingSemaphore(self.app.hasher.semaphore)
        self.app.hasher.semaphore = counter
        resp = self.reset(999)
        self.assertEqual((resp.status, resp.error), (404, "User not found"))
        self.assertEqual(counter.acquired, 0)

    def test_success(self):
        bob_id, bob = self.user_with_own_password("bob")
        bob2 = self.login_token("bob")
        for i in range(5):
            self.login("bob", "wrong password!", headers={"X-Forwarded-For": f"203.0.113.{i}"})
        self.assertTrue(self.users()["bob"]["locked"])
        self.clock_offset = 30
        resp = self.reset(bob_id)
        self.assertEqual(resp.status, 200)
        user = resp.json()["user"]
        self.assertEqual(set(user), ADMIN_KEYS)
        self.assertEqual((user["must_change_password"], user["locked"], user["temp_password_expired"]),
                         (True, False, False))
        row = self.row(bob_id)
        self.assertEqual((row["must_change_password"], row["temp_password_set_at"]), (1, self.base_time + 30))
        self.assertEqual(self.sessions_for(bob_id), [])
        self.assertIsNone(self.session_user(bob))
        self.assertIsNone(self.session_user(bob2))
        self.assertIsNotNone(self.session_user(self.token), "the admin's own session is untouched")
        self.assertNotIn("bob", self.app.lockout.users)
        self.assertEqual(len(self.app.lockout.ips["203.0.113.0"].failures), 1, "IP keys untouched")
        self.assertEqual(self.login("bob", PASSWORD, headers={"X-Forwarded-For": "198.51.100.1"}).status, 401)
        resp = self.login("bob", TEMP2, headers={"X-Forwarded-For": "198.51.100.2"})
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.json()["user"]["must_change_password"])
        log = self.log.getvalue()
        self.assertIn("Password reset for bob by admin", log)
        self.assertNotIn(TEMP2, log)

    def test_reset_of_pending_user_refreshes_timestamp(self):
        bob = self.create_id("bob")
        self.clock_offset = 3 * 86400
        self.assertEqual(self.reset(bob).status, 200)
        self.assertEqual(self.row(bob)["temp_password_set_at"], self.base_time + 3 * 86400)

    def test_reset_admin(self):
        carol_id, carol = self.user_with_own_password("carol", is_admin=True)
        self.assertEqual(self.reset(carol_id).status, 200)
        self.assertIsNone(self.session_user(carol))
        carol = self.login_token("carol", TEMP2)
        resp = self.request("GET", "/api/admin/users", token=carol)
        self.assertEqual((resp.status, resp.json()), (403, PENDING))


class DisableEnableTests(AdminTestCase):
    def test_self(self):
        resp = self.action(1, "disable")
        self.assertEqual((resp.status, resp.error), (403, "You can't disable your own account"))
        self.assertEqual(self.row(1)["disabled"], 0)

    def test_disable(self):
        bob_id, bob = self.user_with_own_password("bob")
        resp = self.action(bob_id, "disable")
        self.assertEqual(resp.status, 200)
        self.assertEqual(set(resp.json()["user"]), ADMIN_KEYS)
        self.assertTrue(resp.json()["user"]["disabled"])
        self.assertEqual(self.sessions_for(bob_id), [])
        resp = self.request("GET", "/api/session", token=bob)
        self.assertIsNone(resp.json()["user"])
        self.assertIn("Max-Age=0", cookie_attributes(resp.set_cookies(self.cookie_name)[0]))
        self.assertIn("User bob disabled by admin", self.log.getvalue())
        # disabling again is a no-op
        resp = self.action(bob_id, "disable")
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.json()["user"]["disabled"])

    def test_disabled_flag_with_live_session(self):
        bob_id, bob = self.user_with_own_password("bob")
        self.set_disabled(bob_id)
        self.assertEqual(len(self.sessions_for(bob_id)), 1)
        for method, path, body in (("GET", "/api/session", None), ("PATCH", "/api/account", {"display_name": "X"}),
                                   ("POST", "/api/account/logout-everywhere", {})):
            with self.subTest(path=path):
                resp = self.request(method, path, body, token=bob)
                if path == "/api/session":
                    self.assertIsNone(resp.json()["user"])
                else:
                    self.assertEqual((resp.status, resp.error), (401, "Not logged in"))
                self.assertIn("Max-Age=0", cookie_attributes(resp.set_cookies(self.cookie_name)[0]))

    def test_disabled_login_is_a_wrong_password(self):
        bob_id = self.insert_user("bob")
        self.assertEqual(self.action(bob_id, "disable").status, 200)
        right = self.login("bob", PASSWORD)
        self.app.lockout.users.clear()
        self.app.lockout.ips.clear()
        wrong = self.login("bob", "wrong password!")
        self.assertEqual((right.status, right.body), (wrong.status, wrong.body))
        self.assertEqual((right.status, right.error), (401, "Wrong username or password"))
        self.assertIsNone(right.headers.get_all("Set-Cookie"))
        self.app.lockout.users.clear()
        self.app.lockout.ips.clear()
        for _ in range(5):
            self.assertEqual(self.login("bob", PASSWORD).status, 401)
        self.assertEqual(len(self.app.lockout.users["bob"].failures), 5)
        self.assertEqual(len(self.app.lockout.ips["127.0.0.1"].failures), 5)
        self.assertEqual(self.login("bob", PASSWORD).status, 429)
        self.assertEqual(self.sessions_for(bob_id), [])

    def test_enable(self):
        bob_id = self.insert_user("bob")
        self.action(bob_id, "disable")
        resp = self.action(bob_id, "enable")
        self.assertEqual(resp.status, 200)
        self.assertFalse(resp.json()["user"]["disabled"])
        self.assertEqual(self.login("bob", PASSWORD).status, 200)
        self.assertIn("User bob enabled by admin", self.log.getvalue())
        self.assertEqual(self.action(bob_id, "enable").status, 200)
        self.assertEqual(self.action(1, "enable").status, 200, "enabling yourself is a harmless no-op")

    def test_disable_admin_with_other_admin(self):
        carol_id, carol = self.user_with_own_password("carol", is_admin=True)
        self.assertEqual(self.action(carol_id, "disable").status, 200)
        self.assertEqual(self.request("GET", "/api/admin/users", token=carol).status, 401)

    def test_setup_stays_closed_when_all_accounts_disabled(self):
        self.insert_user("bob")
        with self.db() as conn:
            conn.execute("UPDATE users SET disabled = 1")
        self.assertEqual(self.request("GET", "/api/session").json(), {"setup_required": False, "user": None})
        resp = self.request("POST", "/api/setup", {"username": "intruder", "display_name": "I", "password": TEMP})
        self.assertEqual((resp.status, resp.error), (409, "Setup is already complete"))
        self.assertEqual(self.user_count(), 2)


class PromoteDemoteTests(AdminTestCase):
    def test_promote(self):
        bob_id, bob = self.user_with_own_password("bob")
        self.assertEqual(self.request("GET", "/api/admin/users", token=bob).status, 403)
        resp = self.action(bob_id, "promote")
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.json()["user"]["is_admin"])
        self.assertTrue(self.session_user(bob)["is_admin"])
        self.assertEqual(self.request("GET", "/api/admin/users", token=bob).status, 200)
        self.assertIn("User bob promoted by admin", self.log.getvalue())
        self.assertEqual(self.action(bob_id, "promote").status, 200)

    def test_demote_other(self):
        carol_id, carol = self.user_with_own_password("carol", is_admin=True)
        resp = self.action(carol_id, "demote")
        self.assertEqual(resp.status, 200)
        self.assertFalse(resp.json()["user"]["is_admin"])
        resp = self.request("GET", "/api/admin/users", token=carol)
        self.assertEqual((resp.status, resp.error), (403, "Admins only"))
        self.assertIn("User carol demoted by admin", self.log.getvalue())
        self.assertEqual(self.action(carol_id, "demote").status, 200, "already not an admin")

    def test_demote_self_with_other_admin(self):
        self.insert_user("carol", is_admin=True)
        resp = self.action(1, "demote")
        self.assertEqual(resp.status, 200)
        self.assertFalse(resp.json()["user"]["is_admin"])
        self.assertFalse(self.session_user(self.token)["is_admin"])
        resp = self.admin("GET", "/api/admin/users")
        self.assertEqual((resp.status, resp.error), (403, "Admins only"))

    def test_demote_last_admin(self):
        self.insert_user("bob")
        resp = self.action(1, "demote")
        self.assertEqual((resp.status, resp.error), (409, "You're the only admin, so you can't remove your own admin rights"))
        self.assertEqual(self.row(1)["is_admin"], 1)

    def test_disabled_admin_does_not_count(self):
        carol = self.insert_user("carol", is_admin=True)
        self.set_disabled(carol)
        resp = self.action(1, "demote")
        self.assertEqual(resp.status, 409)
        self.assertEqual(self.row(1)["is_admin"], 1)

    def test_promoting_disabled_user_does_not_count(self):
        bob = self.insert_user("bob")
        self.assertEqual(self.action(bob, "disable").status, 200)
        resp = self.action(bob, "promote")
        self.assertEqual(resp.status, 200)
        self.assertEqual((resp.json()["user"]["is_admin"], resp.json()["user"]["disabled"]), (True, True))
        self.assertEqual(self.action(1, "demote").status, 409)
        self.assertEqual(self.request("POST", "/api/account/delete", {"password": PASSWORD},
                                      token=self.token).status, 409)
        # once re-enabled, they count
        self.assertEqual(self.action(bob, "enable").status, 200)
        self.assertEqual(self.action(1, "demote").status, 200)


class PendingAdminCountTests(AdminTestCase):
    """An admin with a temporary password can't use the admin screen yet, so they don't count."""

    def setUp(self):
        super().setUp()
        self.carol = self.create_id("carol", is_admin=True)  # pending: must_change_password = 1

    def carol_chooses_password(self):
        token = self.login_token("carol", TEMP)
        self.assertEqual(self.change_own_password(token, TEMP, NEW).status, 200)

    def delete_own_account(self):
        return self.request("POST", "/api/account/delete", {"password": PASSWORD}, token=self.token)

    def test_demote_self(self):
        resp = self.action(1, "demote")
        self.assertEqual((resp.status, resp.error), (409, "You're the only admin, so you can't remove your own admin rights"))
        self.carol_chooses_password()
        self.assertEqual(self.action(1, "demote").status, 200)

    def test_delete_own_account(self):
        resp = self.delete_own_account()
        self.assertEqual((resp.status, resp.error), (409, "You're the only admin, so this account can't be deleted"))
        self.assertIsNotNone(self.row(1))
        self.carol_chooses_password()
        self.assertEqual(self.delete_own_account().status, 200)
        self.assertIsNone(self.row(1))

    def test_reset_makes_admin_pending(self):
        self.carol_chooses_password()
        self.assertEqual(self.action(self.carol, "password", {"new_password": TEMP2}).status, 200)
        self.assertEqual(self.action(1, "demote").status, 409)
        self.assertEqual(self.delete_own_account().status, 409)

    def test_pending_admin_can_still_be_managed(self):
        # The caller counts, so acting on the pending admin is never blocked by the rule.
        self.assertEqual(self.action(self.carol, "demote").status, 200)
        self.assertEqual(self.action(self.carol, "promote").status, 200)
        self.assertEqual(self.action(self.carol, "disable").status, 200)
        self.assertEqual(self.action(self.carol, "delete", {"confirm_username": "carol"}).status, 200)


class LastAdminRaceTests(AdminTestCase):
    def race(self, calls):
        results = [None] * len(calls)
        barrier = threading.Barrier(len(calls))

        def worker(i):
            barrier.wait()
            results[i] = calls[i]().status

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(calls))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        return results

    def enabled_admins(self):
        with self.db() as conn:
            return conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1 AND disabled = 0").fetchone()[0]

    def test_demote_each_other(self):
        for _ in range(3):
            with self.db() as conn:
                others = [r[0] for r in conn.execute("SELECT id FROM users WHERE id != 1")]
            self.delete_user_rows(*others)
            with self.db() as conn:
                conn.execute("UPDATE users SET is_admin = 1")
            carol_id, carol = self.user_with_own_password("carol", is_admin=True)
            results = self.race([lambda: self.action(carol_id, "demote"),
                                 lambda: self.action(1, "demote", token=carol)])
            self.assertEqual(results.count(200), 1, results)
            self.assertTrue(all(r in (200, 403, 409) for r in results), results)
            self.assertEqual(self.enabled_admins(), 1)

    def test_disable_each_other(self):
        carol_id, carol = self.user_with_own_password("carol", is_admin=True)
        results = self.race([lambda: self.action(carol_id, "disable"),
                             lambda: self.action(1, "disable", token=carol)])
        self.assertEqual(results.count(200), 1, results)
        # the loser was logged out by the winner (401) or refused by the re-check (403/409)
        self.assertTrue(all(r in (200, 401, 403, 409) for r in results), results)
        self.assertEqual(self.enabled_admins(), 1)

    def test_delete_each_other(self):
        carol_id, carol = self.user_with_own_password("carol", is_admin=True)
        results = self.race([lambda: self.action(carol_id, "delete", {"confirm_username": "carol"}),
                             lambda: self.action(1, "delete", {"confirm_username": "admin"}, token=carol)])
        self.assertEqual(results.count(200), 1, results)
        self.assertEqual(self.enabled_admins(), 1)


class RevokedMidRequestTests(AdminTestCase):
    """The caller's admin status changes after their session loaded but before the handler's transaction."""

    env = {"WORLD_EATS_TRUSTED_PROXIES": "127.0.0.1"}

    def setUp(self):
        super().setUp()
        self.mid_request = None
        original = server.Handler.load_session

        def load_session(handler, renew):
            original(handler, renew)
            if self.mid_request is not None and (handler.raw_path or "").startswith("/api/admin/"):
                action, self.mid_request = self.mid_request, None
                action()

        patcher = mock.patch.object(server.Handler, "load_session", load_session)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.insert_user("carol", is_admin=True)  # so demoting admin isn't blocked by the last-admin rule
        self.bob = self.insert_user("bob")

    def demote(self):
        self.set_admin(1, 0)

    def disable(self):
        self.set_disabled(1)

    def pend(self):
        self.set_pending(1)

    def logout(self):
        with self.db() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id = 1")

    def lock_bob(self):
        for i in range(5):
            self.login("bob", "wrong password!", headers={"X-Forwarded-For": f"203.0.113.{i}"})
        self.assertTrue(self.app.lockout.is_locked("bob"))

    def snapshot(self):
        with self.db() as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT id, username, display_name, password_hash, is_admin, disabled, must_change_password "
                "FROM users WHERE id != 1 ORDER BY id")]

    def test_writes_refused(self):
        self.lock_bob()
        calls = [
            ("POST", "/api/admin/users", {"username": "newbie", "display_name": "N", "password": TEMP}),
            ("PATCH", f"/api/admin/users/{self.bob}", {"display_name": "Robert"}),
            ("POST", f"/api/admin/users/{self.bob}/password", {"new_password": TEMP2}),
            ("POST", f"/api/admin/users/{self.bob}/disable", {}),
            ("POST", f"/api/admin/users/{self.bob}/enable", {}),
            ("POST", f"/api/admin/users/{self.bob}/promote", {}),
            ("POST", "/api/admin/users/2/demote", {}),
            ("POST", f"/api/admin/users/{self.bob}/unlock", {}),
            ("POST", f"/api/admin/users/{self.bob}/delete", {"confirm_username": "bob"}),
        ]
        for change, status, message in ((self.demote, 403, "Admins only"), (self.disable, 403, "Admins only"),
                                        (self.pend, 403, "Admins only"), (self.logout, 401, "Not logged in")):
            for method, path, body in calls:
                with self.subTest(change=change.__name__, path=path):
                    with self.db() as conn:
                        conn.execute("UPDATE users SET is_admin = 1, disabled = 0, must_change_password = 0 "
                                     "WHERE id = 1")
                    if not self.sessions_for(1):
                        self.token = self.login_token()
                    before = self.snapshot()
                    self.mid_request = change
                    resp = self.admin(method, path, body)
                    self.assertIsNone(self.mid_request, "the change ran")
                    self.assertEqual((resp.status, resp.error), (status, message))
                    self.assertEqual(self.snapshot(), before)
                    self.assertTrue(self.app.lockout.is_locked("bob"), "lock unchanged")
                    self.assertNotIn("newbie", [r[1] for r in self.snapshot()])


class UnlockTests(AdminTestCase):
    env = {"WORLD_EATS_TRUSTED_PROXIES": "127.0.0.1"}

    def test_unlock(self):
        bob = self.insert_user("bob")
        for i in range(4):
            self.login("bob", "wrong password!", headers={"X-Forwarded-For": f"203.0.113.{i}"})
        self.assertFalse(self.users()["bob"]["locked"])
        self.login("bob", "wrong password!", headers={"X-Forwarded-For": "203.0.113.4"})
        self.assertTrue(self.users()["bob"]["locked"])
        self.assertEqual(self.login("bob", PASSWORD, headers={"X-Forwarded-For": "198.51.100.1"}).status, 429)
        resp = self.action(bob, "unlock")
        self.assertEqual(resp.status, 200)
        self.assertEqual(set(resp.json()["user"]), ADMIN_KEYS)
        self.assertFalse(resp.json()["user"]["locked"])
        self.assertFalse(self.users()["bob"]["locked"])
        for i in range(5):
            self.assertEqual(len(self.app.lockout.ips[f"203.0.113.{i}"].failures), 1, "IP keys untouched")
        self.assertEqual(self.login("bob", PASSWORD, headers={"X-Forwarded-For": "198.51.100.2"}).status, 200)

    def test_unlock_does_not_unlock_ip(self):
        bob = self.insert_user("bob")
        for _ in range(5):
            self.login("bob", "wrong password!", headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(self.action(bob, "unlock").status, 200)
        resp = self.login("bob", PASSWORD, headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(resp.status, 429)

    def test_lock_expires(self):
        self.insert_user("bob")
        for i in range(5):
            self.login("bob", "wrong password!", headers={"X-Forwarded-For": f"203.0.113.{i}"})
        self.assertTrue(self.users()["bob"]["locked"])
        self.clock_offset = 900
        self.assertFalse(self.users()["bob"]["locked"])

    def test_unlock_unlocked_user(self):
        bob = self.insert_user("bob")
        resp = self.action(bob, "unlock")
        self.assertEqual(resp.status, 200)
        self.assertFalse(resp.json()["user"]["locked"])

    def test_locked_is_case_insensitive(self):
        self.insert_user("Bob")
        for i in range(5):
            self.login("BOB", "wrong password!", headers={"X-Forwarded-For": f"203.0.113.{i}"})
        self.assertTrue(self.users()["Bob"]["locked"])


class DeleteTests(AdminTestCase):
    def delete(self, user_id, confirm, token="default"):
        body = {} if confirm is ... else {"confirm_username": confirm}
        return self.action(user_id, "delete", body, token=token)

    def test_self(self):
        resp = self.delete(1, "admin")
        self.assertEqual((resp.status, resp.error), (403, "Delete your own account in Account settings"))
        self.assertIsNotNone(self.row(1))

    def test_wrong_confirmation(self):
        bob = self.create_id("bob")
        for confirm in ("bobb", "bo", "", " bob", "bob ", "admin", 5, None, ["bob"], {"bob": 1}, True, ...):
            with self.subTest(confirm=confirm):
                resp = self.delete(bob, confirm)
                self.assertEqual((resp.status, resp.error), (400, "Type the username to confirm"))
        self.assertIsNotNone(self.row(bob))

    def test_success(self):
        bob_id = self.insert_user("Bob")
        bob = self.login_token("Bob")
        self.app.lockout.reserve("bob", None)
        self.app.lockout.finish("bob", None, auth.FAILED)
        resp = self.delete(bob_id, "bOB")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertIsNone(self.row(bob_id))
        self.assertEqual(self.sessions_for(bob_id), [])
        self.assertNotIn("bob", self.app.lockout.users)
        self.assertIn("User Bob deleted by admin", self.log.getvalue())
        # the deleted user's old cookie is cleared
        resp = self.request("GET", "/api/session", token=bob)
        self.assertIsNone(resp.json()["user"])
        self.assertIn("Max-Age=0", cookie_attributes(resp.set_cookies(self.cookie_name)[0]))
        resp = self.request("PATCH", "/api/account", {"display_name": "X"}, token=bob)
        self.assertEqual(resp.status, 401)
        self.assertIn("Max-Age=0", cookie_attributes(resp.set_cookies(self.cookie_name)[0]))
        # the username can be used again
        self.assertEqual(self.create("bob").status, 201)
        self.assertEqual(self.delete(999, "bob").status, 404)

    def test_delete_disabled_admin_while_only_enabled_admin(self):
        carol = self.insert_user("carol", is_admin=True)
        self.set_disabled(carol)
        resp = self.delete(carol, "carol")
        self.assertEqual(resp.status, 200)
        self.assertIsNone(self.row(carol))

    def test_delete_other_admin(self):
        carol = self.insert_user("carol", is_admin=True)
        self.assertEqual(self.delete(carol, "carol").status, 200)
        self.assertEqual(self.action(1, "demote").status, 409)


class AccountDeleteRegressionTests(AdminTestCase):
    def test_only_fellow_admin_disabled(self):
        carol = self.insert_user("carol", is_admin=True)
        self.set_disabled(carol)
        resp = self.request("POST", "/api/account/delete", {"password": PASSWORD}, token=self.token)
        self.assertEqual((resp.status, resp.error), (409, "You're the only admin, so this account can't be deleted"))
        self.assertIsNotNone(self.row(1))
        self.set_disabled(carol, 0)
        self.assertEqual(self.request("POST", "/api/account/delete", {"password": PASSWORD},
                                      token=self.token).status, 200)


class LastActiveTests(AdminTestCase):
    def test_login_and_renewal(self):
        bob = self.insert_user("bob")
        self.assertIsNone(self.row(bob)["last_active_at"])
        self.clock_offset = 100
        token = self.login_token("bob")
        self.assertEqual(self.row(bob)["last_active_at"], self.base_time + 100)
        self.clock_offset = 100 + 3599
        self.session_user(token)
        self.assertEqual(self.row(bob)["last_active_at"], self.base_time + 100, "no renewal within the hour")
        self.clock_offset = 100 + 3600
        self.session_user(token)
        self.assertEqual(self.row(bob)["last_active_at"], self.base_time + 3700)
        self.assertEqual(self.users()["bob"]["last_active_at"], self.base_time + 3700)

    def test_failed_login_does_not_update(self):
        bob = self.insert_user("bob")
        self.login("bob", "wrong password!")
        self.assertIsNone(self.row(bob)["last_active_at"])


class LoggingTests(AdminTestCase):
    def test_admin_actions_logged_without_secrets(self):
        bob = self.create_id("bob", password="created-secret-pw", display_name="Secret Display")
        self.action(bob, "password", {"new_password": "reset-secret-pw"})
        self.action(bob, "disable")
        self.action(bob, "enable")
        self.action(bob, "promote")
        self.action(bob, "demote")
        self.action(bob, "unlock")
        self.admin("PATCH", f"/api/admin/users/{bob}", {"display_name": "Other Secret"})
        self.action(bob, "delete", {"confirm_username": "bob"})
        log = self.log.getvalue()
        for line in ("User bob created by admin", "Password reset for bob by admin", "User bob disabled by admin",
                     "User bob enabled by admin", "User bob promoted by admin", "User bob demoted by admin",
                     "User bob deleted by admin"):
            self.assertIn(line, log)
        for secret in ("created-secret-pw", "reset-secret-pw", "Secret Display", "Other Secret", "scrypt$",
                       "confirm_username"):
            self.assertNotIn(secret, log)


class ExpiryTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.bob = self.create_id("bob")  # temporary password set at base_time

    def test_login_just_before_and_at_expiry(self):
        self.clock_offset = WEEK - 1
        self.assertFalse(self.users()["bob"]["temp_password_expired"])
        self.assertEqual(self.login("bob", TEMP).status, 200)
        self.clock_offset = WEEK
        self.assertTrue(self.users()["bob"]["temp_password_expired"])
        resp = self.login("bob", TEMP)
        self.assertEqual((resp.status, resp.error), (401, "Wrong username or password"))
        self.assertEqual(len(self.app.lockout.users["bob"].failures), 1)
        self.assertEqual(len(self.app.lockout.ips["127.0.0.1"].failures), 1)

    def test_expired_on_password_change(self):
        self.clock_offset = WEEK - 1
        token = self.login_token("bob", TEMP)
        self.clock_offset = WEEK
        resp = self.change_own_password(token, TEMP, NEW)
        self.assertEqual((resp.status, resp.error), (403, "Current password is wrong"))
        self.assertEqual(len(self.app.lockout.users["bob"].failures), 1)
        self.assertEqual(self.row(self.bob)["must_change_password"], 1)

    def test_changed_password_never_expires(self):
        token = self.login_token("bob", TEMP)
        self.assertEqual(self.change_own_password(token, TEMP, NEW).status, 200)
        self.assertIsNone(self.row(self.bob)["temp_password_set_at"])
        self.clock_offset = 4 * WEEK  # well past expiry, still inside the admin's 30-day session
        self.assertFalse(self.users()["bob"]["temp_password_expired"])
        self.assertEqual(self.login("bob", NEW).status, 200)

    def test_reset_starts_a_new_week(self):
        self.clock_offset = WEEK
        self.assertEqual(self.login("bob", TEMP).status, 401)
        self.assertEqual(self.action(self.bob, "password", {"new_password": TEMP2}).status, 200)
        self.assertEqual(self.row(self.bob)["temp_password_set_at"], self.base_time + WEEK)
        self.assertFalse(self.users()["bob"]["temp_password_expired"])
        self.clock_offset = 2 * WEEK - 1
        self.assertEqual(self.login("bob", TEMP2).status, 200)

    def test_existing_session_not_ended(self):
        token = self.login_token("bob", TEMP)
        self.clock_offset = WEEK
        user = self.session_user(token)
        self.assertTrue(user["must_change_password"])

    def test_expired_admin_set_flag_without_timestamp(self):
        # must_change_password with no temp_password_set_at never expires
        self.set_pending(1)
        self.clock_offset = 10 * WEEK
        self.assertEqual(self.login().status, 200)


class LoginRaceTests(AdminTestCase):
    """An admin resets or disables the account while a login is hashing (review L1)."""

    def setUp(self):
        super().setUp()
        self.bob = self.insert_user("bob")
        self.after_verify = None
        hasher = self.app.hasher
        original = hasher.verify

        def verify(password, stored):
            result = original(password, stored)
            if self.after_verify is not None:
                action, self.after_verify = self.after_verify, None
                action()
            return result

        hasher.verify = verify  # test seam: runs a concurrent change right after the check

    def check_refused(self, change):
        # Earlier failures are neither cleared (as a success would) nor added to (NO_RESULT).
        for _ in range(2):
            self.assertEqual(self.login("bob", "wrong password!").status, 401)
        lockout = self.app.lockout
        user_before = list(lockout.users["bob"].failures)
        ip_before = list(lockout.ips["127.0.0.1"].failures)
        self.assertEqual(len(user_before), 2)
        self.after_verify = change
        resp = self.login("bob", PASSWORD)
        self.assertIsNone(self.after_verify, "the change ran")
        self.assertEqual((resp.status, resp.error), (401, "Wrong username or password"))
        self.assertIsNone(resp.headers.get_all("Set-Cookie"))
        self.assertEqual(self.sessions_for(self.bob), [])
        self.assertIsNone(self.row(self.bob)["last_active_at"])
        self.assertEqual(lockout.users["bob"].failures, user_before)
        self.assertEqual(lockout.ips["127.0.0.1"].failures, ip_before)
        self.assertEqual((lockout.users["bob"].in_flight, lockout.ips["127.0.0.1"].in_flight), (0, 0))

    def test_reset_during_login(self):
        new_hash = self.app.hasher.hash(TEMP2)

        def reset():
            with self.db() as conn:
                conn.execute("UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
                             (new_hash, self.bob))

        self.check_refused(reset)

    def test_disable_during_login(self):
        self.check_refused(lambda: self.set_disabled(self.bob))

    def test_delete_during_login(self):
        def delete():
            self.delete_user_rows(self.bob)

        self.after_verify = delete
        resp = self.login("bob", PASSWORD)
        self.assertEqual(resp.status, 401)
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1, "only the admin's")


class NoSecretsInResponsesTests(AdminTestCase):
    def test_no_hash_in_any_response(self):
        bodies = []
        bob = self.create_id("bob")
        for method, path, body in ENDPOINTS:
            if path.endswith("/delete"):
                continue
            resp = self.admin(method, path.format(id=bob), body)
            self.assertIn(resp.status, (200, 201), (path, resp))
            bodies.append(resp.body)
            data = resp.json()
            for user in data.get("users", [data.get("user")]):
                self.assertEqual(set(user), ADMIN_KEYS)
        bodies.append(self.login("bob", TEMP2).body)
        bodies.append(self.request("GET", "/api/session", token=self.token).body)
        for body in bodies:
            self.assertNotIn(b"scrypt", body)
            self.assertNotIn(b"password", body.replace(b"must_change_password", b"").replace(
                b"temp_password_expired", b""))


class LockoutUnitTests(ServerTestCase):
    def test_is_locked_ignores_in_flight(self):
        lockout = self.app.lockout
        for _ in range(5):
            self.assertTrue(lockout.reserve("bob", None))
        self.assertFalse(lockout.is_locked("bob"))
        self.assertFalse(lockout.reserve("bob", None))
        for _ in range(5):
            lockout.finish("bob", None, auth.FAILED)
        self.assertTrue(lockout.is_locked("bob"))
        self.assertFalse(lockout.is_locked("nobody"))

    def test_clear_user_keeps_in_flight(self):
        lockout = self.app.lockout
        for _ in range(4):
            lockout.reserve("bob", "10.0.0.1")
            lockout.finish("bob", "10.0.0.1", auth.FAILED)
        lockout.reserve("bob", None)
        lockout.clear_user("bob")
        self.assertEqual(lockout.users["bob"].failures, [])
        self.assertEqual(lockout.users["bob"].in_flight, 1)
        self.assertEqual(len(lockout.ips["10.0.0.1"].failures), 4)
        lockout.finish("bob", None, auth.NO_RESULT)
        lockout.clear_user("bob")
        self.assertNotIn("bob", lockout.users)
        lockout.clear_user("never-seen")
