"""First-visit setup, login, lockout and client IPs (contract sections 2, 6 and 7)."""

import ipaddress
import json
import threading
import unittest

from tests.helpers import (PASSWORD, CountingSemaphore, ServerTestCase, auth, cookie_attributes,
                           cookie_value, sha256_hex)

LOCKED = "Too many failed attempts. Try again in a few minutes."
WRONG = "Wrong username or password"


def run_concurrently(count, func):
    results = [None] * count
    barrier = threading.Barrier(count)

    def worker(i):
        barrier.wait()
        try:
            results[i] = func(i)
        except Exception as e:  # recorded so a failure shows what went wrong
            results[i] = repr(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


class SetupTests(ServerTestCase):
    def setup_body(self, **overrides):
        body = {"username": "admin", "display_name": "Admin", "password": PASSWORD}
        body.update(overrides)
        return body

    def test_session_before_setup(self):
        resp = self.request("GET", "/api/session")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), {"setup_required": True, "user": None})

    def test_setup_success(self):
        resp = self.request("POST", "/api/setup", self.setup_body(username="David", display_name="  David  "))
        self.assertEqual(resp.status, 201)
        self.assertEqual(resp.json(), {"user": {"id": 1, "username": "David", "display_name": "David",
                                                "is_admin": True,
                                                "must_change_password": False, "avatar_seed": self.seed_of(1), "photo": None}})
        cookies = resp.set_cookies(self.cookie_name)
        self.assertEqual(len(cookies), 1)
        token = cookie_value(cookies[0])
        self.assertEqual(self.session_user(token)["username"], "David")
        self.assertEqual(self.request("GET", "/api/session", token=token).json()["setup_required"], False)
        self.assertEqual(self.request("GET", "/api/session").json(), {"setup_required": False, "user": None})
        self.assertIn("Setup completed by David", self.log.getvalue())
        with self.db() as conn:
            row = conn.execute("SELECT * FROM users").fetchone()
            self.assertEqual(row["is_admin"], 1)
            self.assertTrue(row["password_hash"].startswith("scrypt$"))
            self.assertNotIn(PASSWORD, row["password_hash"])
            session = conn.execute("SELECT * FROM sessions").fetchone()
            self.assertEqual(session["token_hash"], sha256_hex(token))

    def test_boundaries_accepted(self):
        resp = self.request("POST", "/api/setup", self.setup_body(
            username="a" * 32, display_name="d" * 50, password="p" * 128))
        self.assertEqual(resp.status, 201)

    def test_minimum_boundaries_accepted(self):
        resp = self.request("POST", "/api/setup", self.setup_body(username="a._", display_name="d",
                                                                  password=" " * 12))
        self.assertEqual(resp.status, 201)
        self.assertEqual(self.login("A._", " " * 12).status, 200)

    def test_validation_errors(self):
        username_msg = "Username must be 3-32 characters: letters, digits, . _ -"
        length_msg = "Display name must be 1-50 characters"
        chars_msg = "Display name contains characters that aren't allowed"
        password_msg = "Password must be 12-128 characters"
        cases = [
            ({"username": "ab"}, username_msg),
            ({"username": "a" * 33}, username_msg),
            ({"username": "a b"}, username_msg),
            ({"username": " admin"}, username_msg),
            ({"username": "d\u00e4vid"}, username_msg),
            ({"username": "admin\n"}, username_msg),
            ({"username": "ad@min"}, username_msg),
            ({"username": 123}, username_msg),
            ({"username": None}, username_msg),
            ({"username": ["admin"]}, username_msg),
            ({"display_name": ""}, length_msg),
            ({"display_name": "   "}, length_msg),
            ({"display_name": "d" * 51}, length_msg),
            ({"display_name": 5}, length_msg),
            ({"display_name": None}, length_msg),
            ({"display_name": "a\u0007b"}, chars_msg),
            ({"display_name": "a\u202eb"}, chars_msg),
            ({"password": "p" * 11}, password_msg),
            ({"password": "p" * 129}, password_msg),
            ({"password": 123456789012}, password_msg),
            ({"password": None}, password_msg),
            ({"password": "\ud800" + "p" * 12}, password_msg),
            # validation order: username, display_name, password
            ({"username": "x", "password": "short"}, username_msg),
            ({"display_name": "", "password": "short"}, length_msg),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                resp = self.request("POST", "/api/setup", self.setup_body(**overrides))
                self.assertEqual(resp.status, 400)
                self.assertEqual(resp.error, message)
        missing = self.setup_body()
        del missing["password"]
        self.assertEqual(self.request("POST", "/api/setup", missing).error, password_msg)
        self.assertTrue(self.request("GET", "/api/session").json()["setup_required"])

    def test_second_setup_conflict(self):
        self.setup_admin()
        resp = self.request("POST", "/api/setup", self.setup_body(username="other"))
        self.assertEqual(resp.status, 409)
        self.assertEqual(resp.error, "Setup is already complete")
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_setup_after_completion_does_not_hash(self):
        self.setup_admin()
        counter = CountingSemaphore(self.app.hasher.semaphore)
        self.app.hasher.semaphore = counter
        resp = self.request("POST", "/api/setup", self.setup_body(username="other"))
        self.assertEqual(resp.status, 409)
        self.assertEqual(counter.acquired, 0)

    def test_validation_before_conflict(self):
        self.setup_admin()
        self.assertEqual(self.request("POST", "/api/setup", self.setup_body(username="x")).status, 400)

    def test_concurrent_setups(self):
        results = run_concurrently(10, lambda i: self.request(
            "POST", "/api/setup", self.setup_body(username=f"user{i}")).status)
        self.assertEqual(sorted(results), [201] + [409] * 9)
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)

    def test_setup_with_stale_cookie_sets_one_cookie(self):
        resp = self.request("POST", "/api/setup", self.setup_body(), token="stale-token")
        self.assertEqual(resp.status, 201)
        cookies = resp.headers.get_all("Set-Cookie")
        self.assertEqual(len(cookies), 1)
        self.assertIn("Max-Age=2592000", cookies[0])
        self.assertEqual(self.session_user(cookie_value(cookies[0]))["username"], "admin")

    def test_setup_busy(self):
        self.app.hasher.timeout = 0.2
        sem = self.app.hasher.semaphore
        sem.acquire()
        sem.acquire()
        try:
            resp = self.request("POST", "/api/setup", self.setup_body())
        finally:
            sem.release()
            sem.release()
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.error, "Server busy, try again")
        self.assertTrue(self.request("GET", "/api/session").json()["setup_required"])


class LoginTests(ServerTestCase):
    def test_setup_required(self):
        for _ in range(7):
            resp = self.login()
            self.assertEqual(resp.status, 409)
            self.assertEqual(resp.error, "Setup is required")
        self.assertEqual(self.app.lockout.users, {})
        self.assertEqual(self.app.lockout.ips, {})

    def test_success(self):
        self.setup_admin(display_name="The Admin")
        resp = self.login()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), {"user": {"id": 1, "username": "admin", "display_name": "The Admin",
                                                "is_admin": True,
                                                "must_change_password": False, "avatar_seed": self.seed_of(1), "photo": None}})
        token = cookie_value(resp.set_cookies(self.cookie_name)[0])
        self.assertEqual(self.session_user(token)["id"], 1)

    def test_case_insensitive_username(self):
        self.setup_admin(username="David")
        for name in ("david", "DAVID", "dAvId"):
            resp = self.login(name)
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.json()["user"]["username"], "David")

    def test_wrong_password_and_unknown_user(self):
        self.setup_admin()
        for username, password in (("admin", "wrong password!"), ("nobody", PASSWORD),
                                   ("admin", PASSWORD.upper()), ("admin", ""), ("admin", 12345),
                                   ("admin", "x" * 129), ("admin", None)):
            with self.subTest(username=username, password=password):
                self.app.lockout.users.clear()
                self.app.lockout.ips.clear()
                resp = self.login(username, password)
                self.assertEqual(resp.status, 401)
                self.assertEqual(resp.error, WRONG)
                self.assertEqual(resp.set_cookies(self.cookie_name), [])

    def test_lone_surrogate_password(self):
        self.setup_admin()
        raw = b'{"username": "admin", "password": "\\ud800abcdefghijklmnop"}'
        resp = self.request("POST", "/api/login", raw_body=raw)
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.error, WRONG)

    def test_malformed_username_counts_against_ip_only(self):
        self.setup_admin()
        for username in (123, "a", None, "bad name", "x" * 33):
            self.assertEqual(self.login(username, "whatever password").status, 401)
        self.assertEqual(self.app.lockout.users, {})
        self.assertEqual(len(self.app.lockout.ips["127.0.0.1"].failures), 5)
        # the IP is now locked, even for a correct login
        self.assertEqual(self.login().status, 429)

    def test_missing_fields(self):
        self.setup_admin()
        resp = self.request("POST", "/api/login", {})
        self.assertEqual(resp.status, 401)
        self.assertEqual(self.app.lockout.users, {})

    def test_concurrent_wrong_logins(self):
        self.setup_admin()
        results = run_concurrently(20, lambda i: self.login("admin", "wrong password!").status)
        self.assertEqual(results.count(401), 5, results)
        self.assertEqual(results.count(429), 15, results)

    def test_pre_existing_cookie_not_reused(self):
        token = self.setup_admin()
        resp = self.login(token=token)
        self.assertEqual(resp.status, 200)
        cookies = resp.headers.get_all("Set-Cookie")
        self.assertEqual(len(cookies), 1)
        new_token = cookie_value(cookies[0])
        self.assertNotEqual(new_token, token)
        self.assertIsNotNone(self.session_user(token), "old session is not deleted")
        self.assertIsNotNone(self.session_user(new_token))
        self.assertEqual(len(self.sessions_for(1)), 2)

    def test_login_with_unknown_cookie_sets_one_cookie(self):
        self.setup_admin()
        resp = self.login(token="not-a-real-token")
        cookies = resp.headers.get_all("Set-Cookie")
        self.assertEqual(len(cookies), 1)
        self.assertIn("Max-Age=2592000", cookie_attributes(cookies[0]))
        self.assertIsNotNone(self.session_user(cookie_value(cookies[0])))

    def test_failed_login_with_unknown_cookie_clears_it(self):
        self.setup_admin()
        resp = self.login(password="wrong password!", token="not-a-real-token")
        self.assertEqual(resp.status, 401)
        cookies = resp.headers.get_all("Set-Cookie")
        self.assertEqual(len(cookies), 1)
        self.assertIn("Max-Age=0", cookie_attributes(cookies[0]))

    def test_expired_sessions_purged_on_login(self):
        self.setup_admin()
        with self.db() as conn:
            conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, last_used_at, expires_at) "
                         "VALUES ('old', 1, 0, 0, 1)")
        self.assertEqual(len(self.sessions_for(1)), 2)
        self.login()
        hashes = [row["token_hash"] for row in self.sessions_for(1)]
        self.assertNotIn("old", hashes)
        self.assertEqual(len(hashes), 2)

    def test_busy_records_no_failure(self):
        self.setup_admin()
        self.app.hasher.timeout = 0.2
        sem = self.app.hasher.semaphore
        sem.acquire()
        sem.acquire()
        try:
            resp = self.login()
        finally:
            sem.release()
            sem.release()
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.error, "Server busy, try again")
        for entry in (self.app.lockout.users["admin"], self.app.lockout.ips["127.0.0.1"]):
            self.assertEqual((entry.failures, entry.in_flight), ([], 0))


class ProxiedLoginTests(ServerTestCase):
    """The peer (127.0.0.1) is a trusted proxy, so each X-Forwarded-For is its own client."""

    env = {"WORLD_EATS_TRUSTED_PROXIES": "127.0.0.1"}

    def login_from(self, ip, username="admin", password=PASSWORD):
        return self.login(username, password, headers={"X-Forwarded-For": ip})

    def test_username_lockout(self):
        self.setup_admin()
        for i in range(5):
            self.assertEqual(self.login_from(f"203.0.113.{i}", password="wrong password!").status, 401)
        resp = self.login_from("203.0.113.50")
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.error, LOCKED)
        self.assertEqual(self.login_from("203.0.113.51", username="ADMIN").status, 429)

    def test_unknown_username_lockout_identical(self):
        self.setup_admin()
        for i in range(5):
            self.login_from(f"203.0.113.{i}", username="ghost", password="wrong password!")
            self.login_from(f"198.51.100.{i}", username="admin", password="wrong password!")
        ghost = self.login_from("203.0.113.60", username="ghost")
        admin = self.login_from("203.0.113.61", username="admin")
        self.assertEqual((ghost.status, ghost.body), (admin.status, admin.body))
        self.assertEqual(ghost.status, 429)

    def test_ip_lockout_across_usernames(self):
        self.setup_admin()
        self.insert_user("bob")
        for i in range(5):
            self.assertEqual(self.login_from("203.0.113.9", username=f"user{i}",
                                             password="wrong password!").status, 401)
        self.assertEqual(self.login_from("203.0.113.9", username="bob").status, 429)
        self.assertEqual(self.login_from("203.0.113.10", username="bob").status, 200)

    def test_ipv6_clients_share_a_64(self):
        self.setup_admin()
        for i in range(5):
            self.login_from(f"2001:db8:1:2::{i + 1}", username=f"user{i}", password="wrong password!")
        self.assertEqual(self.login_from("2001:db8:1:2:ffff::1", username="other").status, 429)
        self.assertEqual(self.login_from("2001:db8:1:3::1", username="other").status, 401)

    def test_lockout_expires_after_window(self):
        self.setup_admin()
        for i in range(5):
            self.login_from(f"203.0.113.{i}", password="wrong password!")
        # The fake clock is fixed: all five failures are at base_time exactly.
        self.assertEqual(self.login_from("203.0.113.99").status, 429)
        self.clock_offset = 899  # 899 s after the failures: still inside the 15-minute window
        self.assertEqual(self.login_from("203.0.113.99").status, 429)
        self.clock_offset = 900  # exactly 15 minutes: the failures have expired
        self.assertEqual(self.login_from("203.0.113.99").status, 200)

    def test_success_clears_username_failures(self):
        self.setup_admin()
        for i in range(4):
            self.login_from(f"203.0.113.{i}", password="wrong password!")
        self.assertEqual(self.login_from("203.0.113.20").status, 200)
        for i in range(4):
            self.assertEqual(self.login_from(f"198.51.100.{i}", password="wrong password!").status, 401)
        self.assertEqual(self.login_from("198.51.100.30", password="wrong password!").status, 401)
        self.assertEqual(self.login_from("198.51.100.31").status, 429)

    def test_success_does_not_clear_ip_failures(self):
        self.setup_admin()
        for i in range(4):
            self.login_from("203.0.113.1", username=f"user{i}", password="wrong password!")
        self.assertEqual(self.login_from("203.0.113.1").status, 200)
        self.assertEqual(len(self.app.lockout.ips["203.0.113.1"].failures), 4)

    def test_rightmost_untrusted_entry_used(self):
        self.setup_admin()
        self.login(password="wrong password!", headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.7, 127.0.0.1"})
        self.assertEqual(list(self.app.lockout.ips), ["203.0.113.7"])

    def test_multiple_xff_headers_joined(self):
        self.setup_admin()
        body = json.dumps({"username": "admin", "password": "wrongpass"}).encode()
        resp = self.raw_request("POST", "/api/login", headers=self.post_headers(len(body)) + [
            ("X-Forwarded-For", "1.1.1.1"), ("X-Forwarded-For", "203.0.113.8, 127.0.0.1")], body=body)
        self.assertEqual(resp.status, 401)
        self.assertEqual(list(self.app.lockout.ips), ["203.0.113.8"])

    def test_invalid_entry_uses_peer(self):
        self.setup_admin()
        for xff in ("garbage", "203.0.113.5, not-an-ip", "203.0.113.5,", "127.0.0.1"):
            with self.subTest(xff=xff):
                self.app.lockout.ips.clear()
                self.login(password="wrong password!", headers={"X-Forwarded-For": xff})
                self.assertEqual(list(self.app.lockout.ips), ["127.0.0.1"])

    def test_no_warning_from_trusted_peer(self):
        self.setup_admin()
        self.login_from("203.0.113.1")
        self.assertNotIn("X-Forwarded-For received", self.log.getvalue())


class UntrustedProxyTests(ServerTestCase):
    def test_xff_ignored_from_untrusted_peer(self):
        self.setup_admin()
        for i in range(5):
            self.login(f"user{i}", "wrong password!", headers={"X-Forwarded-For": f"203.0.113.{i}"})
        self.assertEqual(list(self.app.lockout.ips), ["127.0.0.1"])
        resp = self.login(headers={"X-Forwarded-For": "198.51.100.1"})
        self.assertEqual(resp.status, 429)
        log = self.log.getvalue()
        self.assertEqual(log.count("X-Forwarded-For received from 127.0.0.1 but WORLD_EATS_TRUSTED_PROXIES "
                                   "is not set; all clients share one lockout bucket"), 1)

    def test_other_forwarding_headers_ignored(self):
        resp = self.raw_request("GET", "/api/session", host="evil.test", headers=[
            ("X-Forwarded-Host", "127.0.0.1"), ("Forwarded", "host=127.0.0.1")])
        self.assertEqual(resp.status, 421)
        resp = self.raw_request("POST", "/api/logout", headers=[
            ("Origin", "https://evil.test"), ("X-Forwarded-Host", "evil.test"), ("X-Forwarded-Proto", "https"),
            ("Content-Type", "application/json"), ("Content-Length", "2")], body=b"{}")
        self.assertEqual(resp.status, 403)


class ClientIpUnitTests(unittest.TestCase):
    trusted = frozenset({ipaddress.ip_address("10.0.0.1"), ipaddress.ip_address("10.0.0.2")})

    def ip(self, peer, *xff):
        return str(auth.client_ip(peer, list(xff), self.trusted))

    def test_untrusted_peer(self):
        self.assertEqual(self.ip("192.0.2.1", "203.0.113.1"), "192.0.2.1")

    def test_trusted_peer(self):
        self.assertEqual(self.ip("10.0.0.1", "203.0.113.1"), "203.0.113.1")
        self.assertEqual(self.ip("10.0.0.1", "6.6.6.6, 203.0.113.1, 10.0.0.2"), "203.0.113.1")
        self.assertEqual(self.ip("10.0.0.1", "6.6.6.6", "203.0.113.1 , 10.0.0.2"), "203.0.113.1")
        self.assertEqual(self.ip("10.0.0.1", "10.0.0.2"), "10.0.0.1")
        self.assertEqual(self.ip("10.0.0.1"), "10.0.0.1")
        self.assertEqual(self.ip("10.0.0.1", "bogus, 10.0.0.2"), "10.0.0.1")
        self.assertEqual(self.ip("10.0.0.1", "::ffff:203.0.113.1"), "203.0.113.1")
        self.assertEqual(self.ip("::ffff:10.0.0.1", "203.0.113.1"), "203.0.113.1")

    def test_ip_key(self):
        key = auth.ip_key
        self.assertEqual(key(ipaddress.ip_address("203.0.113.1")), "203.0.113.1")
        self.assertEqual(key(ipaddress.ip_address("::ffff:203.0.113.1")), "203.0.113.1")
        self.assertEqual(key(ipaddress.ip_address("2001:db8:1:2:3:4:5:6")), "2001:db8:1:2::/64")


class LockoutUnitTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.lockout = auth.Lockout(lambda: self.now, max_keys=3)

    def fail(self, user, ip):
        self.assertTrue(self.lockout.reserve(user, ip))
        self.lockout.finish(user, ip, auth.FAILED)

    def test_in_flight_counts(self):
        lockout = auth.Lockout(lambda: self.now)
        for _ in range(5):
            self.assertTrue(lockout.reserve("u", "ip"))
        self.assertFalse(lockout.reserve("u", "ip2"))
        self.assertFalse(lockout.reserve("u2", "ip"))
        lockout.finish("u", "ip", auth.NO_RESULT)
        self.assertTrue(lockout.reserve("u", "ip"))

    def test_keeps_at_most_five_timestamps(self):
        lockout = auth.Lockout(lambda: self.now)
        for i in range(5):
            self.assertTrue(lockout.reserve("u", f"ip{i}"))
            lockout.finish("u", f"ip{i}", auth.FAILED)
        lockout.finish("u", None, auth.FAILED)  # a stray extra failure is still capped
        self.assertEqual(len(lockout.users["u"].failures), 5)
        self.assertEqual(lockout.users["u"].in_flight, 0)

    def test_eviction_skips_in_flight_keys(self):
        self.assertTrue(self.lockout.reserve("busy", None))
        self.fail("a", None)
        self.now += 1
        self.fail("b", None)
        self.now += 1
        self.fail("c", None)  # table full: evicts "a" (oldest idle), never "busy"
        self.assertEqual(set(self.lockout.users), {"busy", "b", "c"})
        self.lockout.finish("busy", None, auth.NO_RESULT)

    def test_prunes_expired_keys(self):
        self.fail("a", "ip")
        self.now += 901
        self.assertTrue(self.lockout.reserve("b", None))
        self.assertNotIn("a", self.lockout.users)
        self.lockout.finish("b", None, auth.SUCCEEDED)

    def test_finish_on_missing_key_is_noop(self):
        self.lockout.finish("ghost", "ghost-ip", auth.FAILED)
        self.assertEqual(self.lockout.users, {})


if __name__ == "__main__":
    unittest.main()
