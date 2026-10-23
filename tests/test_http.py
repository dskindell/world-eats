"""Request handling rules that apply to every request (contract section 3)."""

import json
import socket
import time

from tests.helpers import SECURITY_HEADERS, STATIC_CONTENT, ServerTestCase, server


class HostTests(ServerTestCase):
    env = {"WORLD_EATS_ALLOWED_HOSTS": "127.0.0.1,localhost,example.test:1234,[::1]"}

    def get_session(self, host):
        return self.raw_request("GET", "/api/session", host=host)

    def test_allowed_host(self):
        self.assertEqual(self.request("GET", "/api/session").status, 200)

    def test_portless_entry_matches_any_port(self):
        for host in ("localhost", "localhost:1", "localhost:65535", f"localhost:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(self.get_session(host).status, 200)

    def test_port_entry_matches_only_that_port(self):
        self.assertEqual(self.get_session("example.test:1234").status, 200)
        for host in ("example.test", "example.test:1235", "example.test:80"):
            with self.subTest(host=host):
                resp = self.get_session(host)
                self.assertEqual(resp.status, 421)
                self.assertEqual(resp.error, "Unknown host")

    def test_wrong_host(self):
        for host in ("evil.test", "localhost.evil.test", "evillocalhost", "127.0.0.2", "localhost.",
                     "localhost:abc", "[::2]", "", "[", "[]:80"):
            with self.subTest(host=host):
                self.assertEqual(self.get_session(host).status, 421)

    def test_uppercase_host(self):
        self.assertEqual(self.get_session("LOCALHOST").status, 200)
        self.assertEqual(self.get_session("EXAMPLE.TEST:1234").status, 200)

    def test_ipv6_host(self):
        self.assertEqual(self.get_session("[::1]:8080").status, 200)
        self.assertEqual(self.get_session("[::1]").status, 200)

    def test_missing_host(self):
        resp = self.raw_request("GET", "/api/session", host=None)
        self.assertEqual(resp.status, 421)
        self.assertEqual(resp.error, "Unknown host")

    def test_duplicate_host(self):
        resp = self.raw_request("GET", "/api/session", headers=[("Host", "localhost")])
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.error, "Bad request")

    def test_health_exempt_from_host_check(self):
        for host in ("evil.test", None):
            with self.subTest(host=host):
                resp = self.raw_request("GET", "/health", host=host)
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.body, b"ok")
                self.assertEqual(resp.headers["Content-Type"], "text/plain; charset=utf-8")
        resp = self.raw_request("HEAD", "/health", host="evil.test")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"")

    def test_health_with_query_is_not_exempt(self):
        self.assertEqual(self.raw_request("GET", "/health?x", host="evil.test").status, 421)
        resp = self.raw_request("GET", "/health?x")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.error, "Not found")


class MethodAndRoutingTests(ServerTestCase):
    def test_unhandled_methods(self):
        for method in ("OPTIONS", "PUT", "DELETE", "TRACE", "CONNECT", "FOO"):
            for path in ("/api/session", "/", "/api/nope"):
                with self.subTest(method=method, path=path):
                    resp = self.raw_request(method, path)
                    self.assertEqual(resp.status, 405)
                    self.assertEqual(resp.error, "Method not allowed")

    def test_known_api_path_wrong_method(self):
        self.assertEqual(self.request("GET", "/api/login").status, 405)
        self.assertEqual(self.request("PATCH", "/api/login", {}).status, 405)
        self.assertEqual(self.request("POST", "/api/session", {}).status, 405)
        self.assertEqual(self.request("HEAD", "/api/login").status, 405)

    def test_unknown_api_paths(self):
        for path in ("/api/nope", "/api/session/", "/api/", "/api/Session", "/api/%73ession",
                     "/api/account/", "/api//session"):
            with self.subTest(path=path):
                resp = self.request("GET", path)
                self.assertEqual(resp.status, 404)
                self.assertEqual(resp.error, "Not found")

    def test_query_string_ignored_for_routing(self):
        self.assertEqual(self.request("GET", "/api/session?x=1").status, 200)

    def test_post_to_non_api_path(self):
        for path in ("/", "/index.html", "/nothing"):
            with self.subTest(path=path):
                resp = self.request("POST", path, {})
                self.assertEqual(resp.status, 405)

    def test_post_to_non_api_path_does_not_read_body(self):
        # Content-Length promises a body that never comes: the 405 must arrive anyway.
        resp = self.raw_request("POST", "/", headers=self.post_headers(100))
        self.assertEqual(resp.status, 405)

    def test_head_on_get_route(self):
        resp = self.request("HEAD", "/api/session")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"")
        self.assertGreater(int(resp.headers["Content-Length"]), 0)

    def test_transfer_encoding_rejected(self):
        resp = self.raw_request("POST", "/api/login", headers=[
            ("Origin", self.origin), ("Content-Type", "application/json"), ("Transfer-Encoding", "chunked")],
            body=b"2\r\n{}\r\n0\r\n\r\n")
        self.assertEqual(resp.status, 411)
        self.assertEqual(resp.error, "Content-Length required")
        self.assertEqual(self.raw_request("GET", "/", headers=[("Transfer-Encoding", "gzip")]).status, 411)


class OriginTests(ServerTestCase):
    def post(self, origin):
        return self.request("POST", "/api/logout", {}, origin=origin)

    def test_rejected_origins(self):
        for origin in (None, "null", "http://evil.test", f"http://127.0.0.1:{self.port + 1}",
                       "http://127.0.0.1", f"ftp://127.0.0.1:{self.port}", f"http://127.0.0.1:{self.port}/",
                       f"http://localhost:{self.port}"):
            with self.subTest(origin=origin):
                resp = self.post(origin)
                self.assertEqual(resp.status, 403)
                self.assertEqual(resp.error, "Cross-origin request blocked")

    def test_accepted_origins(self):
        for origin in (f"http://127.0.0.1:{self.port}", f"https://127.0.0.1:{self.port}",
                       f"HTTP://127.0.0.1:{self.port}"):
            with self.subTest(origin=origin):
                self.assertEqual(self.post(origin).status, 200)

    def test_duplicate_origin(self):
        resp = self.raw_request("POST", "/api/logout", headers=[("Origin", self.origin)] + self.post_headers(2),
                                body=b"{}")
        self.assertEqual(resp.status, 400)

    def test_patch_needs_origin(self):
        self.assertEqual(self.request("PATCH", "/api/account", {}, origin=None).status, 403)


class BodyTests(ServerTestCase):
    def test_wrong_content_type(self):
        for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data",
                      "application/json-patch+json", None):
            with self.subTest(ctype=ctype):
                resp = self.request("POST", "/api/logout", raw_body=b"{}", content_type=ctype)
                self.assertEqual(resp.status, 415)
                self.assertEqual(resp.error, "Content-Type must be application/json")

    def test_content_type_with_parameters(self):
        for ctype in ("application/json; charset=utf-8", "Application/JSON", " application/json ;x=y"):
            with self.subTest(ctype=ctype):
                self.assertEqual(self.request("POST", "/api/logout", {}, content_type=ctype).status, 200)

    def test_missing_content_length(self):
        resp = self.raw_request("POST", "/api/logout", headers=[
            ("Origin", self.origin), ("Content-Type", "application/json")])
        self.assertEqual(resp.status, 411)
        self.assertEqual(resp.error, "Content-Length required")

    def test_invalid_content_length(self):
        for value in ("-1", "+5", "1_0", "abc", "1.0", "0x10", "123456", " ", "1 0"):
            with self.subTest(value=value):
                resp = self.raw_request("POST", "/api/logout", headers=[
                    ("Origin", self.origin), ("Content-Type", "application/json"), ("Content-Length", value)])
                self.assertEqual(resp.status, 400)
                self.assertEqual(resp.error, "Invalid Content-Length")

    def test_duplicate_content_length(self):
        resp = self.raw_request("POST", "/api/logout",
                                headers=self.post_headers(2) + [("Content-Length", "2")], body=b"{}")
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.error, "Bad request")

    def test_duplicate_content_type(self):
        resp = self.raw_request("POST", "/api/logout",
                                headers=self.post_headers(2) + [("Content-Type", "application/json")], body=b"{}")
        self.assertEqual(resp.status, 400)

    def test_too_large(self):
        resp = self.raw_request("POST", "/api/logout", headers=self.post_headers(16385))
        self.assertEqual(resp.status, 413)
        self.assertEqual(resp.error, "Request too large")
        self.assertEqual(self.raw_request("POST", "/api/logout", headers=self.post_headers(99999)).status, 413)

    def test_max_size_accepted(self):
        body = json.dumps({"pad": "x" * (16384 - 11)}).encode()
        self.assertEqual(len(body), 16384)
        self.assertEqual(self.request("POST", "/api/logout", raw_body=body).status, 200)

    def test_empty_body_is_empty_object(self):
        resp = self.request("POST", "/api/logout", raw_body=b"")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), {"ok": True})

    def test_invalid_json(self):
        cases = [
            b"[" * 16384,
            b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}',
            b"[]", b'"x"', b"1", b"null", b"true",
            b'{"a": "\xff"}', b"\xef\xbb\xbf{}",
            b"{", b"{'a': 1}", b'{"a": 1} x',
            b'{"a": ' + b"9" * 5000 + b"}",
        ]
        for raw in cases:
            with self.subTest(raw=raw[:30]):
                resp = self.request("POST", "/api/logout", raw_body=raw)
                self.assertEqual(resp.status, 400)
                self.assertEqual(resp.error, "Invalid JSON")

    def test_unknown_fields_ignored_and_server_fields_not_read(self):
        resp = self.request("POST", "/api/setup", {
            "username": "admin", "display_name": "Admin", "password": "correct horse battery",
            "id": 99, "is_admin": False, "created_at": 0, "extra": [1, 2]})
        self.assertEqual(resp.status, 201)
        self.assertEqual(resp.json()["user"], {"id": 1, "username": "admin", "display_name": "Admin",
                                               "is_admin": True, "must_change_password": False,
                                               "avatar_seed": self.seed_of(1), "photo": None})

    def test_get_with_body(self):
        for value in ("5", "abc", "00", "-0"):
            with self.subTest(value=value):
                resp = self.raw_request("GET", "/api/session", headers=[("Content-Length", value)])
                self.assertEqual(resp.status, 400)
                self.assertEqual(resp.error, "Bad request")
        self.assertEqual(self.raw_request("GET", "/", headers=[("Content-Length", "5")]).status, 400)
        self.assertEqual(self.raw_request("HEAD", "/", headers=[("Content-Length", "1")]).status, 400)
        self.assertEqual(self.raw_request("GET", "/api/session", headers=[("Content-Length", "0")]).status, 200)


class StaticTests(ServerTestCase):
    def test_static_files(self):
        cases = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        for path, (name, ctype) in cases.items():
            with self.subTest(path=path):
                resp = self.request("GET", path)
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.body, STATIC_CONTENT[name])
                self.assertEqual(resp.headers["Content-Type"], ctype)
                self.assertEqual(resp.headers["Cache-Control"], "no-cache")

    def test_static_query_string(self):
        self.assertEqual(self.request("GET", "/app.js?v=2").status, 200)

    def test_head_static(self):
        resp = self.request("HEAD", "/app.js")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"")
        self.assertEqual(resp.headers["Content-Length"], str(len(STATIC_CONTENT["app.js"])))

    def test_not_in_map(self):
        for path in ("/../server.py", "/%2e%2e/server.py", "/static/", "/index.html/", "//app.js",
                     "/server.py", "/static/index.html", "/app.js/", "/APP.JS", "/%61pp.js", "/favicon.ico",
                     "/.", "/api", "/health/", "/app.js%00", "/..%2fserver.py"):
            with self.subTest(path=path):
                resp = self.raw_request("GET", path)
                self.assertEqual(resp.status, 404)
                self.assertEqual(resp.error, "Not found")
                self.assertEqual(resp.headers["Content-Type"], "application/json; charset=utf-8")


class HeaderTests(ServerTestCase):
    def assertSecurityHeaders(self, resp):
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(resp.headers.get_all(name), [value], name)
        self.assertEqual(resp.headers["Server"], "WorldEats")
        for name in resp.headers.keys():
            self.assertFalse(name.lower().startswith("access-control-"), name)

    def test_api_response(self):
        resp = self.request("GET", "/api/session")
        self.assertSecurityHeaders(resp)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        self.assertEqual(resp.headers["Content-Type"], "application/json; charset=utf-8")

    def test_api_error_responses_no_store(self):
        for resp in (self.request("GET", "/api/nope"), self.request("POST", "/api/login", {}, origin=None),
                     self.request("GET", "/api/login")):
            self.assertSecurityHeaders(resp)
            self.assertEqual(resp.headers["Cache-Control"], "no-store")

    def test_static_response(self):
        self.assertSecurityHeaders(self.request("GET", "/"))

    def test_health(self):
        self.assertSecurityHeaders(self.request("GET", "/health"))

    def test_not_found(self):
        self.assertSecurityHeaders(self.request("GET", "/nope"))

    def test_unknown_host(self):
        resp = self.raw_request("GET", "/", host="evil.test")
        self.assertEqual(resp.status, 421)
        self.assertSecurityHeaders(resp)

    def test_stdlib_errors_are_json(self):
        cases = [
            b"GARBAGE\r\n\r\n",
            b"GET / HTTP/1.0 extra\r\n\r\n",
            b"GET / FTP/1.0\r\n\r\n",
            b"GET /\r\n\r\n",  # HTTP/0.9
            b"GET / HTTP/2.0\r\n\r\n",
            # Over the header budget; sized so the server reads everything sent (no reset on close).
            b"GET /" + b"a" * (16385 - 5),
            b"GET / HTTP/1.0\r\n" + b"".join(b"X-%d: 1\r\n" % i for i in range(51)),
            b"GET / HTTP/1.0\r\n" + b"X-Long: " + b"a" * (16385 - 16 - 8),
        ]
        for raw in cases:
            with self.subTest(raw=raw[:40]):
                resp = self.raw(raw)
                self.assertGreaterEqual(resp.status, 400)
                self.assertSecurityHeaders(resp)
                self.assertEqual(resp.headers["Content-Type"], "application/json; charset=utf-8")
                error = json.loads(resp.body)["error"]
                self.assertIsInstance(error, str)
                self.assertNotIn("GARBAGE", error)
                self.assertNotIn("<", resp.body.decode())

    def test_server_header_exact(self):
        resp = self.raw_request("GET", "/api/session")
        self.assertEqual(resp.headers.get_all("Server"), ["WorldEats"])

    def test_one_request_per_connection(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            req = f"GET /api/session HTTP/1.1\r\nHost: {self.host}\r\nConnection: keep-alive\r\n\r\n".encode()
            sock.sendall(req + req)
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        self.assertTrue(data.startswith(b"HTTP/1.0 200"))
        self.assertEqual(data.count(b"HTTP/1.0 "), 1)


class CatchAllTests(ServerTestCase):
    def test_handler_exception(self):
        def boom(req):
            raise RuntimeError("secret detail in /app/server.py SELECT * FROM users")
        self.add_route("/api/boom", {"GET": server.Route(boom, public=True)})
        resp = self.request("GET", "/api/boom")
        self.assertEqual(resp.status, 500)
        self.assertEqual(resp.json(), {"error": "Internal error", "code": "internal_error"})
        for text in ("Traceback", "secret", "server.py", "SELECT"):
            self.assertNotIn(text, resp.body.decode())
        self.assertIn("Traceback", self.log.getvalue())
        self.assertIn("secret detail", self.log.getvalue())
        self.assertEqual(resp.headers["X-Frame-Options"], "DENY")

    def test_server_keeps_working_after_exception(self):
        self.add_route("/api/boom", {"GET": server.Route(lambda req: 1 / 0, public=True)})
        self.assertEqual(self.request("GET", "/api/boom").status, 500)
        self.assertEqual(self.request("GET", "/api/session").status, 200)


class LoggingTests(ServerTestCase):
    def test_login_does_not_log_secrets(self):
        token = self.setup_admin(password="hunter2hunter2-secret")
        resp = self.login("admin", "hunter2hunter2-secret")
        new_token = resp.set_cookies(self.cookie_name)[0].split(";")[0].split("=", 1)[1]
        self.request("GET", "/api/session", token=new_token)
        self.login("admin", "wrong-password-value")
        log = self.log.getvalue()
        for secret in ("hunter2hunter2-secret", "wrong-password-value", token, new_token, "Cookie", "Set-Cookie"):
            self.assertNotIn(secret, log)
        self.assertIn("POST /api/login 200", log)
        self.assertIn("POST /api/login 401", log)
        self.assertIn("Setup completed by admin", log)

    def test_query_string_not_logged(self):
        self.request("GET", "/api/session?password=secret")
        self.request("GET", "/nothing?token=secret")
        log = self.log.getvalue()
        self.assertNotIn("secret", log)
        self.assertNotIn("?", log)
        self.assertIn("GET /api/session 200", log)
        self.assertIn("GET /nothing 404", log)

    def test_request_line_not_logged_on_stdlib_error(self):
        self.raw(b"GET /x?password=secret HTTP/9.9.9\r\n\r\n")
        self.raw(b"GARBAGE-secret\r\n\r\n")
        self.assertNotIn("secret", self.log.getvalue())

    def test_health_not_logged(self):
        self.request("GET", "/health")
        self.request("HEAD", "/health")
        self.assertNotIn("health", self.log.getvalue())

    def test_control_characters_escaped(self):
        self.raw(b"GET /a\x1b[31mred\x07 HTTP/1.0\r\nHost: localhost\r\n\r\n")
        log = self.log.getvalue()
        self.assertNotIn("\x1b", log)
        self.assertIn("GET /a\\x1b[31mred\\x07 404", log)

    def test_setup_required_logged_at_startup(self):
        self.assertIn("Setup required: no accounts exist", self.log.getvalue())


class ConnectionLimitTests(ServerTestCase):
    app_kwargs = {"max_connections": 2}

    def test_overflow_connection_closed_immediately(self):
        idle = [socket.create_connection(("127.0.0.1", self.port), timeout=5) for _ in range(2)]
        try:
            time.sleep(0.3)
            with socket.create_connection(("127.0.0.1", self.port), timeout=5) as extra:
                start = time.monotonic()
                try:
                    data = extra.recv(1024)
                except ConnectionResetError:
                    data = b""
                self.assertEqual(data, b"")
                self.assertLess(time.monotonic() - start, 3)
        finally:
            for sock in idle:
                sock.close()
        time.sleep(0.3)
        self.assertEqual(self.request("GET", "/api/session").status, 200)


class BodyDeadlineTests(ServerTestCase):
    app_kwargs = {"body_deadline": 1.5}

    def test_slow_body_closed_without_response(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            head = (f"POST /api/setup HTTP/1.0\r\nHost: {self.host}\r\nOrigin: {self.origin}\r\n"
                    "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n")
            sock.sendall(head.encode() + b'{"username"')
            start = time.monotonic()
            data = b""
            try:
                while True:
                    try:
                        chunk = sock.recv(1024)
                    except socket.timeout:
                        sock.sendall(b" ")
                        continue
                    if not chunk:
                        break
                    data += chunk
            except (BrokenPipeError, ConnectionResetError):
                pass
            elapsed = time.monotonic() - start
        self.assertEqual(data, b"")
        self.assertLess(elapsed, 6)

    def test_short_body_runs_no_handler(self):
        body = json.dumps({"username": "admin", "display_name": "A", "password": "correct horse battery"})
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            head = (f"POST /api/setup HTTP/1.0\r\nHost: {self.host}\r\nOrigin: {self.origin}\r\n"
                    f"Content-Type: application/json\r\nContent-Length: {len(body) + 10}\r\n\r\n")
            sock.sendall(head.encode() + body.encode())
            sock.shutdown(socket.SHUT_WR)
            self.assertEqual(sock.recv(1024), b"")
        self.assertTrue(self.request("GET", "/api/session").json()["setup_required"])


class HeaderDeadlineTests(ServerTestCase):
    def test_trickling_headers_disconnected(self):
        """A client sending one header byte a second (inside the 15 s read timeout) is cut off at ~20 s."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as sock:
            start = time.monotonic()
            sock.sendall(f"GET / HTTP/1.0\r\nHost: {self.host}\r\nX-Slow: ".encode())
            closed_at = None
            while time.monotonic() - start < 30:
                try:
                    sock.sendall(b"a")
                    sock.settimeout(1)
                    if sock.recv(1024) == b"":
                        closed_at = time.monotonic()
                        break
                except socket.timeout:
                    continue
                except OSError:
                    closed_at = time.monotonic()
                    break
        self.assertIsNotNone(closed_at, "connection was never closed")
        self.assertGreaterEqual(closed_at - start, 19)
        self.assertLessEqual(closed_at - start, 22.5)
        self.assertEqual(self.request("GET", "/api/session").status, 200)


class HeaderBudgetTests(ServerTestCase):
    """The request line and headers share a 16384-byte budget and at most 50 header lines."""

    def head_of_size(self, size):
        start = b"GET /api/session HTTP/1.0\r\nHost: localhost\r\nX-Pad: "
        end = b"\r\n\r\n"
        return start + b"a" * (size - len(start) - len(end)) + end

    def assertTooLarge(self, resp):
        self.assertEqual(resp.status, 431)
        self.assertEqual(resp.json(), {"error": "Request headers too large", "code": "headers_too_large"})
        self.assertEqual(resp.headers["X-Frame-Options"], "DENY")

    def test_exactly_at_budget(self):
        head = self.head_of_size(16384)
        self.assertEqual(len(head), 16384)
        self.assertEqual(self.raw(head).status, 200)

    def test_one_byte_over_budget(self):
        self.assertTooLarge(self.raw(self.head_of_size(16385)))

    def test_fifty_header_lines(self):
        headers = [("Host", "localhost")] + [(f"X-{i}", "1") for i in range(49)]
        lines = "".join(f"{k}: {v}\r\n" for k, v in headers)
        self.assertEqual(self.raw(f"GET /api/session HTTP/1.0\r\n{lines}\r\n".encode()).status, 200)

    def test_fifty_one_header_lines(self):
        headers = [("Host", "localhost")] + [(f"X-{i}", "1") for i in range(50)]
        lines = "".join(f"{k}: {v}\r\n" for k, v in headers)
        self.assertTooLarge(self.raw(f"GET /api/session HTTP/1.0\r\n{lines}\r\n".encode()))

    def test_huge_header_line(self):
        self.assertTooLarge(self.raw(b"GET / HTTP/1.0\r\nX-Big: " + b"a" * 16400))

    def test_huge_request_line(self):
        self.assertTooLarge(self.raw(b"GET /" + b"a" * 16400))

    def test_body_not_counted(self):
        token = self.setup_admin()
        body = ('{"display_name": "Big", "pad": "' + "x" * 16000 + '"}').encode()
        head = ("PATCH /api/account HTTP/1.0\r\nHost: localhost\r\nOrigin: http://localhost\r\n"
                f"Content-Type: application/json\r\nCookie: we_session={token}\r\n"
                f"Content-Length: {len(body)}\r\n\r\n").encode()
        resp = self.raw(head + body)
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(resp.json()["user"]["display_name"], "Big")


class ReaperMarkTests(ServerTestCase):
    app_kwargs = {"request_deadline": 1.0}

    def test_cut_request_is_not_dispatched(self):
        token = self.setup_admin()
        with self.db() as conn:
            before = conn.execute("SELECT last_used_at FROM sessions").fetchone()[0]
        self.clock_offset = 7200  # a dispatched request would renew the session
        self.log.truncate(0)
        self.log.seek(0)
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(f"GET /api/session HTTP/1.0\r\nHost: {self.host}\r\nCookie: we_session={token}\r\n".encode())
            start = time.monotonic()
            self.assertEqual(sock.recv(1024), b"")
            self.assertLess(time.monotonic() - start, 4)
        time.sleep(0.2)
        self.assertNotIn("/api/session", self.log.getvalue())
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT last_used_at FROM sessions").fetchone()[0], before)

    def test_reaped_socket_marked(self):
        reaper = server.Reaper(0)
        a, b = socket.socketpair()
        try:
            reaper.track(a)
            reaper.reap_once()
            self.assertTrue(reaper.is_reaped(a))
            self.assertFalse(reaper.finish_reading(a))
            self.assertEqual(a.recv(10), b"")  # shut down
            reaper.forget(a)
            self.assertFalse(reaper.is_reaped(a))
        finally:
            a.close()
            b.close()

    def test_finished_socket_not_reaped(self):
        reaper = server.Reaper(0)
        a, b = socket.socketpair()
        try:
            reaper.track(a)
            self.assertTrue(reaper.finish_reading(a))
            reaper.reap_once()
            self.assertFalse(reaper.is_reaped(a))
        finally:
            a.close()
            b.close()


class RouteOptionTests(ServerTestCase):
    def test_routes_need_login_by_default(self):
        called = []

        def handler(req):
            called.append(True)
            return 200, {"ok": True}

        self.add_route("/api/private", {"GET": server.Route(handler)})
        resp = self.request("GET", "/api/private")
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.error, "Not logged in")
        self.assertEqual(called, [])
        token = self.setup_admin()
        self.assertEqual(self.request("GET", "/api/private", token=token).status, 200)
        self.assertEqual(called, [True])

    def test_only_the_four_public_routes(self):
        public = {pattern.pattern for pattern, methods in server.ROUTES
                  for route in methods.values() if route.public}
        self.assertEqual(public, {"/api/session", "/api/setup", "/api/login", "/api/logout"})
        no_renew = {pattern.pattern for pattern, methods in server.ROUTES
                    for route in methods.values() if not route.renew}
        # Change 11d: image loads don't renew (no session writes).
        self.assertEqual(no_renew, {"/api/setup", "/api/login", "/api/logout", "/api/photos/([A-Za-z0-9_-]{43})"})

    def test_protected_endpoints_without_login(self):
        for method, path in (("PATCH", "/api/account"), ("POST", "/api/account/password"),
                             ("POST", "/api/account/logout-everywhere"), ("POST", "/api/account/delete")):
            with self.subTest(path=path):
                resp = self.request(method, path, {})
                self.assertEqual(resp.status, 401)
                self.assertEqual(resp.error, "Not logged in")

    def test_session_user_has_no_password_hash(self):
        seen = {}

        def handler(req):
            seen["keys"] = sorted(req.user.keys())
            return 200, {}

        self.add_route("/api/whoami", {"GET": server.Route(handler)})
        token = self.setup_admin()
        self.assertEqual(self.request("GET", "/api/whoami", token=token).status, 200)
        self.assertNotIn("password_hash", seen["keys"])
        self.assertEqual(seen["keys"], ["avatar_seed", "display_name", "id", "is_admin", "last_used_at",
                                        "must_change_password", "photo_token", "session_id", "username"])
