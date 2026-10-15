"""Feature 2: passports (contract .claude/specs/2-passports.md, section 8)."""

import threading
from unittest import mock

from tests.helpers import (PASSWORD, ServerTestCase, db, fast_hasher, passports, server, sha256_hex)

NOT_FOUND = {"error": "Passport not found"}
OWNER_ONLY = "Only the owner can do that"
PENDING = {"error": "You must change your password first", "password_change_required": True}
PERSON_KEYS = {"id", "username", "display_name", "inactive"}
SUMMARY_KEYS = {"id", "name", "is_personal", "role", "is_default", "owner"}
DETAIL_PASSPORT_KEYS = SUMMARY_KEYS | {"created_at"}
TEMP = "temporary password 1"


def ref(user_id, username):
    return {"user_id": user_id, "username": username}


class PassportTestCase(ServerTestCase):
    """Admin "admin" (id 1, self.token). Every test ends with the schema invariants checked."""

    def setUp(self):
        super().setUp()
        self.token = self.setup_admin()
        self.deleted_ids = []

    def tearDown(self):
        try:
            self.assert_invariants()
        finally:
            super().tearDown()

    # --- Requests ---

    def api(self, method, path, body=None, token="default", **kwargs):
        if body is None and method in ("POST", "PATCH"):
            body = {}
        return self.request(method, path, body, token=self.token if token == "default" else token, **kwargs)

    def user(self, username, display_name=None):
        """A user with their own password: (id, token)."""
        user_id = self.insert_user(username, display_name=display_name)
        return user_id, self.login_token(username)

    def create(self, name="Date nights", participants=(), viewers=(), token="default"):
        return self.api("POST", "/api/passports", {"name": name, "participants": list(participants),
                                                   "viewers": list(viewers)}, token=token)

    def create_id(self, *args, **kwargs):
        resp = self.create(*args, **kwargs)
        self.assertEqual(resp.status, 201, resp)
        return resp.json()["passport"]["id"]

    def get(self, passport_id, token="default"):
        return self.api("GET", f"/api/passports/{passport_id}", token=token)

    def add(self, passport_id, user_id, username, role, token="default"):
        return self.api("POST", f"/api/passports/{passport_id}/members",
                        {**ref(user_id, username), "role": role}, token=token)

    def remove(self, passport_id, user_id, token="default"):
        return self.api("POST", f"/api/passports/{passport_id}/members/{user_id}/remove", token=token)

    def leave(self, passport_id, token="default"):
        return self.api("POST", f"/api/passports/{passport_id}/leave", token=token)

    def delete(self, passport_id, confirm, token="default"):
        return self.api("POST", f"/api/passports/{passport_id}/delete", {"confirm_name": confirm}, token=token)

    def set_default(self, value, token="default"):
        return self.api("PATCH", "/api/account", {"default_passport_id": value}, token=token)

    def listing(self, token="default"):
        resp = self.api("GET", "/api/passports", token=token)
        self.assertEqual(resp.status, 200, resp)
        return resp.json()

    def admin_action(self, user_id, name, body=None):
        return self.api("POST", f"/api/admin/users/{user_id}/{name}", body)

    def admin_delete(self, user_id, username):
        resp = self.admin_action(user_id, "delete", {"confirm_username": username})
        self.assertEqual(resp.status, 200, resp)
        self.deleted_ids.append(user_id)

    def self_delete(self, user_id, token):
        resp = self.api("POST", "/api/account/delete", {"password": PASSWORD}, token=token)
        self.assertEqual(resp.status, 200, resp)
        self.deleted_ids.append(user_id)

    # --- Direct database access ---

    def query(self, sql, params=()):
        with self.db() as conn:
            return [tuple(r) for r in conn.execute(sql, params)]

    def personal_id(self, user_id):
        return self.query("SELECT id FROM passports WHERE owner_id = ? AND is_personal = 1", (user_id,))[0][0]

    def owner_of(self, passport_id):
        rows = self.query("SELECT owner_id FROM passports WHERE id = ?", (passport_id,))
        return rows[0][0] if rows else None

    def members(self, passport_id):
        return self.query("SELECT user_id, role FROM passport_members WHERE passport_id = ? ORDER BY id",
                          (passport_id,))

    def stored_default(self, user_id):
        return self.query("SELECT default_passport_id FROM users WHERE id = ?", (user_id,))[0][0]

    def insert_passport(self, owner_id, name="Inserted", participants=(), viewers=()):
        with self.db() as conn:
            return passports.create(conn, name, owner_id, list(participants), list(viewers), self.base_time)

    def set_disabled(self, user_id, value=1):
        with self.db() as conn:
            conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (value, user_id))

    def set_owner(self, passport_id, user_id):
        with self.db() as conn:
            conn.execute("UPDATE passports SET owner_id = ? WHERE id = ?", (user_id, passport_id))

    def assert_invariants(self):
        """Contract §2: owners are participants, personal passports have no other participants,
        every user has one personal passport, defaults point at memberships, deleted users leave no trace."""
        q = self.query
        self.assertEqual(q("SELECT id FROM passports p WHERE NOT EXISTS (SELECT 1 FROM passport_members m "
                           "WHERE m.passport_id = p.id AND m.user_id = p.owner_id AND m.role = 'participant')"),
                         [], "owner without a participant row")
        self.assertEqual(q("SELECT m.id FROM passport_members m JOIN passports p ON p.id = m.passport_id "
                           "WHERE p.is_personal = 1 AND m.role = 'participant' AND m.user_id != p.owner_id"),
                         [], "personal passport with another participant")
        self.assertEqual(q("SELECT id FROM users WHERE (SELECT COUNT(*) FROM passports p "
                           "WHERE p.owner_id = users.id AND p.is_personal = 1) != 1"),
                         [], "user without exactly one personal passport")
        self.assertEqual(q("SELECT id FROM users WHERE default_passport_id IS NOT NULL AND NOT EXISTS "
                           "(SELECT 1 FROM passport_members m WHERE m.passport_id = users.default_passport_id "
                           "AND m.user_id = users.id)"), [], "default pointing at a non-membership")
        # Contract 3 §3: every meal belongs to a participant membership; no dangling foreign keys.
        self.assertEqual(q("SELECT meals.id FROM meals JOIN passport_members m ON m.passport_id = meals.passport_id "
                           "AND m.user_id = meals.user_id WHERE m.role != 'participant'"), [], "meal of a viewer")
        self.assertEqual(q("PRAGMA foreign_key_check"), [], "foreign key violation")
        for user_id in self.deleted_ids:
            if q("SELECT 1 FROM users WHERE id = ?", (user_id,)):
                continue  # the id was reused by a new account (IdReuseTests checks that one itself)
            self.assertEqual(q("SELECT id FROM passports WHERE owner_id = ?", (user_id,)), [])
            self.assertEqual(q("SELECT id FROM passport_members WHERE user_id = ?", (user_id,)), [])
            self.assertEqual(q("SELECT id FROM sessions WHERE user_id = ?", (user_id,)), [])
            self.assertEqual(q("SELECT id FROM meals WHERE user_id = ?", (user_id,)), [])


# --- Migration and account creation ---

class MigrationServerTests(ServerTestCase):
    """The server starting on a 1b (schema version 2) database with existing sessions."""

    def prepare_data(self):
        path = f"{self.data_dir}/{db.DB_NAME}"
        db.init_schema(path, db.MIGRATIONS[:2])
        conn = db.connect(path)
        try:
            password_hash = fast_hasher().hash(PASSWORD)
            conn.execute("INSERT INTO users (id, username, display_name, password_hash, is_admin, created_at) "
                         "VALUES (1, 'admin', 'Admin', ?, 1, 100), (2, 'bob', 'Bob', ?, 0, 100)",
                         (password_hash, password_hash))
            now = self.base_time
            conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, last_used_at, expires_at) "
                         "VALUES (?, 1, ?, ?, ?)", (sha256_hex("live-token"), now - 600, now - 500, now + 1000))
        finally:
            conn.close()

    def test_upgraded_database_works(self):
        with self.db() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.MIGRATIONS[-1][0])
        resp = self.request("GET", "/api/passports", token="live-token")
        self.assertEqual(resp.status, 200)
        body = resp.json()
        self.assertEqual(len(body["passports"]), 1)
        personal = body["passports"][0]
        self.assertEqual((personal["name"], personal["is_personal"], personal["role"], personal["is_default"]),
                         ("My passport", True, "owner", True))
        self.assertEqual(body["default_passport_id"], personal["id"])
        bob = self.login_token("bob")
        resp = self.request("POST", "/api/passports", {"name": "Shared", "participants": [ref(1, "admin")]},
                            token=bob)
        self.assertEqual(resp.status, 201)


class AccountCreationTests(PassportTestCase):
    def test_setup_and_admin_create_make_personal_passport(self):
        self.assertEqual(self.query("SELECT owner_id, name, is_personal FROM passports"), [(1, "My passport", 1)])
        self.assertEqual(self.members(self.personal_id(1)), [(1, "participant")])
        resp = self.api("POST", "/api/admin/users", {"username": "bob", "display_name": "Bob", "password": TEMP})
        self.assertEqual(resp.status, 201)
        bob = resp.json()["user"]["id"]
        self.assertEqual(self.members(self.personal_id(bob)), [(bob, "participant")])

    def failing_create_personal(self):
        original = passports.create_personal

        def create_personal(conn, user_id, now):
            original(conn, user_id, now)
            raise RuntimeError("simulated failure after the passport insert")

        return mock.patch.object(passports, "create_personal", create_personal)

    def test_admin_create_failure_leaves_nothing(self):
        with self.failing_create_personal():
            resp = self.api("POST", "/api/admin/users", {"username": "bob", "display_name": "B", "password": TEMP})
        self.assertEqual(resp.status, 500)
        self.assertEqual(self.query("SELECT username FROM users"), [("admin",)])
        self.assertEqual(self.query("SELECT owner_id FROM passports"), [(1,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM passport_members"), [(1,)])


class SetupFailureTests(ServerTestCase):
    def test_setup_failure_leaves_nothing(self):
        original = passports.create_personal

        def create_personal(conn, user_id, now):
            original(conn, user_id, now)
            raise RuntimeError("simulated failure")

        with mock.patch.object(passports, "create_personal", create_personal):
            resp = self.request("POST", "/api/setup", {"username": "admin", "display_name": "A",
                                                       "password": PASSWORD})
        self.assertEqual(resp.status, 500)
        with self.db() as conn:
            for sql in ("SELECT COUNT(*) FROM users", "SELECT COUNT(*) FROM passports",
                        "SELECT COUNT(*) FROM passport_members", "SELECT COUNT(*) FROM sessions"):
                self.assertEqual(conn.execute(sql).fetchone()[0], 0, sql)
        self.setup_admin()


# --- Access rules (§3) ---

class AccessTests(PassportTestCase):
    def setUp(self):
        super().setUp()
        self.bob, self.bob_token = self.user("bob")  # participant
        self.sam, self.sam_token = self.user("sam")  # viewer
        self.eve, self.eve_token = self.user("eve")  # not a member
        self.dan, _ = self.user("dan")
        self.set_disabled(self.dan)
        self.pid = self.create_id("Trip", [ref(self.bob, "bob")], [ref(self.sam, "sam")])
        self.personal = self.personal_id(1)

    def outsider_calls(self, pid):
        """Every passport route, with bodies that would otherwise succeed or fail later in the order."""
        return [
            ("GET", f"/api/passports/{pid}", None),
            ("PATCH", f"/api/passports/{pid}", {"name": "Hijacked"}),
            ("POST", f"/api/passports/{pid}/delete", {"confirm_name": "Trip"}),
            ("POST", f"/api/passports/{pid}/delete", {"confirm_name": "My passport"}),
            ("POST", f"/api/passports/{pid}/delete", {"confirm_name": "wrong"}),
            ("POST", f"/api/passports/{pid}/delete", {"confirm_name": 5}),
            ("POST", f"/api/passports/{pid}/delete", {}),
            ("POST", f"/api/passports/{pid}/members", {**ref(self.eve, "eve"), "role": "viewer"}),
            ("POST", f"/api/passports/{pid}/members", {**ref(self.eve, "eve"), "role": "participant"}),
            ("POST", f"/api/passports/{pid}/members", {**ref(self.dan, "dan"), "role": "viewer"}),
            ("POST", f"/api/passports/{pid}/members", {**ref(self.bob, "bob"), "role": "viewer"}),
            ("POST", f"/api/passports/{pid}/members/1/remove", {}),
            ("POST", f"/api/passports/{pid}/members/{self.bob}/remove", {}),
            ("POST", f"/api/passports/{pid}/members/{self.eve}/remove", {}),
            ("POST", f"/api/passports/{pid}/members/999/remove", {}),
            ("POST", f"/api/passports/{pid}/leave", {}),
        ]

    def test_non_member_same_as_missing(self):
        before = self.query("SELECT id, name, owner_id FROM passports ORDER BY id")
        members_before = self.query("SELECT passport_id, user_id, role FROM passport_members ORDER BY id")
        missing = {}
        for method, path, body in self.outsider_calls(99999):
            resp = self.api(method, path, body, token=self.eve_token)
            missing[(method, path.replace("99999", "{pid}"), repr(body))] = (resp.status, resp.body)
        for pid in (self.pid, self.personal):
            for method, path, body in self.outsider_calls(pid):
                with self.subTest(pid=pid, method=method, path=path, body=body):
                    resp = self.api(method, path, body, token=self.eve_token)
                    self.assertEqual(resp.status, 404)
                    self.assertEqual(resp.json(), NOT_FOUND)
                    key = (method, path.replace(f"/{pid}", "/{pid}", 1), repr(body))
                    self.assertEqual((resp.status, resp.body), missing[key], "byte-identical to a missing id")
        self.assertEqual(self.query("SELECT id, name, owner_id FROM passports ORDER BY id"), before)
        self.assertEqual(self.query("SELECT passport_id, user_id, role FROM passport_members ORDER BY id"),
                         members_before)

    def test_admin_gets_no_access(self):
        bob_personal = self.personal_id(self.bob)
        for method, path, body in self.outsider_calls(bob_personal):
            with self.subTest(method=method, path=path):
                resp = self.api(method, path, body)
                self.assertEqual((resp.status, resp.json()), (404, NOT_FOUND))

    def test_members_can_read_but_not_manage(self):
        for token, role in ((self.bob_token, "participant"), (self.sam_token, "viewer")):
            with self.subTest(role=role):
                resp = self.get(self.pid, token=token)
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.json()["passport"]["role"], role)
                for method, path, body in (
                        ("PATCH", f"/api/passports/{self.pid}", {"name": "Mine now"}),
                        ("PATCH", f"/api/passports/{self.pid}", {"name": ""}),
                        ("POST", f"/api/passports/{self.pid}/delete", {"confirm_name": "Trip"}),
                        ("POST", f"/api/passports/{self.pid}/members", {**ref(self.eve, "eve"), "role": "viewer"}),
                        ("POST", f"/api/passports/{self.pid}/members/1/remove", {}),
                        ("POST", f"/api/passports/{self.pid}/members/{self.sam}/remove", {}),
                        ("POST", f"/api/passports/{self.pid}/members/999/remove", {})):
                    resp = self.api(method, path, body, token=token)
                    if body == {"name": ""}:
                        # body validation (no database) comes before the membership checks
                        self.assertEqual((resp.status, resp.error), (400, "Passport name must be 1-50 characters"))
                        continue
                    self.assertEqual((resp.status, resp.error), (403, OWNER_ONLY), path)
        self.assertEqual(self.owner_of(self.pid), 1)
        self.assertEqual(len(self.members(self.pid)), 3)

    def all_routes(self):
        return [("GET", "/api/users", None), ("GET", "/api/passports", None),
                ("POST", "/api/passports", {"name": "X"}), ("GET", "/api/account/delete-preview", None)] + [
            (m, p, b) for m, p, b in self.outsider_calls(self.pid)]

    def test_logged_out(self):
        for method, path, body in self.all_routes():
            with self.subTest(method=method, path=path):
                resp = self.api(method, path, body, token=None)
                self.assertEqual((resp.status, resp.error), (401, "Not logged in"))

    def test_pending_password(self):
        with self.db() as conn:
            conn.execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (self.bob,))
        for method, path, body in self.all_routes():
            with self.subTest(method=method, path=path):
                resp = self.api(method, path, body, token=self.bob_token)
                self.assertEqual((resp.status, resp.json()), (403, PENDING))
        resp = self.set_default(self.pid, token=self.bob_token)
        self.assertEqual((resp.status, resp.json()), (403, PENDING))


class RoutingTests(PassportTestCase):
    def test_bad_ids(self):
        for bad in ("0", "01", "-1", "+1", "1/", "abc", "12345678901", "1.0", "%31", " 1"):
            for suffix in ("", "/delete", "/members", "/members/1/remove", "/leave"):
                path = f"/api/passports/{bad}{suffix}"
                with self.subTest(path=path):
                    method = "GET" if suffix == "" else "POST"
                    resp = self.api(method, path.replace(" ", "%20"))
                    self.assertEqual((resp.status, resp.error), (404, "Not found"))
        for bad in ("0", "01", "-1", "1/", "12345678901"):
            path = f"/api/passports/1/members/{bad}/remove"
            with self.subTest(path=path):
                self.assertEqual(self.api("POST", path).error, "Not found")

    def test_unicode_digits(self):
        for raw in (b"\xd9\xa5", b"\xef\xbc\x91", b"1\xd9\xa5"):
            with self.subTest(raw=raw):
                head = (b"GET /api/passports/" + raw + b" HTTP/1.0\r\n"
                        + f"Host: {self.host}\r\nCookie: {self.cookie_name}={self.token}\r\n\r\n".encode())
                resp = self.raw(head)
                self.assertEqual((resp.status, resp.error), (404, "Not found"))
        for path in ("/api/passports/٥", "/api/passports/１/leave", "/api/passports/1/members/٥/remove"):
            with self.subTest(path=path):
                self.assertFalse(any(p.fullmatch(path) for p, _ in server.ROUTES))

    def test_largest_id(self):
        resp = self.get(9999999999)
        self.assertEqual((resp.status, resp.json()), (404, NOT_FOUND))
        resp = self.remove(self.personal_id(1), 9999999999)
        self.assertEqual((resp.status, resp.error), (404, "Not in this passport"))

    def test_wrong_method(self):
        for method, path in (("PATCH", "/api/passports"), ("POST", "/api/passports/1"),
                             ("GET", "/api/passports/1/delete"), ("GET", "/api/passports/1/leave"),
                             ("GET", "/api/passports/1/members"), ("POST", "/api/users"),
                             ("POST", "/api/account/delete-preview"), ("GET", "/api/passports/1/members/1/remove")):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.api(method, path).status, 405)

    def test_head(self):
        resp = self.api("HEAD", f"/api/passports/{self.personal_id(1)}")
        self.assertEqual((resp.status, resp.body), (200, b""))


class CrossSiteTests(PassportTestCase):
    def test_every_write_route(self):
        bob, _ = self.user("bob")
        pid = self.create_id("Trip", [ref(bob, "bob")])
        calls = [("POST", "/api/passports", {"name": "Evil"}),
                 ("PATCH", f"/api/passports/{pid}", {"name": "Evil"}),
                 ("POST", f"/api/passports/{pid}/delete", {"confirm_name": "Trip"}),
                 ("POST", f"/api/passports/{pid}/members", {**ref(bob, "bob"), "role": "viewer"}),
                 ("POST", f"/api/passports/{pid}/members/{bob}/remove", {}),
                 ("POST", f"/api/passports/{pid}/leave", {}),
                 ("PATCH", "/api/account", {"default_passport_id": pid})]
        before = (self.query("SELECT id, name FROM passports"), self.query("SELECT * FROM passport_members"),
                  self.stored_default(1))
        for method, path, body in calls:
            with self.subTest(path=path):
                resp = self.api(method, path, body, content_type="text/plain")
                self.assertEqual((resp.status, resp.error), (415, "Content-Type must be application/json"))
                resp = self.api(method, path, body, origin="http://evil.example")
                self.assertEqual((resp.status, resp.error), (403, "Cross-origin request blocked"))
        after = (self.query("SELECT id, name FROM passports"), self.query("SELECT * FROM passport_members"),
                 self.stored_default(1))
        self.assertEqual(after, before)


# --- Users list and object shapes ---

class UsersListTests(PassportTestCase):
    def test_list(self):
        zed, zed_token = self.user("zed", display_name="anna")
        self.user("bob", display_name="Bob")
        anna, _ = self.user("anna", display_name="Anna")
        dan, _ = self.user("dan", display_name="Dan")
        self.set_disabled(dan)
        resp = self.api("GET", "/api/users")
        self.assertEqual(resp.status, 200)
        users = resp.json()["users"]
        self.assertEqual([u["username"] for u in users], ["anna", "zed", "bob"])
        for user in users:
            self.assertEqual(set(user), PERSON_KEYS)
            self.assertFalse(user["inactive"])
        self.assertEqual(users[0], {"id": anna, "username": "anna", "display_name": "Anna", "inactive": False})
        # the caller is left out; the admin shows up for other users
        names = [u["username"] for u in self.api("GET", "/api/users", token=zed_token).json()["users"]]
        self.assertEqual(names, ["admin", "anna", "bob"])
        self.assertNotIn(b"scrypt", resp.body)

    def test_includes_pending_users(self):
        # "active" means not disabled; a user who must change their password can still be picked
        resp = self.api("POST", "/api/admin/users", {"username": "newbie", "display_name": "N", "password": TEMP})
        self.assertEqual(resp.status, 201)
        names = [u["username"] for u in self.api("GET", "/api/users").json()["users"]]
        self.assertEqual(names, ["newbie"])


class KeySetTests(PassportTestCase):
    def test_exact_keys(self):
        bob, bob_token = self.user("bob")
        sam, _ = self.user("sam")
        pid = self.create_id("Trip", [ref(bob, "bob")], [ref(sam, "sam")])
        detail = self.get(pid).json()
        self.assertEqual(set(detail), {"passport", "participants", "viewers"})
        self.assertEqual(set(detail["passport"]), DETAIL_PASSPORT_KEYS)
        self.assertEqual(set(detail["passport"]["owner"]), PERSON_KEYS)
        for person in detail["participants"]:
            self.assertEqual(set(person), PERSON_KEYS | {"is_owner"})
        for person in detail["viewers"]:
            self.assertEqual(set(person), PERSON_KEYS)
        listing = self.listing(token=bob_token)
        self.assertEqual(set(listing), {"passports", "default_passport_id"})
        for summary in listing["passports"]:
            self.assertEqual(set(summary), SUMMARY_KEYS)
            self.assertEqual(set(summary["owner"]), PERSON_KEYS)
        self.assertNotIn(b"scrypt", self.get(pid).body)


# --- Create ---

class CreateTests(PassportTestCase):
    def test_success(self):
        bob, bob_token = self.user("bob", display_name="Bob B")
        sam, _ = self.user("sam", display_name="Sam S")
        ann, _ = self.user("ann", display_name="Ann A")
        resp = self.create("  Date nights ", [ref(sam, "SAM"), ref(bob, "bob")], [ref(ann, "ann")])
        self.assertEqual(resp.status, 201)
        body = resp.json()
        pid = body["passport"]["id"]
        self.assertEqual(body["passport"], {
            "id": pid, "name": "Date nights", "is_personal": False, "role": "owner", "is_default": False,
            "created_at": self.base_time,
            "owner": {"id": 1, "username": "admin", "display_name": "Admin", "inactive": False}})
        self.assertEqual(body["participants"], [
            {"id": 1, "username": "admin", "display_name": "Admin", "inactive": False, "is_owner": True},
            {"id": sam, "username": "sam", "display_name": "Sam S", "inactive": False, "is_owner": False},
            {"id": bob, "username": "bob", "display_name": "Bob B", "inactive": False, "is_owner": False}])
        self.assertEqual(body["viewers"], [
            {"id": ann, "username": "ann", "display_name": "Ann A", "inactive": False}])
        self.assertEqual(self.members(pid), [(1, "participant"), (sam, "participant"), (bob, "participant"),
                                             (ann, "viewer")])
        # it shows up for the others immediately
        self.assertEqual(self.get(pid, token=bob_token).json()["passport"]["role"], "participant")

    def test_minimal_body(self):
        resp = self.api("POST", "/api/passports", {"name": "Solo"})
        self.assertEqual(resp.status, 201)
        self.assertEqual((resp.json()["participants"][0]["id"], resp.json()["viewers"]), (1, []))

    def test_duplicates_and_caller(self):
        bob, _ = self.user("bob")
        sam, _ = self.user("sam")
        for participants, viewers in (([ref(bob, "bob"), ref(bob, "bob")], []),
                                      ([], [ref(sam, "sam"), ref(sam, "SAM")]),
                                      ([ref(bob, "bob")], [ref(bob, "bob")]),
                                      ([ref(1, "admin")], []), ([], [ref(1, "admin")])):
            with self.subTest(participants=participants, viewers=viewers):
                resp = self.create("X", participants, viewers)
                self.assertEqual((resp.status, resp.error), (400, "Each person can only be added once"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM passports"), [(3,)])

    def test_bad_people(self):
        bob, _ = self.user("bob")
        dan, _ = self.user("dan")
        self.set_disabled(dan)
        bad_refs = [ref(999, "bob"), ref(dan, "dan"), ref(True, "admin"), ref("2", "bob"), ref(2.0, "bob"),
                    ref(0, "bob"), ref(-1, "bob"), ref(10000000000, "bob"), ref(bob, "sam"), ref(bob, "dan"),
                    ref(bob, None), ref(bob, 5), ref(bob, "bob\udc00"), ref(bob, "b"), {"user_id": bob},
                    {"username": "bob"}, None, "bob", bob, [bob, "bob"]]
        for item in bad_refs:
            for key in ("participants", "viewers"):
                with self.subTest(item=item, key=key):
                    resp = self.api("POST", "/api/passports", {"name": "X", key: [item]})
                    self.assertEqual((resp.status, resp.error), (400, "Choose people from the list"))
        for value in (None, {}, "bob", 5, True, {"user_id": bob, "username": "bob"}):
            for key in ("participants", "viewers"):
                with self.subTest(value=value, key=key):
                    resp = self.api("POST", "/api/passports", {"name": "X", key: value})
                    self.assertEqual((resp.status, resp.error), (400, "Choose people from the list"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM passports WHERE is_personal = 0"), [(0,)])

    def test_reused_id_with_old_username(self):
        bob, _ = self.user("bob")
        self.admin_delete(bob, "bob")
        sam, _ = self.user("sam")
        self.assertEqual(sam, bob, "SQLite reused the id")
        resp = self.create("X", [ref(bob, "bob")])
        self.assertEqual((resp.status, resp.error), (400, "Choose people from the list"))
        self.assertEqual(self.create("X", [ref(sam, "sam")]).status, 201)

    def test_name_validation(self):
        for value in ("", "   ", "a" * 51, None, 5, ["a"], True):
            with self.subTest(value=value):
                resp = self.api("POST", "/api/passports", {"name": value})
                self.assertEqual((resp.status, resp.error), (400, "Passport name must be 1-50 characters"))
        resp = self.api("POST", "/api/passports", {})
        self.assertEqual((resp.status, resp.error), (400, "Passport name must be 1-50 characters"))
        for value in ("a\u0000b", "a‮b", "a​b", "a\ud800b"):
            with self.subTest(value=value):
                resp = self.api("POST", "/api/passports", {"name": value})
                self.assertEqual((resp.status, resp.error),
                                 (400, "Passport name contains characters that aren't allowed"))
        for value in ("a" * 50, "é" * 50, "x", "<img src=x onerror=alert(1)>\"'&"):
            with self.subTest(value=value):
                self.assertEqual(self.create(value).status, 201)

    def test_name_checked_before_people(self):
        resp = self.api("POST", "/api/passports", {"name": "", "participants": None})
        self.assertEqual(resp.error, "Passport name must be 1-50 characters")

    def test_owned_limit(self):
        for i in range(48):
            self.insert_passport(1, f"P{i}")
        self.assertEqual(self.create("Fiftieth").status, 201)
        resp = self.create("Fifty-first")
        self.assertEqual((resp.status, resp.error), (409, "You can own at most 50 passports"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM passports WHERE owner_id = 1"), [(50,)])

    def test_concurrent_creates_at_49(self):
        for i in range(48):
            self.insert_passport(1, f"P{i}")
        results = race([lambda: self.create("A"), lambda: self.create("B"), lambda: self.create("C")])
        self.assertEqual(sorted(results), [201, 409, 409])
        self.assertEqual(self.query("SELECT COUNT(*) FROM passports WHERE owner_id = 1"), [(50,)])

    def test_people_limit(self):
        refs = [ref(self.insert_user(f"user{i:02}"), f"user{i:02}") for i in range(20)]
        resp = self.create("Full", refs[:10], refs[10:19])
        self.assertEqual(resp.status, 201)
        self.assertEqual(len(resp.json()["participants"]) + len(resp.json()["viewers"]), 20)
        resp = self.create("Too full", refs[:10], refs[10:20])
        self.assertEqual((resp.status, resp.error), (409, "A passport can have at most 20 people"))
        resp = self.create("Too long", refs[:20])
        self.assertEqual((resp.status, resp.error), (400, "Choose people from the list"))

    def test_server_fields_ignored(self):
        bob, _ = self.user("bob")
        resp = self.api("POST", "/api/passports", {"name": "X", "owner_id": bob, "is_personal": True, "id": 1,
                                                   "created_at": 5})
        self.assertEqual(resp.status, 201)
        p = resp.json()["passport"]
        self.assertEqual((p["owner"]["id"], p["is_personal"], p["created_at"]), (1, False, self.base_time))


def race(calls):
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


# --- Rename and delete ---

class RenameTests(PassportTestCase):
    def test_rename(self):
        bob, bob_token = self.user("bob")
        pid = self.create_id("Trip", [ref(bob, "bob")])
        resp = self.api("PATCH", f"/api/passports/{pid}", {"name": " Road trip\t", "owner_id": bob,
                                                          "is_personal": True})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["passport"]["name"], "Road trip")
        self.assertEqual(self.owner_of(pid), 1)
        self.assertEqual(self.query("SELECT is_personal FROM passports WHERE id = ?", (pid,)), [(0,)])
        self.assertEqual(self.get(pid, token=bob_token).json()["passport"]["name"], "Road trip")

    def test_personal_allowed(self):
        personal = self.personal_id(1)
        resp = self.api("PATCH", f"/api/passports/{personal}", {"name": "Mine"})
        self.assertEqual(resp.status, 200)
        self.assertEqual((resp.json()["passport"]["name"], resp.json()["passport"]["is_personal"]), ("Mine", True))

    def test_validation(self):
        pid = self.create_id("Trip")
        for body in ({}, {"display_name": "x"}, {"owner_id": 2}):
            resp = self.api("PATCH", f"/api/passports/{pid}", body)
            self.assertEqual((resp.status, resp.error), (400, "Nothing to update"))
        for value in ("", None, "a" * 51):
            resp = self.api("PATCH", f"/api/passports/{pid}", {"name": value})
            self.assertEqual((resp.status, resp.error), (400, "Passport name must be 1-50 characters"))
        resp = self.api("PATCH", f"/api/passports/{pid}", {"name": "a b"})
        self.assertEqual(resp.error, "Passport name contains characters that aren't allowed")
        self.assertEqual(self.get(pid).json()["passport"]["name"], "Trip")


class DeleteTests(PassportTestCase):
    def test_personal(self):
        personal = self.personal_id(1)
        for confirm in ("My passport", "wrong", None):
            resp = self.delete(personal, confirm)
            self.assertEqual((resp.status, resp.error), (409, "Your personal passport can't be deleted"))
        self.assertEqual(self.get(personal).status, 200)

    def test_wrong_confirmation(self):
        pid = self.create_id("Date Nights")
        for confirm in ("date nights", "Date  Nights", "Date", "", 5, None, True, ["Date Nights"], {}):
            with self.subTest(confirm=confirm):
                resp = self.delete(pid, confirm)
                self.assertEqual((resp.status, resp.error), (400, "Type the passport name to confirm"))
        resp = self.api("POST", f"/api/passports/{pid}/delete", {})
        self.assertEqual((resp.status, resp.error), (400, "Type the passport name to confirm"))
        self.assertEqual(self.get(pid).status, 200)

    def test_success(self):
        bob, bob_token = self.user("bob")
        sam, sam_token = self.user("sam")
        pid = self.create_id("Café", [ref(bob, "bob")], [ref(sam, "sam")])
        self.assertEqual(self.set_default(pid, token=bob_token).status, 200)
        self.assertEqual(self.set_default(pid, token=sam_token).status, 200)
        self.assertEqual(self.set_default(pid).status, 200)
        resp = self.delete(pid, " Café ")  # NFC + trim before the exact compare
        self.assertEqual((resp.status, resp.json()), (200, {"ok": True}))
        self.assertEqual(self.members(pid), [])
        self.assertIsNone(self.owner_of(pid))
        for user_id, token in ((1, self.token), (bob, bob_token), (sam, sam_token)):
            self.assertIsNone(self.stored_default(user_id))
            self.assertEqual(self.listing(token=token)["default_passport_id"], self.personal_id(user_id))
            self.assertEqual(self.get(pid, token=token).json(), NOT_FOUND)


# --- Members ---

class MemberTests(PassportTestCase):
    def setUp(self):
        super().setUp()
        self.bob, self.bob_token = self.user("bob")
        self.sam, self.sam_token = self.user("sam")
        self.pid = self.create_id("Trip")

    def test_add(self):
        resp = self.add(self.pid, self.bob, "BOB", "participant")
        self.assertEqual(resp.status, 200)
        self.assertEqual([p["id"] for p in resp.json()["participants"]], [1, self.bob])
        resp = self.add(self.pid, self.sam, "sam", "viewer")
        self.assertEqual([v["id"] for v in resp.json()["viewers"]], [self.sam])
        self.assertEqual(self.members(self.pid), [(1, "participant"), (self.bob, "participant"),
                                                  (self.sam, "viewer")])
        self.assertEqual(self.get(self.pid, token=self.sam_token).json()["passport"]["role"], "viewer")

    def test_personal(self):
        personal = self.personal_id(1)
        resp = self.add(personal, self.bob, "bob", "participant")
        self.assertEqual((resp.status, resp.error), (409, "A personal passport can't have participants"))
        resp = self.add(personal, self.bob, "bob", "viewer")
        self.assertEqual(resp.status, 200)
        detail = self.get(personal, token=self.bob_token).json()
        self.assertEqual((detail["passport"]["role"], detail["passport"]["is_personal"]), ("viewer", True))

    def test_already_member(self):
        self.add(self.pid, self.bob, "bob", "participant")
        self.add(self.pid, self.sam, "sam", "viewer")
        for user_id, username in ((self.bob, "bob"), (self.sam, "sam"), (1, "admin")):
            for role in ("participant", "viewer"):
                with self.subTest(username=username, role=role):
                    resp = self.add(self.pid, user_id, username, role)
                    self.assertEqual((resp.status, resp.error), (409, "Already in this passport"))
        self.assertEqual(len(self.members(self.pid)), 3)

    def test_bad_target(self):
        dan, _ = self.user("dan")
        self.set_disabled(dan)
        for user_id, username in ((dan, "dan"), (999, "bob"), (self.bob, "sam"), (self.bob, "admin")):
            with self.subTest(user_id=user_id, username=username):
                resp = self.add(self.pid, user_id, username, "viewer")
                self.assertEqual((resp.status, resp.error), (400, "Choose people from the list"))
        for body in ({"user_id": True, "username": "bob", "role": "viewer"},
                     {"user_id": str(self.bob), "username": "bob", "role": "viewer"},
                     {"user_id": float(self.bob), "username": "bob", "role": "viewer"},
                     {"username": "bob", "role": "viewer"}, {"user_id": self.bob, "role": "viewer"}):
            with self.subTest(body=body):
                resp = self.api("POST", f"/api/passports/{self.pid}/members", body)
                self.assertEqual((resp.status, resp.error), (400, "Choose people from the list"))

    def test_bad_role(self):
        for role in ("owner", "Participant", "", None, 1, True, ["viewer"]):
            with self.subTest(role=role):
                resp = self.add(self.pid, self.bob, "bob", role)
                self.assertEqual((resp.status, resp.error), (400, "Role must be participant or viewer"))
        resp = self.api("POST", f"/api/passports/{self.pid}/members", ref(self.bob, "bob"))
        self.assertEqual((resp.status, resp.error), (400, "Role must be participant or viewer"))

    def fill_to(self, count):
        """Adds people directly until the passport has `count` members."""
        people = []
        for i in range(count - len(self.members(self.pid))):
            people.append(self.insert_user(f"fill{i:02}"))
        with self.db() as conn:
            for user_id in people:
                passports.add_member(conn, self.pid, user_id, "viewer", self.base_time)

    def test_people_limit(self):
        self.fill_to(19)
        self.assertEqual(self.add(self.pid, self.bob, "bob", "participant").status, 200)
        resp = self.add(self.pid, self.sam, "sam", "viewer")
        self.assertEqual((resp.status, resp.error), (409, "A passport can have at most 20 people"))

    def test_concurrent_adds_at_19(self):
        self.fill_to(19)
        results = race([lambda: self.add(self.pid, self.bob, "bob", "participant"),
                        lambda: self.add(self.pid, self.sam, "sam", "viewer")])
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(len(self.members(self.pid)), 20)

    def test_remove(self):
        dan, dan_token = self.user("dan")
        for user_id, username, role in ((self.bob, "bob", "participant"), (self.sam, "sam", "viewer"),
                                        (dan, "dan", "participant")):
            self.add(self.pid, user_id, username, role)
        self.assertEqual(self.set_default(self.pid, token=self.bob_token).status, 200)
        self.assertEqual(self.set_default(self.pid, token=self.sam_token).status, 200)
        self.assertEqual(self.set_default(self.pid, token=dan_token).status, 200)
        self.set_disabled(dan)
        resp = self.get(self.pid)
        self.assertTrue([p for p in resp.json()["participants"] if p["id"] == dan][0]["inactive"])
        for user_id, token in ((self.bob, self.bob_token), (self.sam, self.sam_token), (dan, None)):
            with self.subTest(user_id=user_id):
                resp = self.remove(self.pid, user_id)
                self.assertEqual(resp.status, 200)
                self.assertNotIn(user_id, [p["id"] for p in resp.json()["participants"] + resp.json()["viewers"]])
                self.assertIsNone(self.stored_default(user_id))
                if token:
                    self.assertEqual(self.get(self.pid, token=token).json(), NOT_FOUND)
                    self.assertEqual(self.listing(token=token)["default_passport_id"], self.personal_id(user_id))
        self.assertEqual(self.members(self.pid), [(1, "participant")])

    def test_remove_owner_and_non_member(self):
        resp = self.remove(self.pid, 1)
        self.assertEqual((resp.status, resp.error), (409, "The owner can't be removed"))
        for user_id in (self.bob, 999):
            resp = self.remove(self.pid, user_id)
            self.assertEqual((resp.status, resp.error), (404, "Not in this passport"))
        self.assertEqual(self.members(self.pid), [(1, "participant")])

    def test_remove_keeps_other_defaults(self):
        self.add(self.pid, self.bob, "bob", "participant")
        other = self.create_id("Other", [ref(self.bob, "bob")])
        self.assertEqual(self.set_default(other, token=self.bob_token).status, 200)
        self.assertEqual(self.remove(self.pid, self.bob).status, 200)
        self.assertEqual(self.stored_default(self.bob), other)


class LeaveTests(PassportTestCase):
    def setUp(self):
        super().setUp()
        self.bob, self.bob_token = self.user("bob")
        self.sam, self.sam_token = self.user("sam")
        self.pid = self.create_id("Trip", [ref(self.bob, "bob")], [ref(self.sam, "sam")])

    def test_participant_and_viewer_leave(self):
        for user_id, token in ((self.bob, self.bob_token), (self.sam, self.sam_token)):
            with self.subTest(user_id=user_id):
                self.assertEqual(self.set_default(self.pid, token=token).status, 200)
                resp = self.leave(self.pid, token=token)
                self.assertEqual((resp.status, resp.json()), (200, {"ok": True}))
                self.assertIsNone(self.stored_default(user_id))
                self.assertEqual(self.listing(token=token)["default_passport_id"], self.personal_id(user_id))
                self.assertEqual(self.get(self.pid, token=token).json(), NOT_FOUND)
                self.assertEqual(self.leave(self.pid, token=token).json(), NOT_FOUND)
        self.assertEqual(self.members(self.pid), [(1, "participant")])

    def test_owner_cannot_leave(self):
        for pid in (self.pid, self.personal_id(1)):
            resp = self.leave(pid)
            self.assertEqual((resp.status, resp.error), (409, "The owner can't leave. Delete the passport instead."))
        self.assertEqual(len(self.members(self.pid)), 3)

    def test_viewer_leaves_someone_elses_personal_passport(self):
        bob_personal = self.personal_id(self.bob)
        self.assertEqual(self.add(bob_personal, self.sam, "sam", "viewer", token=self.bob_token).status, 200)
        self.assertEqual(self.leave(bob_personal, token=self.sam_token).status, 200)
        self.assertEqual(self.members(bob_personal), [(self.bob, "participant")])


# --- Default passport and listing ---

class DefaultPassportTests(PassportTestCase):
    def setUp(self):
        super().setUp()
        self.bob, self.bob_token = self.user("bob")
        self.owned = self.create_id("Owned")
        self.taking_part = self.create_id("Taking part", token=self.bob_token, participants=[ref(1, "admin")])
        self.viewing = self.create_id("Viewing", token=self.bob_token, viewers=[ref(1, "admin")])
        self.not_member = self.create_id("Not mine", token=self.bob_token)

    def test_set(self):
        for pid in (self.owned, self.taking_part, self.viewing):
            with self.subTest(pid=pid):
                resp = self.set_default(pid)
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.json()["default_passport_id"], pid)
                self.assertEqual(set(resp.json()), {"user", "default_passport_id"})
                listing = self.listing()
                self.assertEqual(listing["default_passport_id"], pid)
                self.assertEqual([p["id"] for p in listing["passports"] if p["is_default"]], [pid])
                self.assertTrue(self.get(pid).json()["passport"]["is_default"])
        resp = self.set_default(None)
        self.assertEqual((resp.status, resp.json()["default_passport_id"]), (200, self.personal_id(1)))
        self.assertIsNone(self.stored_default(1))

    def test_rejected(self):
        self.assertEqual(self.set_default(self.owned).status, 200)
        for value in (self.not_member, self.personal_id(self.bob), 99999, 9999999999, 0, -1, 10000000000, True,
                      False, str(self.owned), float(self.owned), [self.owned], {}, ""):
            with self.subTest(value=value):
                resp = self.set_default(value)
                self.assertEqual((resp.status, resp.error), (400, "Choose a passport you belong to"))
        self.assertEqual(self.stored_default(1), self.owned)

    def test_with_display_name(self):
        resp = self.api("PATCH", "/api/account", {"display_name": "Boss", "default_passport_id": self.viewing})
        self.assertEqual(resp.status, 200)
        self.assertEqual((resp.json()["user"]["display_name"], resp.json()["default_passport_id"]),
                         ("Boss", self.viewing))
        # display_name is validated first; nothing is stored when either fails
        resp = self.api("PATCH", "/api/account", {"display_name": "", "default_passport_id": "x"})
        self.assertEqual(resp.error, "Display name must be 1-50 characters")
        resp = self.api("PATCH", "/api/account", {"display_name": "New", "default_passport_id": self.not_member})
        self.assertEqual(resp.error, "Choose a passport you belong to")
        self.assertEqual(self.session_user(self.token)["display_name"], "Boss")
        self.assertEqual(self.stored_default(1), self.viewing)

    def test_display_name_only_keeps_default(self):
        self.set_default(self.owned)
        resp = self.api("PATCH", "/api/account", {"display_name": "X", "is_admin": False,
                                                  "must_change_password": True})
        self.assertEqual(resp.json()["default_passport_id"], self.owned)
        self.assertEqual(self.query("SELECT is_admin, must_change_password FROM users WHERE id = 1"), [(1, 0)])

    def test_fallback_on_read(self):
        # Even if a stale default were stored, reads fall back to the personal passport.
        with self.db() as conn:
            conn.execute("UPDATE users SET default_passport_id = ? WHERE id = 1", (self.not_member,))
        self.assertEqual(self.listing()["default_passport_id"], self.personal_id(1))
        with self.db() as conn:
            conn.execute("UPDATE users SET default_passport_id = NULL WHERE id = 1")

    def test_listing_order(self):
        self.create_id("apple")
        self.create_id("Banana")
        self.create_id("apple")
        listing = self.listing()
        names = [(p["name"], p["role"]) for p in listing["passports"]]
        self.assertEqual(names, [("My passport", "owner"), ("apple", "owner"), ("apple", "owner"),
                                 ("Banana", "owner"), ("Owned", "owner"), ("Taking part", "participant"),
                                 ("Viewing", "viewer")])
        ids = [p["id"] for p in listing["passports"] if p["name"] == "apple"]
        self.assertEqual(ids, sorted(ids))
        first = listing["passports"][0]
        self.assertEqual((first["is_personal"], first["is_default"]), (True, True))
        self.assertEqual(listing["passports"][-1]["owner"],
                         {"id": self.bob, "username": "bob", "display_name": "bob", "inactive": False})

    def test_viewed_personal_passport_sorts_by_name(self):
        bob_personal = self.personal_id(self.bob)
        self.add(bob_personal, 1, "admin", "viewer", token=self.bob_token)
        self.api("PATCH", f"/api/passports/{bob_personal}", {"name": "Zzz"}, token=self.bob_token)
        listing = self.listing()["passports"]
        self.assertEqual((listing[0]["id"], listing[-1]["id"]), (self.personal_id(1), bob_personal))
        self.assertEqual((listing[-1]["is_personal"], listing[-1]["role"]), (True, "viewer"))


# --- Cascades (§6) ---

class DisableHandoverTests(PassportTestCase):
    def test_handover(self):
        bob, bob_token = self.user("bob")
        dan, _ = self.user("dan")
        sam, _ = self.user("sam")
        eve, _ = self.user("eve")
        pid = self.insert_passport(bob, "Shared", participants=[dan, sam, eve])
        self.set_disabled(dan)  # the longest-standing other participant, but disabled: skipped
        viewers_only = self.insert_passport(bob, "Viewers only", viewers=[sam])
        alone = self.insert_passport(bob, "Alone")
        bob_personal = self.personal_id(bob)
        self.add(bob_personal, sam, "sam", "viewer", token=bob_token)
        resp = self.admin_action(bob, "disable")
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.owner_of(pid), sam)
        self.assertEqual(self.members(pid), [(bob, "participant"), (dan, "participant"), (sam, "participant"),
                                             (eve, "participant")])
        for unchanged in (viewers_only, alone, bob_personal):
            self.assertEqual(self.owner_of(unchanged), bob)
        # the old owner stays as an inactive participant
        sam_token = self.login_token("sam")
        detail = self.get(pid, token=sam_token).json()
        self.assertEqual(detail["passport"]["role"], "owner")
        old = [p for p in detail["participants"] if p["id"] == bob][0]
        self.assertEqual((old["inactive"], old["is_owner"]), (True, False))
        # re-enabling doesn't change ownership
        self.assertEqual(self.admin_action(bob, "enable").status, 200)
        self.assertEqual(self.owner_of(pid), sam)
        bob_token = self.login_token("bob")
        self.assertEqual(self.get(pid, token=bob_token).json()["passport"]["role"], "participant")

    def test_handover_ignores_limits(self):
        bob, _ = self.user("bob")
        sam, sam_token = self.user("sam")
        for i in range(49):
            self.insert_passport(sam, f"S{i}")
        pid = self.insert_passport(bob, "Shared", participants=[sam])
        self.assertEqual(self.admin_action(bob, "disable").status, 200)
        self.assertEqual(self.owner_of(pid), sam)
        self.assertEqual(self.query("SELECT COUNT(*) FROM passports WHERE owner_id = ?", (sam,)), [(51,)])
        resp = self.create("More", token=sam_token)
        self.assertEqual((resp.status, resp.error), (409, "You can own at most 50 passports"))


class DeleteCascadeTests(PassportTestCase):
    def build(self):
        """Bob owns: Active (handover to sam), Disabled only (to dan), Viewers only and Alone (deleted)."""
        self.bob, self.bob_token = self.user("bob")
        self.dan, _ = self.user("dan")
        self.sam, self.sam_token = self.user("sam")
        self.eve, self.eve_token = self.user("eve")
        p = {}
        p["active"] = self.insert_passport(self.bob, "Active", participants=[self.dan, self.sam], viewers=[self.eve])
        p["disabled"] = self.insert_passport(self.bob, "Disabled only", participants=[self.dan])
        p["viewers"] = self.insert_passport(self.bob, "Viewers only", viewers=[self.eve])
        p["alone"] = self.insert_passport(self.bob, "Alone")
        p["elsewhere"] = self.insert_passport(self.sam, "Sam's", participants=[self.bob])
        p["viewing"] = self.insert_passport(self.eve, "Eve's", viewers=[self.bob])
        self.set_disabled(self.dan)
        self.bob_personal = self.personal_id(self.bob)
        self.add(self.bob_personal, self.eve, "eve", "viewer", token=self.bob_token)
        self.assertEqual(self.set_default(p["viewers"], token=self.eve_token).status, 200)
        self.p = p

    def check(self):
        p = self.p
        self.assertEqual(self.owner_of(p["active"]), self.sam)
        self.assertEqual(self.owner_of(p["disabled"]), self.dan)
        self.assertIsNone(self.owner_of(p["viewers"]))
        self.assertIsNone(self.owner_of(p["alone"]))
        self.assertIsNone(self.owner_of(self.bob_personal))
        self.assertEqual(self.members(p["active"]), [(self.dan, "participant"), (self.sam, "participant"),
                                                     (self.eve, "viewer")])
        self.assertEqual(self.members(p["elsewhere"]), [(self.sam, "participant")])
        self.assertEqual(self.members(p["viewing"]), [(self.eve, "participant")])
        self.assertIsNone(self.stored_default(self.eve))
        self.assertEqual(self.listing(token=self.eve_token)["default_passport_id"], self.personal_id(self.eve))
        self.assertEqual(self.query("SELECT id FROM users WHERE id = ?", (self.bob,)), [])

    def test_admin_delete(self):
        self.build()
        self.admin_delete(self.bob, "bob")
        self.check()

    def test_self_delete(self):
        self.build()
        self.self_delete(self.bob, self.bob_token)
        self.check()

    def test_preview_matches_delete(self):
        self.build()
        resp = self.api("GET", "/api/account/delete-preview", token=self.bob_token)
        self.assertEqual(resp.status, 200)
        p = self.p
        self.assertEqual(resp.json(), {
            "handed_over": [
                {"id": p["active"], "name": "Active",
                 "new_owner": {"id": self.sam, "username": "sam", "display_name": "sam", "inactive": False}},
                {"id": p["disabled"], "name": "Disabled only",
                 "new_owner": {"id": self.dan, "username": "dan", "display_name": "dan", "inactive": True}}],
            "deleted": [{"id": p["alone"], "name": "Alone"}, {"id": p["viewers"], "name": "Viewers only"}],
            "left": 2})
        self.self_delete(self.bob, self.bob_token)
        for item in resp.json()["handed_over"]:
            self.assertEqual(self.owner_of(item["id"]), item["new_owner"]["id"])
        for item in resp.json()["deleted"]:
            self.assertIsNone(self.owner_of(item["id"]))
        self.check()

    def test_preview_of_plain_account(self):
        _, token = self.user("bob")
        resp = self.api("GET", "/api/account/delete-preview", token=token)
        self.assertEqual(resp.json(), {"handed_over": [], "deleted": [], "left": 0})

    def test_deleted_owner_of_admins_passport(self):
        # the admin's own passport where bob only took part is untouched apart from bob's row
        bob, _ = self.user("bob")
        pid = self.create_id("Admin's", [ref(bob, "bob")])
        self.admin_delete(bob, "bob")
        self.assertEqual(self.members(pid), [(1, "participant")])


class IdReuseTests(PassportTestCase):
    def test_new_account_with_reused_id_inherits_nothing(self):
        sam, _ = self.user("sam")
        bob, bob_token = self.user("bob")
        owned = self.create_id("Bob's", [ref(sam, "sam")], token=bob_token)
        member_of = self.create_id("Admin's", [ref(bob, "bob")])
        viewing = self.create_id("Admin's 2", viewers=[ref(bob, "bob")])
        self.admin_delete(bob, "bob")
        resp = self.api("POST", "/api/admin/users", {"username": "newbie", "display_name": "N", "password": TEMP})
        new_id = resp.json()["user"]["id"]
        self.assertEqual(new_id, bob, "SQLite reused the id")
        self.assertEqual(self.query("SELECT id, is_personal FROM passports WHERE owner_id = ?", (new_id,)),
                         [(self.personal_id(new_id), 1)])
        self.assertEqual(self.query("SELECT passport_id FROM passport_members WHERE user_id = ?", (new_id,)),
                         [(self.personal_id(new_id),)])
        self.assertEqual(self.owner_of(owned), sam)
        for pid in (member_of, viewing):
            self.assertEqual(self.members(pid), [(1, "participant")])
        self.assertIsNone(self.stored_default(new_id))


# --- Concurrency (§3, review M2) ---

class MidRequestTests(PassportTestCase):
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
        self.bob, self.bob_token = self.user("bob")
        self.sam, _ = self.user("sam")
        self.pid = self.create_id("Trip", [ref(self.bob, "bob")])

    def test_old_owner_refused_after_handover(self):
        for method, path, body in (
                ("PATCH", f"/api/passports/{self.pid}", {"name": "Renamed"}),
                ("POST", f"/api/passports/{self.pid}/delete", {"confirm_name": "Trip"}),
                ("POST", f"/api/passports/{self.pid}/members", {**ref(self.sam, "sam"), "role": "viewer"}),
                ("POST", f"/api/passports/{self.pid}/members/{self.bob}/remove", {})):
            with self.subTest(path=path):
                self.set_owner(self.pid, 1)
                self.mid_request = lambda: self.set_owner(self.pid, self.bob)
                resp = self.api(method, path, body)
                self.assertIsNone(self.mid_request, "the change ran")
                self.assertEqual((resp.status, resp.error), (403, OWNER_ONLY))
                self.assertEqual(self.members(self.pid), [(1, "participant"), (self.bob, "participant")])
                self.assertEqual(self.query("SELECT name FROM passports WHERE id = ?", (self.pid,)), [("Trip",)])
        self.set_owner(self.pid, 1)

    def test_leave_after_becoming_owner(self):
        self.mid_request = lambda: self.set_owner(self.pid, self.bob)
        resp = self.leave(self.pid, token=self.bob_token)
        self.assertEqual((resp.status, resp.error), (409, "The owner can't leave. Delete the passport instead."))
        self.assertEqual(self.members(self.pid), [(1, "participant"), (self.bob, "participant")])

    def test_removed_mid_request(self):
        def removed():
            with self.db() as conn:
                conn.execute("DELETE FROM passport_members WHERE passport_id = ? AND user_id = ?",
                             (self.pid, self.bob))

        self.mid_request = removed
        resp = self.leave(self.pid, token=self.bob_token)
        self.assertEqual((resp.status, resp.json()), (404, NOT_FOUND))

    def test_logged_out_mid_request(self):
        def logout():
            with self.db() as conn:
                conn.execute("DELETE FROM sessions WHERE user_id = 1")

        self.mid_request = logout
        resp = self.api("PATCH", f"/api/passports/{self.pid}", {"name": "Renamed"})
        self.assertEqual((resp.status, resp.error), (401, "Not logged in"))
        self.assertEqual(self.query("SELECT name FROM passports WHERE id = ?", (self.pid,)), [("Trip",)])
        self.token = self.login_token()

    def test_remove_and_leave_race(self):
        for _ in range(5):
            if not self.members(self.pid)[1:]:
                self.add(self.pid, self.bob, "bob", "participant")
            results = race([lambda: self.remove(self.pid, self.bob), lambda: self.leave(self.pid, token=self.bob_token)])
            self.assertIn(200, results)
            self.assertTrue(all(r in (200, 404) for r in results), results)
            self.assertEqual(self.members(self.pid), [(1, "participant")])


# --- Logging and storage ---

class LoggingTests(PassportTestCase):
    def test_no_names_logged(self):
        bob, bob_token = self.user("bob", display_name="Hidden Display")
        sam, _ = self.user("sam", display_name="Other Secret")
        pid = self.create_id("SecretTrip", [ref(bob, "bob")], [ref(sam, "sam")])
        self.api("PATCH", f"/api/passports/{pid}", {"name": "SecretRename"})
        self.remove(pid, sam)
        self.add(pid, sam, "sam", "viewer")
        self.leave(pid, token=bob_token)
        other = self.create_id("SecretKept", [ref(1, "admin")], token=bob_token)
        self.admin_action(bob, "disable")
        self.admin_action(bob, "enable")
        self.api("GET", "/api/account/delete-preview")
        self.delete(pid, "SecretRename")
        self.admin_delete(bob, "bob")
        log = self.log.getvalue()
        for text in ("Secret", "Hidden Display", "Other Secret", "My passport"):
            self.assertNotIn(text, log)
        self.assertIn("User bob deleted by admin", log)
        self.assertEqual(self.owner_of(other), 1, "handed over to the admin on disable")


class SecureDeleteTests(PassportTestCase):
    def test_request_connections(self):
        self.add_route("/api/test-secure-delete", {"GET": server.Route(
            lambda req: (200, {"value": req.db().execute("PRAGMA secure_delete").fetchone()[0]}), public=True)})
        resp = self.api("GET", "/api/test-secure-delete")
        self.assertEqual(resp.json(), {"value": 1})
