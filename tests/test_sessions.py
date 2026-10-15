"""Session cookies, expiry, renewal, logout and log out everywhere (contract sections 5 and 7)."""

from tests.helpers import ServerTestCase, cookie_attributes, cookie_value, sha256_hex


class SessionCookieTests(ServerTestCase):
    def test_cookie_attributes(self):
        resp = self.request("POST", "/api/setup", {
            "username": "admin", "display_name": "Admin", "password": "correct horse battery"})
        cookies = resp.headers.get_all("Set-Cookie")
        self.assertEqual(len(cookies), 1)
        name_value = cookies[0].split(";", 1)[0]
        self.assertTrue(name_value.startswith("we_session="))
        self.assertEqual(len(cookie_value(cookies[0])), 43)
        self.assertEqual(sorted(cookie_attributes(cookies[0])),
                         sorted(["HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=2592000"]))
        self.assertNotIn("Domain", cookies[0])
        self.assertNotIn("Secure", cookies[0])

    def test_only_hash_stored(self):
        token = self.setup_admin()
        token2 = self.login_token()
        with self.db() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM sessions")]
        self.assertEqual({r["token_hash"] for r in rows}, {sha256_hex(token), sha256_hex(token2)})
        dump = repr(rows)
        self.assertNotIn(token, dump)
        self.assertNotIn(token2, dump)
        with open(self.app.db_path, "rb") as f:
            self.assertNotIn(token.encode(), f.read())

    def test_timestamps(self):
        self.setup_admin()
        with self.db() as conn:
            row = conn.execute("SELECT * FROM sessions").fetchone()
        self.assertIsInstance(row["created_at"], int)
        self.assertEqual(row["last_used_at"], row["created_at"])
        self.assertEqual(row["expires_at"], row["created_at"] + 2592000)

    def test_expired_session(self):
        token = self.setup_admin()
        self.clock_offset = 2592000
        resp = self.request("GET", "/api/session", token=token)
        self.assertIsNone(resp.json()["user"])
        cookies = resp.headers.get_all("Set-Cookie")
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0].split(";", 1)[0], "we_session=")
        self.assertIn("Max-Age=0", cookie_attributes(cookies[0]))
        self.assertIn("HttpOnly", cookie_attributes(cookies[0]))
        self.clock_offset = 2592000 - 5
        self.assertIsNotNone(self.session_user(token))

    def test_renewal_only_after_an_hour(self):
        token = self.setup_admin()
        with self.db() as conn:
            before = dict(conn.execute("SELECT * FROM sessions").fetchone())
        self.clock_offset = 3500
        resp = self.request("GET", "/api/session", token=token)
        self.assertIsNotNone(resp.json()["user"])
        self.assertEqual(resp.headers.get_all("Set-Cookie"), None)
        with self.db() as conn:
            self.assertEqual(dict(conn.execute("SELECT * FROM sessions").fetchone()), before)
        self.clock_offset = 3601
        resp = self.request("GET", "/api/session", token=token)
        cookies = resp.headers.get_all("Set-Cookie")
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookie_value(cookies[0]), token)
        self.assertIn("Max-Age=2592000", cookie_attributes(cookies[0]))
        with self.db() as conn:
            after = conn.execute("SELECT * FROM sessions").fetchone()
        self.assertGreaterEqual(after["last_used_at"], before["last_used_at"] + 3600)
        self.assertEqual(after["expires_at"], after["last_used_at"] + 2592000)
        # renewed: still valid long after the original expiry
        self.clock_offset = 2592000 + 3000
        self.assertIsNotNone(self.session_user(token))

    def test_overlong_cookie_ignored(self):
        self.setup_admin()
        resp = self.request("GET", "/api/session", token="a" * 101)
        self.assertIsNone(resp.json()["user"])
        self.assertIn("Max-Age=0", resp.headers["Set-Cookie"])

    def test_unknown_cookie_cleared(self):
        self.setup_admin()
        for value in ("unknown", "", "a" * 100):
            with self.subTest(value=value):
                resp = self.request("GET", "/api/session", token=value)
                self.assertIsNone(resp.json()["user"])
                self.assertEqual(len(resp.headers.get_all("Set-Cookie")), 1)
                self.assertIn("Max-Age=0", resp.headers["Set-Cookie"])

    def test_duplicate_cookie_name(self):
        token = self.setup_admin()
        resp = self.request("GET", "/api/session", cookie=f"we_session={token}; we_session={token}")
        self.assertIsNone(resp.json()["user"])
        self.assertIsNone(resp.headers.get_all("Set-Cookie"))

    def test_duplicate_cookie_header(self):
        token = self.setup_admin()
        resp = self.raw_request("GET", "/api/session", headers=[
            ("Cookie", f"we_session={token}"), ("Cookie", "other=1")])
        self.assertEqual(resp.status, 400)

    def test_other_cookies_ignored(self):
        token = self.setup_admin()
        user = self.request("GET", "/api/session", cookie=f"a=b; we_session={token} ;c=d=e; flag").json()["user"]
        self.assertEqual(user["username"], "admin")
        resp = self.request("GET", "/api/session", cookie="other=1")
        self.assertIsNone(resp.json()["user"])
        self.assertIsNone(resp.headers.get_all("Set-Cookie"))

    def test_deleted_user_session(self):
        token = self.setup_admin()
        with self.db() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DELETE FROM users")
        resp = self.request("GET", "/api/session", token=token)
        self.assertEqual(resp.json(), {"setup_required": True, "user": None})
        self.assertIn("Max-Age=0", resp.headers["Set-Cookie"])

    def test_invalid_cookie_on_authenticated_endpoint(self):
        self.setup_admin()
        resp = self.request("PATCH", "/api/account", {"display_name": "X"}, token="bogus")
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.error, "Not logged in")
        self.assertIn("Max-Age=0", resp.headers["Set-Cookie"])


class SecureCookieTests(ServerTestCase):
    env = {"WORLD_EATS_SECURE_COOKIES": "true"}

    def test_secure_cookie(self):
        token = self.setup_admin()
        resp = self.login()
        cookie = resp.headers["Set-Cookie"]
        self.assertTrue(cookie.startswith("__Host-we_session="))
        self.assertIn("Secure", cookie_attributes(cookie))
        self.assertIn("Path=/", cookie_attributes(cookie))
        self.assertNotIn("Domain", cookie)
        self.assertEqual(self.session_user(token)["username"], "admin")
        # the plain name means nothing when secure cookies are on
        resp = self.request("GET", "/api/session", cookie=f"we_session={token}")
        self.assertIsNone(resp.json()["user"])
        clear = self.request("POST", "/api/logout", {}, token=token).headers["Set-Cookie"]
        self.assertTrue(clear.startswith("__Host-we_session=;"))
        self.assertIn("Secure", cookie_attributes(clear))
        self.assertIn("Max-Age=0", cookie_attributes(clear))


class LogoutTests(ServerTestCase):
    def test_logout_ends_this_session_only(self):
        token = self.setup_admin()
        other = self.login_token()
        resp = self.request("POST", "/api/logout", {}, token=token)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), {"ok": True})
        cookies = resp.headers.get_all("Set-Cookie")
        self.assertEqual(len(cookies), 1)
        self.assertIn("Max-Age=0", cookie_attributes(cookies[0]))
        self.assertIsNone(self.session_user(token))
        self.assertIsNotNone(self.session_user(other))
        self.assertEqual(len(self.sessions_for(1)), 1)

    def test_logout_without_session(self):
        for kwargs in ({}, {"token": "bogus"}):
            resp = self.request("POST", "/api/logout", {}, **kwargs)
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.json(), {"ok": True})
            self.assertEqual(len(resp.headers.get_all("Set-Cookie")), 1)
            self.assertIn("Max-Age=0", resp.headers["Set-Cookie"])

    def test_logout_everywhere(self):
        token = self.setup_admin()
        other = self.login_token()
        resp = self.request("POST", "/api/account/logout-everywhere", {}, token=token)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), {"ok": True})
        cookies = resp.headers.get_all("Set-Cookie")
        self.assertEqual(len(cookies), 1)
        self.assertIn("Max-Age=0", cookie_attributes(cookies[0]))
        self.assertIsNone(self.session_user(token))
        self.assertIsNone(self.session_user(other))
        self.assertEqual(self.sessions_for(1), [])

    def test_logout_everywhere_needs_login(self):
        self.setup_admin()
        resp = self.request("POST", "/api/account/logout-everywhere", {})
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.error, "Not logged in")
        self.assertEqual(len(self.sessions_for(1)), 1)

    def test_logout_everywhere_leaves_other_users(self):
        token = self.setup_admin()
        self.insert_user("bob")
        bob = self.login_token("bob")
        self.request("POST", "/api/account/logout-everywhere", {}, token=token)
        self.assertIsNotNone(self.session_user(bob))
