"""Change 11d: profile photo endpoints (contract 11d §3, §8, §9; security notes §5 and §6).

The JPEG rules themselves are in tests/test_photos.py.
"""

import re
import threading
import time
import unittest
from unittest import mock

from tests import jpeg
from tests.helpers import PASSWORD, ServerTestCase, auth, cookie_attributes, cookie_value, server, sha256_hex
from tests.test_meals import DATE, MealTestCase
from tests.test_passports import PENDING

import photos  # noqa: E402  (app/ is on sys.path via tests.helpers)

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
CLIENT_TOKEN = "A" * 43  # a well-formed token the server must never store
PHOTO = jpeg.CHROME_PHOTO
DETAILED = jpeg.CHROME_DETAILED
PHOTO_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; sandbox",
    "Content-Type": "image/jpeg",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "private, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}
GLOBAL_CSP = dict(server.SECURITY_HEADERS)["Content-Security-Policy"]


class PhotoMixin:
    def upload(self, data=PHOTO, token="default", content_type="image/jpeg", path="/api/account/photo", **kwargs):
        token = self.token if token == "default" else token
        return self.request("POST", path, raw_body=data, token=token, content_type=content_type, **kwargs)

    def upload_ok(self, data=PHOTO, token="default"):
        resp = self.upload(data, token)
        self.assertEqual(resp.status, 200, resp)
        photo = resp.json()["user"]["photo"]
        self.assertTrue(TOKEN_RE.fullmatch(photo), photo)
        return photo

    def photo_get(self, photo, token="default", method="GET"):
        token = self.token if token == "default" else token
        return self.request(method, f"/api/photos/{photo}", token=token)

    def stored(self, user_id):
        """(photo_token, jpeg bytes or None) as stored."""
        with self.db() as conn:
            token = conn.execute("SELECT photo_token FROM users WHERE id = ?", (user_id,)).fetchone()[0]
            row = conn.execute("SELECT jpeg FROM user_photos WHERE user_id = ?", (user_id,)).fetchone()
        return token, (bytes(row[0]) if row else None)

    def photo_rows(self):
        with self.db() as conn:
            return conn.execute("SELECT COUNT(*) FROM user_photos").fetchone()[0]

    def remove_own(self, token="default", **kwargs):
        return self.api("POST", "/api/account/photo/delete", {}, token=token, **kwargs)

    def admin_remove(self, user_id, token="default"):
        return self.api("POST", f"/api/admin/users/{user_id}/photo/delete", {}, token=token)

    def access_log(self, needle, timeout=2):
        """The log, once a line containing `needle` is in it (access lines are written after the response)."""
        deadline = time.monotonic() + timeout
        while needle not in self.log.getvalue() and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.log.getvalue()

    def assert_error(self, resp, status, code):
        self.assertEqual((resp.status, resp.json()["code"]), (status, code), resp)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        self.assertEqual(resp.headers.get_all("Content-Security-Policy"), [GLOBAL_CSP])


class PhotoTestCase(PhotoMixin, MealTestCase):
    """Admin (id 1), bob (participant in Trip), sam (viewer), eve (outsider)."""


# --- Upload (§3) ---

class UploadTests(PhotoTestCase):
    def test_upload(self):
        session_before = self.session_user(self.token)
        self.assertIsNone(session_before["photo"])
        resp = self.upload()
        self.assertEqual(resp.status, 200, resp)
        body = resp.json()
        self.assertEqual(set(body), {"user"})
        user = body["user"]
        token, stored = self.stored(1)
        self.assertEqual(user, {**session_before, "photo": token})
        self.assertTrue(TOKEN_RE.fullmatch(token))
        self.assertEqual(stored, photos.rebuild(PHOTO), "only the rebuilt bytes are stored")
        self.assertNotEqual(stored, PHOTO)
        self.assertEqual(self.session_user(self.token), user)
        resp = self.photo_get(token)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, stored)

    def test_real_fixtures_upload(self):
        for name, data in jpeg.REAL_FIXTURES.items():
            with self.subTest(name=name):
                photo = self.upload_ok(data)
                self.assertEqual(self.photo_get(photo).body, photos.rebuild(data))

    def test_token_rotates_and_old_address_404s(self):
        first = self.upload_ok(PHOTO)
        second = self.upload_ok(DETAILED)
        third = self.upload_ok(PHOTO)  # the same image again still gets a new address
        self.assertEqual(len({first, second, third}), 3)
        self.assert_error(self.photo_get(first), 404, "photo_not_found")
        self.assert_error(self.photo_get(second), 404, "photo_not_found")
        self.assertEqual(self.photo_get(third).status, 200)
        self.assertEqual(self.photo_rows(), 1, "one row per user, replaced")

    def test_token_not_derived(self):
        photo = self.upload_ok()
        with self.db() as conn:
            seed, username = conn.execute("SELECT avatar_seed, username FROM users WHERE id = 1").fetchone()
        for value in (seed, seed[:8], username, str(self.base_time)):
            self.assertNotIn(value, photo)

    def test_only_own_photo(self):
        photo = self.upload_ok(token=self.bob_token)
        self.assertEqual(self.stored(self.bob)[0], photo)
        self.assertEqual(self.stored(1), (None, None))

    def test_query_string_ignored(self):
        resp = self.upload(path=f"/api/account/photo?user_id={self.bob}&token={CLIENT_TOKEN}")
        self.assertEqual(resp.status, 200, resp)
        self.assertNotEqual(self.stored(1)[0], CLIENT_TOKEN)
        self.assertIsNotNone(self.stored(1)[0])
        self.assertEqual(self.stored(self.bob), (None, None))

    def test_media_type_variants_accepted(self):
        for content_type in ("image/jpeg; charset=x", "IMAGE/JPEG", " image/jpeg ", "Image/Jpeg;q=1"):
            with self.subTest(content_type=content_type):
                resp = self.upload(content_type=content_type)
                self.assertEqual(resp.status, 200, resp)

    def test_invalid_images(self):
        cases = {"png": b"\x89PNG\r\n\x1a\n" + bytes(100), "text": b"hello",
                 "trailing zip": PHOTO + b"PK\x03\x04" + bytes(40),
                 "second soi": b"\xff\xd8" + PHOTO, "truncated": PHOTO[:-10],
                 "wrong size": PHOTO.replace(b"\xff\xc0\x00\x11\x08\x01\x00\x01\x00", b"\xff\xc0\x00\x11\x08\x02\x00\x02\x00")}
        for name, data in cases.items():
            with self.subTest(name=name):
                resp = self.upload(data)
                self.assert_error(resp, 400, "invalid_image")
                self.assertEqual(resp.json(), {"error": "That photo couldn't be read. Try another photo.",
                                               "code": "invalid_image"}, "no parser detail")
        self.assertEqual(self.stored(1), (None, None))

    def test_invalid_replacement_keeps_the_old_photo(self):
        photo = self.upload_ok()
        self.assert_error(self.upload(b"\xff\xd8junk"), 400, "invalid_image")
        self.assertEqual(self.stored(1)[0], photo)
        self.assertEqual(self.photo_get(photo).status, 200)

    def test_needs_login_and_parser_not_called(self):
        with mock.patch.object(photos, "rebuild", wraps=photos.rebuild) as rebuild:
            for token in (None, "not-a-session"):
                with self.subTest(token=token):
                    self.assert_error(self.upload(token=token), 401, "not_logged_in")
            self.assertEqual(rebuild.call_count, 0)
        self.assertEqual(self.photo_rows(), 0)

    def test_pending_password_change(self):
        with self.db() as conn:
            conn.execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (self.bob,))
        with mock.patch.object(photos, "rebuild", wraps=photos.rebuild) as rebuild:
            resp = self.upload(token=self.bob_token)
        self.assertEqual(resp.status, 403)
        self.assertEqual(resp.json(), PENDING)
        self.assertEqual(rebuild.call_count, 0)
        self.assertEqual(self.stored(self.bob), (None, None))

    def test_session_revoked_mid_request(self):
        """§4b-9: the session is re-checked inside the write transaction, after the parse."""
        original = photos.rebuild

        def rebuild(data):
            with self.db() as conn:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (self.bob,))
            return original(data)

        with mock.patch.object(photos, "rebuild", rebuild):
            resp = self.upload(token=self.bob_token)
        self.assert_error(resp, 401, "not_logged_in")
        self.assertEqual(self.stored(self.bob), (None, None))

    def test_renews_the_session(self):
        self.clock_offset = 3601
        resp = self.upload(token=self.bob_token)
        self.assertEqual(resp.status, 200)
        cookies = resp.set_cookies(self.cookie_name)
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookie_value(cookies[0]), self.bob_token)
        self.assertIn("Max-Age=2592000", cookie_attributes(cookies[0]))

    def test_disabled_users_photo_still_served(self):
        photo = self.upload_ok(token=self.bob_token)
        self.assertEqual(self.admin_action(self.bob, "disable").status, 200)
        resp = self.photo_get(photo)
        self.assertEqual(resp.status, 200)
        detail = self.get(self.pid).json()
        bob = next(p for p in detail["participants"] if p["id"] == self.bob)
        self.assertEqual((bob["inactive"], bob["photo"]), (True, photo))

    def test_no_log_line(self):
        self.log.truncate(0)
        self.log.seek(0)
        photo = self.upload_ok()
        log = self.access_log("POST /api/account/photo 200")
        self.assertNotIn(photo, log)
        for line in log.splitlines():
            self.assertRegex(line, r"^[A-Z]+ /\S* \d{3}$")


# --- Transport (§5-1 … §5-10) ---

class TransportTests(PhotoTestCase):
    def raw_upload(self, headers, body=b"", token="default"):
        token = self.token if token == "default" else token
        cookie = [("Cookie", f"{self.cookie_name}={token}")] if token else []
        return self.raw_request("POST", "/api/account/photo", headers=list(headers) + cookie, body=body)

    def headers(self, length, content_type="image/jpeg"):
        return [("Origin", self.origin), ("Content-Type", content_type), ("Content-Length", str(length))]

    def test_origin(self):
        for origin in ("http://evil.test", None, "null", f"http://{self.host}.evil.test"):
            with self.subTest(origin=origin):
                self.assert_error(self.upload(origin=origin), 403, "cross_origin")
        self.assert_error(self.upload(origin="http://evil.test", content_type="text/plain"), 403, "cross_origin")
        self.assertEqual(self.photo_rows(), 0)

    def test_media_type(self):
        for content_type in ("text/plain", "multipart/form-data; boundary=x", "application/json", "image/jpg",
                             "image/png", "image/pjpeg", "application/x-www-form-urlencoded", "image/jpeg2", None):
            with self.subTest(content_type=content_type):
                self.assert_error(self.upload(content_type=content_type), 415, "content_type_jpeg")
        self.assertEqual(self.photo_rows(), 0)

    def test_jpeg_to_other_routes(self):
        for path in ("/api/account/avatar", "/api/account/photo/delete", "/api/logout", "/api/account"):
            with self.subTest(path=path):
                method = "PATCH" if path == "/api/account" else "POST"
                resp = self.request(method, path, raw_body=PHOTO[:100], token=self.token, content_type="image/jpeg")
                self.assert_error(resp, 415, "content_type_json")

    def test_content_length(self):
        self.assert_error(self.raw_upload([("Origin", self.origin), ("Content-Type", "image/jpeg")]),
                          411, "length_required")
        for value in ("abc", "-1", "1e3", "", "1 0", "0x10", "1000000", "0000001", "²"):
            with self.subTest(value=value):
                headers = [("Origin", self.origin), ("Content-Type", "image/jpeg"), ("Content-Length", value)]
                self.assert_error(self.raw_upload(headers), 400, "invalid_content_length")
        for value in (131073, 999999):
            with self.subTest(value=value):
                self.assert_error(self.raw_upload(self.headers(value)), 413, "too_large")

    def test_the_limit_itself_is_read(self):
        """131,072 bytes pass the reader (and reach the parser, which refuses the padding)."""
        body = PHOTO + bytes(photos.MAX_BYTES - len(PHOTO))
        self.assertEqual(len(body), 131072)
        self.assert_error(self.raw_upload(self.headers(len(body)), body), 400, "invalid_image")

    def test_empty_body(self):
        """§9.9: Content-Length 0 is a 400 in the reader, before auth, and isn't counted."""
        self.assert_error(self.raw_upload(self.headers(0)), 400, "invalid_image")
        self.assert_error(self.raw_upload(self.headers(0), token=None), 400, "invalid_image")
        self.assertEqual(self.app.upload_limiter.stamps, {})

    def test_chunked(self):
        headers = [("Origin", self.origin), ("Content-Type", "image/jpeg"), ("Transfer-Encoding", "chunked")]
        self.assert_error(self.raw_upload(headers, b"10\r\n" + PHOTO[:16] + b"\r\n0\r\n\r\n"), 411, "length_required")

    def test_duplicate_headers(self):
        for extra in (("Content-Length", str(len(PHOTO))), ("Content-Type", "image/jpeg")):
            with self.subTest(header=extra[0]):
                self.assert_error(self.raw_upload(self.headers(len(PHOTO)) + [extra], PHOTO), 400, "bad_request")

    def test_short_body_closes_without_a_response(self):
        self.app.body_deadline = 0.5
        resp = self.raw_upload(self.headers(len(PHOTO)), PHOTO[:1000])
        self.assertIsNone(resp.status)
        self.assertEqual(resp.body, b"")
        self.assertEqual(self.photo_rows(), 0)

    def test_methods(self):
        for method in ("PUT", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                resp = self.raw_request(method, "/api/account/photo")
                self.assertEqual(resp.status, 405)
                self.assertFalse([h for h in resp.headers if h.lower().startswith("access-control-")])
        for method in ("GET", "PATCH"):
            with self.subTest(method=method):
                self.assert_error(self.api(method, "/api/account/photo"), 405, "method_not_allowed")
        resp = self.raw_request("PUT", f"/api/photos/{CLIENT_TOKEN}")
        self.assertEqual(resp.status, 405)
        self.assert_error(self.api("POST", f"/api/photos/{CLIENT_TOKEN}"), 405, "method_not_allowed")

    def test_json_reader_unchanged(self):
        self.assert_error(self.raw_request("POST", "/api/logout", headers=[
            ("Origin", self.origin), ("Content-Type", "application/json"), ("Content-Length", "16385")]),
            413, "too_large")
        self.assert_error(self.raw_request("POST", "/api/logout", headers=[
            ("Origin", self.origin), ("Content-Type", "application/json"), ("Content-Length", "100000")]),
            400, "invalid_content_length")
        self.assertEqual(server.CONTENT_LENGTH_RE.pattern, "[0-9]{1,5}")
        self.assertEqual(server.MAX_BODY, 16384)
        self.assertEqual(server.HANDLED_METHODS, frozenset({"GET", "HEAD", "POST", "PATCH"}))


# --- Removing (§3, §9.5) ---

class RemoveTests(PhotoTestCase):
    def test_remove_own(self):
        photo = self.upload_ok()
        before = self.session_user(self.token)
        resp = self.remove_own()
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(resp.json(), {"user": {**before, "photo": None}})
        self.assertEqual(self.stored(1), (None, None))
        self.assert_error(self.photo_get(photo), 404, "photo_not_found")
        self.assertIsNone(self.session_user(self.token)["photo"])

    def test_remove_own_is_idempotent_and_ignores_the_body(self):
        for body in ({}, {"photo": CLIENT_TOKEN, "user_id": self.bob}):
            with self.subTest(body=body):
                resp = self.api("POST", "/api/account/photo/delete", body)
                self.assertEqual(resp.status, 200, resp)
                self.assertIsNone(resp.json()["user"]["photo"])
        self.assertEqual(self.request("POST", "/api/account/photo/delete", raw_body=b"", token=self.token).status, 200)

    def test_remove_own_leaves_others(self):
        bob_photo = self.upload_ok(token=self.bob_token)
        self.upload_ok()
        self.assertEqual(self.remove_own().status, 200)
        self.assertEqual(self.stored(self.bob)[0], bob_photo)
        self.assertEqual(self.photo_get(bob_photo).status, 200)

    def test_remove_own_checks(self):
        self.upload_ok()
        self.assert_error(self.remove_own(token=None), 401, "not_logged_in")
        self.assert_error(self.remove_own(content_type="text/plain"), 415, "content_type_json")
        self.assert_error(self.remove_own(content_type="image/jpeg"), 415, "content_type_json")
        self.assert_error(self.remove_own(origin="http://evil.test"), 403, "cross_origin")
        with self.db() as conn:
            conn.execute("UPDATE users SET must_change_password = 1 WHERE id = 1")
        self.assertEqual(self.remove_own().json(), PENDING)
        self.assertIsNotNone(self.stored(1)[0])

    def test_remove_own_session_revoked(self):
        self.upload_ok(token=self.bob_token)
        original = server.Handler.load_session

        def load_session(handler, renew):
            original(handler, renew)
            if handler.raw_path == "/api/account/photo/delete":
                with self.db() as conn:
                    conn.execute("DELETE FROM sessions WHERE user_id = ?", (self.bob,))

        with mock.patch.object(server.Handler, "load_session", load_session):
            self.assert_error(self.remove_own(token=self.bob_token), 401, "not_logged_in")
        self.assertIsNotNone(self.stored(self.bob)[0])

    def test_admin_remove(self):
        photo = self.upload_ok(token=self.bob_token)
        self.log.truncate(0)
        self.log.seek(0)
        resp = self.admin_remove(self.bob)
        self.assertEqual(resp.status, 200, resp)
        user = resp.json()["user"]
        self.assertEqual((user["id"], user["photo"]), (self.bob, None))
        self.assertEqual(user, next(u for u in self.api("GET", "/api/admin/users").json()["users"]
                                    if u["id"] == self.bob))
        self.assertEqual(self.stored(self.bob), (None, None))
        self.assert_error(self.photo_get(photo), 404, "photo_not_found")
        log = self.access_log("photo/delete 200")
        self.assertIn("Photo removed for bob by admin", log)
        self.assertNotIn(photo, log)
        # Idempotent.
        self.assertEqual(self.admin_remove(self.bob).status, 200)

    def test_admin_remove_own_and_disabled(self):
        self.upload_ok()
        self.assertEqual(self.admin_remove(1).status, 200)
        self.assertEqual(self.stored(1), (None, None))
        self.upload_ok(token=self.bob_token)
        self.assertEqual(self.admin_action(self.bob, "disable").status, 200)
        self.assertEqual(self.admin_remove(self.bob).status, 200)
        self.assertEqual(self.stored(self.bob), (None, None))

    def test_admin_remove_checks(self):
        photo = self.upload_ok(token=self.sam_token)
        self.assert_error(self.admin_remove(self.sam, token=self.bob_token), 403, "admins_only")
        self.assert_error(self.admin_remove(self.sam, token=None), 401, "not_logged_in")
        self.assert_error(self.admin_remove(9999), 404, "user_not_found")
        self.assert_error(self.api("POST", "/api/admin/users/0/photo/delete"), 404, "not_found")
        self.assertEqual(self.stored(self.sam)[0], photo)

    def test_admin_demoted_mid_request(self):
        self.upload_ok(token=self.sam_token)
        original = server.Handler.load_session

        def load_session(handler, renew):
            original(handler, renew)
            if handler.raw_path.endswith("/photo/delete"):
                with self.db() as conn:
                    conn.execute("UPDATE users SET is_admin = 0 WHERE id = 1")

        with mock.patch.object(server.Handler, "load_session", load_session):
            self.assert_error(self.admin_remove(self.sam), 403, "admins_only")
        self.assertIsNotNone(self.stored(self.sam)[0])


# --- Serving (§3, §9.2-§9.4, §9.9) ---

class ServeTests(PhotoTestCase):
    def test_headers(self):
        photo = self.upload_ok()
        resp = self.photo_get(photo, token=self.eve_token)  # anyone logged in, even outside every passport
        self.assertEqual(resp.status, 200)
        for name, value in PHOTO_HEADERS.items():
            with self.subTest(header=name):
                self.assertEqual(resp.headers.get_all(name), [value])
        self.assertEqual(resp.headers["Content-Length"], str(len(resp.body)))
        self.assertIsNone(resp.headers.get("Set-Cookie"))
        expected = set(PHOTO_HEADERS) | {"Content-Length", "Server", "Date"}
        self.assertEqual({h for h in resp.headers}, expected)

    def test_head(self):
        photo = self.upload_ok()
        get = self.photo_get(photo)
        resp = self.photo_get(photo, method="HEAD")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"")
        self.assertEqual(resp.headers["Content-Length"], str(len(get.body)))
        self.assertEqual(resp.headers.get_all("Content-Security-Policy"), [PHOTO_HEADERS["Content-Security-Policy"]])

    def test_other_responses_keep_the_global_csp(self):
        self.upload_ok()
        for path in ("/api/session", "/", "/app.js"):
            with self.subTest(path=path):
                resp = self.request("GET", path, token=self.token)
                self.assertEqual(resp.headers.get_all("Content-Security-Policy"), [GLOBAL_CSP])
                self.assertIsNone(resp.headers.get("Cross-Origin-Resource-Policy"))
        resp = self.request("GET", "/health")
        self.assertEqual(resp.headers.get_all("Content-Security-Policy"), [GLOBAL_CSP])

    def test_errors(self):
        photo = self.upload_ok()
        self.assert_error(self.photo_get(photo, token=None), 401, "not_logged_in")
        self.assert_error(self.photo_get(photo, token="not-a-session"), 401, "not_logged_in")
        self.assert_error(self.photo_get(CLIENT_TOKEN), 404, "photo_not_found")
        self.assert_error(self.photo_get(photo.swapcase() if photo.swapcase() != photo else CLIENT_TOKEN),
                          404, "photo_not_found")
        with self.db() as conn:
            conn.execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (self.bob,))
        resp = self.photo_get(photo, token=self.bob_token)
        self.assertEqual((resp.status, resp.json()), (403, PENDING))

    def test_bad_paths_404_before_auth(self):
        photo = self.upload_ok()
        for path in (photo[:42], photo + "A", photo[:42] + "=", photo[:42] + ".", "", photo + "/",
                     photo[:42] + "%41", photo[:42] + "%CE%91"):  # %CE%91: a Greek capital alpha, percent-encoded
            with self.subTest(path=path):
                for token in (None, self.token):
                    resp = self.request("GET", "/api/photos/" + path, token=token)
                    self.assert_error(resp, 404, "not_found")
        resp = self.request("GET", f"/api/photos/{photo}?x=1", token=self.token)
        self.assertEqual(resp.status, 200)

    def test_no_session_renewal(self):
        photo = self.upload_ok(token=self.bob_token)
        with self.db() as conn:
            before = conn.execute("SELECT last_used_at, expires_at FROM sessions WHERE user_id = ?",
                                  (self.bob,)).fetchone()
            active = conn.execute("SELECT last_active_at FROM users WHERE id = ?", (self.bob,)).fetchone()
        self.clock_offset = 7200
        resp = self.photo_get(photo, token=self.bob_token)
        self.assertEqual(resp.status, 200)
        self.assertIsNone(resp.headers.get("Set-Cookie"))
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT last_used_at, expires_at FROM sessions WHERE user_id = ?",
                                          (self.bob,)).fetchone(), before)
            self.assertEqual(conn.execute("SELECT last_active_at FROM users WHERE id = ?", (self.bob,)).fetchone(),
                             active)

    def test_no_write_lock_taken(self):
        photo = self.upload_ok()
        self.clock_offset = 7200
        with self.app.write_lock:
            resp = self.photo_get(photo)
        self.assertEqual(resp.status, 200)

    def test_raw_response_never_sets_a_cookie(self):
        """§9.2: route() refuses a RawBody when a cookie is pending (a 500, not a cached Set-Cookie)."""
        def handler(req):
            req.clear_session_cookie()
            return server.RawBody(b"x", "image/jpeg", "no-store", "default-src 'none'")

        self.add_route("/api/rawtest", {"GET": server.Route(handler)})
        resp = self.request("GET", "/api/rawtest", token=self.token)
        self.assertEqual(resp.status, 500)
        self.assertIsNone(resp.headers.get("Set-Cookie"))

    def test_request_log_masks_the_token(self):
        photo = self.upload_ok()
        self.photo_get(photo)
        self.photo_get(CLIENT_TOKEN)
        self.photo_get(photo, token=None)
        log = self.access_log("GET /api/photos/* 401")
        self.assertIn("GET /api/photos/* 200", log)
        self.assertIn("GET /api/photos/* 404", log)
        self.assertNotIn(photo, log)
        self.assertNotIn(CLIENT_TOKEN, log)


# --- photo in every object (§3) ---

class ObjectTests(PhotoTestCase):
    def setUp(self):
        super().setUp()
        self.admin_photo = self.upload_ok()
        self.bob_photo = self.upload_ok(token=self.bob_token)

    def check(self, person):
        self.assertIn("photo", person)
        expected = {1: self.admin_photo, self.bob: self.bob_photo}.get(person["id"])
        self.assertEqual(person["photo"], expected)

    def test_account_objects(self):
        self.check(self.session_user(self.token))
        self.check(self.login().json()["user"])
        for body in ({"display_name": "Boss"}, {"default_passport_id": None}):
            self.check(self.api("PATCH", "/api/account", body).json()["user"])
        self.check(self.api("POST", "/api/account/avatar", {}).json()["user"])

    def test_people(self):
        for user in self.api("GET", "/api/users").json()["users"]:
            self.check(user)
        for item in self.listing(self.bob_token)["passports"]:
            self.check(item["owner"])
        detail = self.get(self.pid, token=self.sam_token).json()
        self.check(detail["passport"]["owner"])
        for person in detail["participants"] + detail["viewers"]:
            self.check(person)
        self.assertEqual(self.log_meal(self.pid, "PE", token=self.bob_token).status, 201)
        self.assertEqual(self.api("POST", f"/api/passports/{self.pid}/countries/JP/next-stops",
                                  {"list": "mine", "on": True}, token=self.bob_token).status, 200)
        self.assertEqual(self.api("POST", f"/api/passports/{self.pid}/countries/JP/next-stops",
                                  {"list": "ours", "on": True}, token=self.bob_token).status, 200)
        page = self.country(self.pid, "PE", token=self.sam_token)
        for meal in page["meals"]:
            self.check(meal["user"])
        for person in page["participants"]:
            self.check(person)
        stops = self.country(self.pid, "JP")["next_stops"]
        self.check(stops["people"][0])
        self.check(stops["shared"]["added_by"])
        handed = self.api("GET", "/api/account/delete-preview").json()["handed_over"]
        self.check(handed[0]["new_owner"])

    def test_admin_objects(self):
        for user in self.api("GET", "/api/admin/users").json()["users"]:
            self.check(user)
        for name in ("disable", "enable", "promote", "demote", "unlock"):
            with self.subTest(action=name):
                self.check(self.admin_action(self.bob, name).json()["user"])
        self.check(self.api("PATCH", f"/api/admin/users/{self.bob}", {"display_name": "B"}).json()["user"])
        created = self.api("POST", "/api/admin/users", {"username": "zed", "display_name": "Zed",
                                                         "password": PASSWORD, "photo": CLIENT_TOKEN}).json()["user"]
        self.assertIsNone(created["photo"])

    def test_account_patch_returns_the_stored_photo(self):
        """§9.6: a photo changed after the session loaded shows up in the PATCH and avatar responses."""
        original = server.Handler.load_session

        def load_session(handler, renew):
            original(handler, renew)
            if handler.command == "POST" and handler.raw_path == "/api/account/avatar" or handler.command == "PATCH":
                with self.db() as conn:
                    conn.execute("DELETE FROM user_photos WHERE user_id = 1")
                    conn.execute("UPDATE users SET photo_token = NULL WHERE id = 1")

        with mock.patch.object(server.Handler, "load_session", load_session):
            self.assertIsNone(self.api("PATCH", "/api/account", {"default_passport_id": None}).json()["user"]["photo"])
        self.admin_photo = self.upload_ok()
        with mock.patch.object(server.Handler, "load_session", load_session):
            self.assertIsNone(self.api("POST", "/api/account/avatar", {}).json()["user"]["photo"])


class ClientPhotoIgnoredTests(PhotoMixin, ServerTestCase):
    def test_setup_ignores_photo(self):
        resp = self.request("POST", "/api/setup", {"username": "admin", "display_name": "Admin",
                                                    "password": PASSWORD, "photo": CLIENT_TOKEN,
                                                    "photo_token": CLIENT_TOKEN})
        self.assertEqual(resp.status, 201, resp)
        self.assertIsNone(resp.json()["user"]["photo"])
        self.assertEqual(self.stored(1), (None, None))

    def test_other_endpoints_ignore_photo(self):
        self.token = self.setup_admin()
        bob = self.request("POST", "/api/admin/users", {"username": "bob", "display_name": "Bob",
                                                         "password": PASSWORD, "photo": CLIENT_TOKEN},
                           token=self.token).json()["user"]["id"]
        self.assertEqual(self.stored(bob), (None, None))
        mine = self.upload_ok()
        for method, path, body in (("PATCH", "/api/account", {"display_name": "A", "photo": CLIENT_TOKEN}),
                                   ("PATCH", f"/api/admin/users/{bob}", {"display_name": "B", "photo": CLIENT_TOKEN}),
                                   ("PATCH", f"/api/admin/users/1", {"display_name": "C", "photo": None}),
                                   ("POST", "/api/account/avatar", {"photo": CLIENT_TOKEN, "photo_token": None})):
            with self.subTest(path=path):
                resp = self.request(method, path, body, token=self.token)
                self.assertEqual(resp.status, 200, resp)
                self.assertEqual(self.stored(1)[0], mine)
                self.assertEqual(self.stored(bob), (None, None))


# --- Account deletion (§1, §6-6, 1b L5) ---

class DeletionTests(PhotoTestCase):
    def test_self_delete_removes_the_photo(self):
        photo = self.upload_ok(token=self.eve_token)
        self.self_delete(self.eve, self.eve_token)
        self.deleted_ids.append(self.eve)
        self.assertEqual(self.photo_rows(), 0)
        self.assert_error(self.photo_get(photo), 404, "photo_not_found")

    def test_admin_delete_removes_the_photo(self):
        photo = self.upload_ok(token=self.eve_token)
        self.admin_delete(self.eve, "eve")
        self.assertEqual(self.photo_rows(), 0)
        self.assert_error(self.photo_get(photo), 404, "photo_not_found")

    def test_id_reuse_inherits_no_photo(self):
        self.upload_ok(token=self.eve_token)
        self.admin_delete(self.eve, "eve")
        resp = self.api("POST", "/api/admin/users", {"username": "neo", "display_name": "Neo", "password": PASSWORD})
        self.assertEqual(resp.status, 201, resp)
        user = resp.json()["user"]
        self.assertEqual(user["id"], self.eve, "the id was reused")
        self.assertIsNone(user["photo"])
        self.assertEqual(self.stored(self.eve), (None, None))


# --- Rate limits (§6) ---

class RateLimitTestCase(PhotoMixin, MealTestCase):
    app_kwargs = {"upload_wait": 0.2}

    def stamps(self, user_id):
        return list(self.app.upload_limiter.stamps.get(user_id, []))


class RateLimitTests(RateLimitTestCase):
    def test_ten_an_hour(self):
        for _ in range(10):
            last = self.upload_ok()
        resp = self.upload(DETAILED)
        self.assert_error(resp, 429, "too_many_uploads")
        self.assertEqual(resp.json(), {"error": "Too many photo changes. Try again later.", "code": "too_many_uploads"})
        self.assertIsNone(resp.headers.get("Retry-After"))
        self.assertEqual(self.stored(1)[0], last, "stored token unchanged")
        self.clock_offset = 3599
        self.assert_error(self.upload(), 429, "too_many_uploads")
        self.clock_offset = 3601  # all ten were at 0, so all have left the window
        for _ in range(10):
            self.upload_ok()
        self.assert_error(self.upload(), 429, "too_many_uploads")

    def test_sliding_window(self):
        for step in range(10):
            self.clock_offset = step * 200  # 0 … 1,800 s
            self.upload_ok()
        self.assert_error(self.upload(), 429, "too_many_uploads")
        self.clock_offset = 3600  # the first (at 0) has left the window
        self.upload_ok()
        self.assert_error(self.upload(), 429, "too_many_uploads")
        self.clock_offset = 3800  # and the second
        self.upload_ok()

    def test_invalid_uploads_count(self):
        for _ in range(10):
            self.assert_error(self.upload(b"\xff\xd8 not a jpeg"), 400, "invalid_image")
        self.assert_error(self.upload(), 429, "too_many_uploads")
        self.assertEqual(self.stored(1), (None, None))

    def test_refused_before_the_slot_dont_count(self):
        for _ in range(3):
            self.assert_error(self.upload(token=None), 401, "not_logged_in")
            self.assert_error(self.upload(origin="http://evil.test"), 403, "cross_origin")
            self.assert_error(self.upload(content_type="text/plain"), 415, "content_type_jpeg")
            self.assert_error(self.raw_request("POST", "/api/account/photo", headers=[
                ("Origin", self.origin), ("Content-Type", "image/jpeg"), ("Content-Length", "131073"),
                ("Cookie", f"{self.cookie_name}={self.token}")]), 413, "too_large")
            self.assert_error(self.upload(b""), 400, "invalid_image")
        with self.db() as conn:
            conn.execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (self.bob,))
        self.assertEqual(self.upload(token=self.bob_token).status, 403)
        self.assertEqual(self.app.upload_limiter.stamps, {})
        for _ in range(10):
            self.upload_ok()

    def test_per_user(self):
        for _ in range(10):
            self.upload_ok()
        self.assert_error(self.upload(), 429, "too_many_uploads")
        self.upload_ok(token=self.bob_token)
        self.assertEqual(len(self.stamps(1)), 10)
        self.assertEqual(len(self.stamps(self.bob)), 1)

    def test_sessions_of_one_user_share_the_limit(self):
        other = self.login_token("bob")
        for i in range(10):
            self.upload_ok(token=self.bob_token if i % 2 else other)
        self.assert_error(self.upload(token=other), 429, "too_many_uploads")
        self.assert_error(self.upload(token=self.bob_token), 429, "too_many_uploads")

    def test_removes_not_limited(self):
        for _ in range(10):
            self.upload_ok(token=self.bob_token)
        self.assert_error(self.upload(token=self.bob_token), 429, "too_many_uploads")
        for _ in range(15):
            self.assertEqual(self.remove_own(token=self.bob_token).status, 200)
        self.upload_ok()
        self.assertEqual(self.admin_remove(1).status, 200)
        self.assertEqual(len(self.stamps(self.bob)), 10)

    def test_self_delete_clears_the_counter(self):
        for _ in range(10):
            self.upload_ok(token=self.eve_token)
        self.self_delete(self.eve, self.eve_token)
        self.deleted_ids.append(self.eve)
        self.assertEqual(self.stamps(self.eve), [])
        new_id, token = self.user("neo")
        self.assertEqual(new_id, self.eve, "the id was reused")
        self.upload_ok(token=token)

    def test_admin_delete_clears_the_counter(self):
        for _ in range(10):
            self.upload_ok(token=self.eve_token)
        self.admin_delete(self.eve, "eve")
        self.assertEqual(self.stamps(self.eve), [])
        new_id, token = self.user("neo")
        self.assertEqual(new_id, self.eve)
        self.upload_ok(token=token)

    def test_disable_and_enable_keep_the_counter(self):
        for _ in range(10):
            self.upload_ok(token=self.bob_token)
        self.assertEqual(self.admin_action(self.bob, "disable").status, 200)
        self.assertEqual(self.admin_action(self.bob, "enable").status, 200)
        self.assertEqual(len(self.stamps(self.bob)), 10)
        self.assert_error(self.upload(token=self.login_token("bob")), 429, "too_many_uploads")

    def test_nothing_logged_on_429_or_503(self):
        for _ in range(10):
            self.upload_ok()
        self.log.truncate(0)
        self.log.seek(0)
        self.upload()
        log = self.access_log("POST /api/account/photo 429")
        for line in log.splitlines():
            self.assertRegex(line, r"^[A-Z]+ /\S* \d{3}$")


class NoSlotTests(RateLimitTestCase):
    app_kwargs = {"upload_slots": 0, "upload_wait": 0}

    def test_busy(self):
        with mock.patch.object(photos, "rebuild", wraps=photos.rebuild) as rebuild:
            resp = self.upload()
        self.assert_error(resp, 503, "busy")
        self.assertEqual(rebuild.call_count, 0, "parser not called")
        self.assertEqual(self.stamps(1), [], "a 503 isn't counted")
        self.assertEqual(self.stored(1), (None, None))
        self.assertEqual(self.remove_own().status, 200, "removes take no slot")
        self.assertEqual(self.admin_remove(1).status, 200)


class OneSlotTests(RateLimitTestCase):
    app_kwargs = {"upload_slots": 1, "upload_wait": 0}

    def test_slot_released_after_invalid_images(self):
        for _ in range(5):
            self.assert_error(self.upload(b"\xff\xd8\xff"), 400, "invalid_image")
        self.upload_ok()

    def test_slot_released_after_a_parser_crash(self):
        with mock.patch.object(photos, "rebuild", side_effect=RuntimeError("boom")):
            for _ in range(3):
                self.assert_error(self.upload(), 500, "internal_error")
        self.upload_ok()
        self.assertEqual(len(self.stamps(1)), 4, "the crashed attempts got a slot, so they count")

    def test_slot_released_after_429(self):
        for _ in range(10):
            self.upload_ok(token=self.bob_token)
        self.assert_error(self.upload(token=self.bob_token), 429, "too_many_uploads")
        self.upload_ok()

    def test_slot_not_held_across_the_write_lock(self):
        """§6-3: an upload waiting for the write lock has already released its slot."""
        results = {}

        def run(name, token):
            results[name] = self.upload(token=token)

        with self.app.write_lock:
            first = threading.Thread(target=run, args=("admin", self.token))
            first.start()
            deadline = time.monotonic() + 5
            while not self.stamps(1) and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.2)  # admin's parse is done and it now waits for the write lock
            with mock.patch.object(photos, "rebuild", wraps=photos.rebuild) as rebuild:
                second = threading.Thread(target=run, args=("bob", self.bob_token))
                second.start()
                deadline = time.monotonic() + 5
                while not rebuild.call_count and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(rebuild.call_count, 1, "bob's parse ran while the write lock was held")
        first.join(10)
        second.join(10)
        self.assertEqual(results["admin"].status, 200, results["admin"])
        self.assertEqual(results["bob"].status, 200, results["bob"])


class TwoSlotTests(RateLimitTestCase):
    app_kwargs = {"upload_wait": 0.3}

    def test_at_most_two_parse_at_once(self):
        release = threading.Event()
        started = threading.Semaphore(0)
        original = photos.rebuild

        def slow(data):
            started.release()
            release.wait(10)
            return original(data)

        results = {}

        def run(name, token):
            results[name] = self.upload(token=token)

        with mock.patch.object(photos, "rebuild", slow):
            threads = [threading.Thread(target=run, args=(n, t)) for n, t in (("a", self.token), ("b", self.bob_token))]
            for thread in threads:
                thread.start()
            self.assertTrue(started.acquire(timeout=5) and started.acquire(timeout=5))
            resp = self.upload(token=self.eve_token)
            release.set()
            for thread in threads:
                thread.join(10)
        self.assert_error(resp, 503, "busy")
        self.assertEqual(self.stamps(self.eve), [])
        self.assertEqual((results["a"].status, results["b"].status), (200, 200))


# --- UploadLimiter units (§6-1) ---

class UploadLimiterTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.limiter = auth.UploadLimiter(lambda: self.now, limit=3, window=100, max_keys=4)

    def test_limit_and_window(self):
        self.assertTrue(all(self.limiter.try_record(1) for _ in range(3)))
        self.assertFalse(self.limiter.try_record(1))
        self.assertEqual(len(self.limiter.stamps[1]), 3, "refusals aren't recorded")
        self.now += 99.9
        self.assertFalse(self.limiter.try_record(1))
        self.now += 0.1
        self.assertTrue(self.limiter.try_record(1))
        self.assertLessEqual(len(self.limiter.stamps[1]), 3)

    def test_lists_never_exceed_the_limit(self):
        for _ in range(50):
            self.limiter.try_record(7)
            self.now += 40
            self.assertLessEqual(len(self.limiter.stamps[7]), 3)

    def test_empty_keys_pruned(self):
        self.limiter.try_record(1)
        self.limiter.try_record(2)
        self.now += 100
        self.limiter.try_record(3)
        self.assertEqual(set(self.limiter.stamps), {3})

    def test_idle_oldest_evicted(self):
        for key in (1, 2, 3, 4):
            self.limiter.try_record(key)
            self.now += 1
        self.limiter.try_record(1)  # 1 is now the most recent
        self.now += 1
        self.assertTrue(self.limiter.try_record(5))
        self.assertEqual(set(self.limiter.stamps), {1, 3, 4, 5}, "2 had the oldest latest attempt")
        self.assertLessEqual(len(self.limiter.stamps), 4)

    def test_clear(self):
        for _ in range(3):
            self.limiter.try_record(1)
        self.limiter.try_record(2)
        self.limiter.clear(1)
        self.limiter.clear(99)
        self.assertEqual(set(self.limiter.stamps), {2})
        self.assertTrue(self.limiter.try_record(1))

    def test_app_defaults(self):
        self.assertEqual((server.UPLOAD_SLOTS, server.UPLOAD_WAIT, server.UPLOAD_LIMIT, server.UPLOAD_WINDOW),
                         (2, 1, 10, 3600))
        self.assertEqual(server.MAX_PHOTO, 131072)
        limiter = auth.UploadLimiter(time.time)
        self.assertEqual((limiter.limit, limiter.window, limiter.max_keys), (10, 3600, 10000))


# --- The server on an upgraded version-7 database (§2) ---

class MigrationServerTests(PhotoMixin, ServerTestCase):
    def prepare_data(self):
        import db  # noqa: E402
        path = f"{self.data_dir}/{db.DB_NAME}"
        db.init_schema(path, db.MIGRATIONS[:7])
        conn = db.connect(path)
        try:
            from tests.helpers import fast_hasher
            password_hash = fast_hasher().hash(PASSWORD)
            conn.executemany("INSERT INTO users (id, username, display_name, password_hash, is_admin, created_at, "
                             "avatar_seed) VALUES (?, ?, ?, ?, ?, 100, ?)",
                             [(1, "admin", "Admin", password_hash, 1, "a" * 32), (2, "bob", "Bob", password_hash, 0,
                                                                                  "b" * 32)])
            conn.executescript("""
                INSERT INTO passports (id, name, owner_id, is_personal, created_at)
                    VALUES (1, 'My passport', 1, 1, 100), (2, 'My passport', 2, 1, 100), (3, 'Trip', 1, 0, 100);
                INSERT INTO passport_members (passport_id, user_id, role, created_at)
                    VALUES (1, 1, 'participant', 100), (2, 2, 'participant', 100),
                           (3, 1, 'participant', 100), (3, 2, 'participant', 100);
                INSERT INTO meals (passport_id, user_id, country_code, eaten_on, created_at, updated_at)
                    VALUES (3, 2, 'PE', '2026-01-15', 1, 1);
            """)
            now = self.base_time
            conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, last_used_at, expires_at) "
                         "VALUES (?, 1, ?, ?, ?)", (sha256_hex("live-token"), now, now, now + 100000))
        finally:
            conn.close()

    def test_old_accounts_work_and_can_upload(self):
        self.token = "live-token"
        user = self.session_user(self.token)
        self.assertEqual((user["avatar_seed"], user["photo"]), ("a" * 32, None))
        detail = self.request("GET", "/api/passports/3", token=self.token).json()
        self.assertEqual([p["photo"] for p in detail["participants"]], [None, None])
        photo = self.upload_ok()
        self.assertEqual(self.photo_get(photo).body, photos.rebuild(PHOTO))
        meals = self.request("GET", "/api/passports/3/countries/PE", token=self.token).json()["meals"]
        self.assertEqual(len(meals), 1)


if __name__ == "__main__":
    unittest.main()
