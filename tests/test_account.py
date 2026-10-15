"""Account settings: display name, password change and account deletion (contract sections 4 and 7)."""

import threading

from tests.helpers import PASSWORD, ServerTestCase, cookie_attributes

LOCKED = "Too many failed attempts. Try again in a few minutes."
NEW_PASSWORD = "a brand new passphrase"


class DisplayNameTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.setup_admin()

    def patch(self, body, token="default"):
        return self.request("PATCH", "/api/account", body, token=self.token if token == "default" else token)

    def test_update(self):
        resp = self.patch({"display_name": "  Cafe\u0301 Crew \t"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), {"user": {"id": 1, "username": "admin", "display_name": "Caf\u00e9 Crew",
                                                "is_admin": True,
                                                "must_change_password": False}})
        self.assertEqual(self.session_user(self.token)["display_name"], "Caf\u00e9 Crew")

    def test_allowed_text(self):
        for name in ("\U0001F468\u200d\U0001F469\u200d\U0001F467", "Jos\u00e9 & Mar\u00eda <3", "\u5f20\u4f1f",
                     "a" * 50, "x", "\U0001F1F5\U0001F1EA", "O'Brien \"Bob\""):
            with self.subTest(name=name):
                resp = self.patch({"display_name": name})
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.json()["user"]["display_name"], name)

    def test_length_rules(self):
        # 50 code points after NFC: 50 x (e + combining acute) normalises to 50 x U+00E9
        self.assertEqual(self.patch({"display_name": "e\u0301" * 50}).status, 200)
        for value in ("", "   ", "\u3000", "a" * 51, 5, None, ["a"], {"a": 1}, True):
            with self.subTest(value=value):
                resp = self.patch({"display_name": value})
                self.assertEqual(resp.status, 400)
                self.assertEqual(resp.error, "Display name must be 1-50 characters")

    def test_rejected_characters(self):
        for value in ("a\u0000b", "a\u0007b", "a\u007fb", "a\u202eb", "a\u200bb", "a\u2028b", "a\u2029b",
                      "a\ue000b", "a\u0378b", "a\ufeffb", "a\u00adb", "a\u2066b", "a\ud800b",
                      "\U0001f600\ud800"):
            with self.subTest(value=value):
                resp = self.patch({"display_name": value})
                self.assertEqual(resp.status, 400)
                self.assertEqual(resp.error, "Display name contains characters that aren't allowed")

    def test_lone_surrogate_from_json_escape(self):
        resp = self.request("PATCH", "/api/account", raw_body=b'{"display_name": "x\\udc00y"}', token=self.token)
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.session_user(self.token)["display_name"], "Admin")

    def test_nothing_to_update(self):
        for body in ({}, {"name": "x"}, {"username": "other"}):
            with self.subTest(body=body):
                resp = self.patch(body)
                self.assertEqual(resp.status, 400)
                self.assertEqual(resp.error, "Nothing to update")
        self.assertEqual(self.session_user(self.token)["username"], "admin")

    def test_needs_login(self):
        resp = self.patch({"display_name": "X"}, token=None)
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.error, "Not logged in")


class PasswordChangeTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.setup_admin()

    def change(self, current, new, token="default"):
        return self.request("POST", "/api/account/password", {"current_password": current, "new_password": new},
                            token=self.token if token == "default" else token)

    def test_needs_login(self):
        self.assertEqual(self.change(PASSWORD, NEW_PASSWORD, token=None).status, 401)

    def test_invalid_new_password(self):
        for new in ("short", "p" * 129, 123456789012345, None, "\udfff" + "p" * 12):
            with self.subTest(new=new):
                resp = self.change("wrong current password", new)
                self.assertEqual(resp.status, 400)
                self.assertEqual(resp.error, "Password must be 12-128 characters")
        self.assertEqual(self.app.lockout.users, {}, "400 is returned before any password check")

    def test_wrong_current_password(self):
        for current in ("wrong password!", "", None, 5, "x" * 129):
            with self.subTest(current=current):
                self.app.lockout.users.clear()
                resp = self.change(current, NEW_PASSWORD)
                self.assertEqual(resp.status, 403)
                self.assertEqual(resp.error, "Current password is wrong")
        self.assertEqual(self.login(password=PASSWORD).status, 200)

    def test_lockout(self):
        for _ in range(5):
            self.assertEqual(self.change("wrong password!", NEW_PASSWORD).status, 403)
        resp = self.change(PASSWORD, NEW_PASSWORD)
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.error, LOCKED)
        # the same username key also guards login
        self.assertEqual(self.login().status, 429)
        self.clock_offset = 901
        self.assertEqual(self.change(PASSWORD, NEW_PASSWORD).status, 200)

    def test_success(self):
        other = self.login_token()
        resp = self.change(PASSWORD, NEW_PASSWORD)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertIsNotNone(self.session_user(self.token), "this session is kept")
        self.assertIsNone(self.session_user(other), "other sessions are revoked")
        self.assertEqual(len(self.sessions_for(1)), 1)
        self.assertEqual(self.login(password=PASSWORD).status, 401)
        self.assertEqual(self.login(password=NEW_PASSWORD).status, 200)

    def test_other_users_sessions_untouched(self):
        self.insert_user("bob")
        bob = self.login_token("bob")
        self.assertEqual(self.change(PASSWORD, NEW_PASSWORD).status, 200)
        self.assertIsNotNone(self.session_user(bob))


class DeleteAccountTests(ServerTestCase):
    def delete(self, password, token):
        return self.request("POST", "/api/account/delete", {"password": password}, token=token)

    def test_needs_login(self):
        self.setup_admin()
        self.assertEqual(self.delete(PASSWORD, None).status, 401)

    def test_wrong_password(self):
        token = self.setup_admin()
        self.insert_user("other", is_admin=True)
        for password in ("wrong password!", None, ""):
            with self.subTest(password=password):
                self.app.lockout.users.clear()
                resp = self.delete(password, token)
                self.assertEqual(resp.status, 403)
                self.assertEqual(resp.error, "Password is wrong")
                self.assertIsNone(resp.headers.get_all("Set-Cookie"))
        self.assertIsNotNone(self.session_user(token))

    def test_lockout(self):
        token = self.setup_admin()
        self.insert_user("other", is_admin=True)
        for _ in range(5):
            self.assertEqual(self.delete("wrong password!", token).status, 403)
        resp = self.delete(PASSWORD, token)
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.error, LOCKED)
        self.assertIsNotNone(self.session_user(token))

    def test_only_admin(self):
        token = self.setup_admin()
        self.insert_user("bob")  # a non-admin doesn't count
        resp = self.delete(PASSWORD, token)
        self.assertEqual(resp.status, 409)
        self.assertEqual(resp.error, "You're the only admin, so this account can't be deleted")
        self.assertIsNotNone(self.session_user(token))
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)

    def test_admin_with_second_admin(self):
        token = self.setup_admin()
        other = self.login_token()
        self.insert_user("second", is_admin=True)
        resp = self.delete(PASSWORD, token)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertIn("Max-Age=0", cookie_attributes(resp.headers["Set-Cookie"]))
        self.assertIsNone(self.session_user(token))
        self.assertIsNone(self.session_user(other))
        with self.db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM users WHERE id = 1").fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = 1").fetchone()[0], 0)
        self.assertEqual(self.login().status, 401)

    def test_non_admin(self):
        self.setup_admin()
        bob_id = self.insert_user("bob")
        bob = self.login_token("bob")
        resp = self.delete(PASSWORD, bob)
        self.assertEqual(resp.status, 200)
        with self.db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM users WHERE id = ?", (bob_id,)).fetchone())
        self.assertEqual(self.sessions_for(bob_id), [])
        self.assertEqual(self.request("GET", "/api/session").json()["setup_required"], False)

    def test_concurrent_last_admins(self):
        self.insert_user("alice", is_admin=True)
        self.insert_user("carol", is_admin=True)
        tokens = [self.login_token("alice"), self.login_token("carol")]
        results = [None, None]
        barrier = threading.Barrier(2)

        def worker(i):
            barrier.wait()
            results[i] = self.delete(PASSWORD, tokens[i]).status

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(results), [200, 409])
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0], 1)

    def test_last_user_deleted_returns_to_setup(self):
        self.insert_user("alice", is_admin=False)
        token = self.login_token("alice")
        self.assertEqual(self.delete(PASSWORD, token).status, 200)
        self.assertEqual(self.request("GET", "/api/session").json(), {"setup_required": True, "user": None})


class RevokedDuringCheckTests(ServerTestCase):
    """A request whose session is revoked, or whose password changes, while its password check runs."""

    def setUp(self):
        super().setUp()
        self.token = self.setup_admin()
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

    def revoke_sessions(self):
        with self.db() as conn:
            conn.execute("DELETE FROM sessions")

    def test_password_change_after_revocation(self):
        self.after_verify = self.revoke_sessions
        resp = self.request("POST", "/api/account/password",
                            {"current_password": PASSWORD, "new_password": NEW_PASSWORD}, token=self.token)
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.error, "Not logged in")
        self.assertEqual(self.login(password=PASSWORD).status, 200)
        self.assertEqual(self.login(password=NEW_PASSWORD).status, 401)

    def test_password_changed_meanwhile(self):
        other_hash = self.app.hasher.hash("someone else's new password")

        def change():
            with self.db() as conn:
                conn.execute("UPDATE users SET password_hash = ?", (other_hash,))

        self.after_verify = change
        resp = self.request("POST", "/api/account/password",
                            {"current_password": PASSWORD, "new_password": NEW_PASSWORD}, token=self.token)
        self.assertEqual(resp.status, 403)
        self.assertEqual(resp.error, "Current password is wrong")
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT password_hash FROM users").fetchone()[0], other_hash)
        self.assertEqual(len(self.sessions_for(1)), 1, "other sessions are not revoked on failure")

    def test_delete_after_revocation(self):
        self.insert_user("second", is_admin=True)
        self.after_verify = self.revoke_sessions
        resp = self.request("POST", "/api/account/delete", {"password": PASSWORD}, token=self.token)
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.error, "Not logged in")
        with self.db() as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM users WHERE id = 1").fetchone())
