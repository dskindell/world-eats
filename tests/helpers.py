"""Shared test helper: starts the real server in-process and talks to it over HTTP.

Each test gets its own server, temporary DATA_DIR and static directory, so tests
don't share data or lockout state. The server uses a cheap scrypt cost so the
suite stays fast; test_hashing checks the real parameters.
"""

import hashlib
import http.client
import io
import json
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import auth  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import passports  # noqa: E402
import server  # noqa: E402

PASSWORD = "correct horse battery"
STATIC_CONTENT = {
    "index.html": b"<!doctype html><title>World Eats test</title>",
    "app.js": b"console.log('test');",
    "style.css": b"body { color: white; }",
}
SECURITY_HEADERS = dict(server.SECURITY_HEADERS)

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fast_hasher():
    return auth.PasswordHasher(n=1024, r=8, p=1)


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers  # http.client.HTTPMessage
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    @property
    def error(self):
        return self.json().get("error")

    def set_cookies(self, name):
        return [v for v in self.headers.get_all("Set-Cookie") or [] if v.startswith(name + "=")]

    def __repr__(self):
        return f"<Response {self.status} {self.body[:200]!r}>"


def cookie_value(set_cookie):
    return set_cookie.split(";", 1)[0].split("=", 1)[1]


def cookie_attributes(set_cookie):
    return [part.strip() for part in set_cookie.split(";")[1:]]


class ServerTestCase(unittest.TestCase):
    env = {}  # extra environment for load_config, per test class
    app_kwargs = {}  # extra App keyword arguments, per test class

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="we-data-")
        self.static_dir = tempfile.mkdtemp(prefix="we-static-")
        for name, content in STATIC_CONTENT.items():
            Path(self.static_dir, name).write_bytes(content)
        # A fixed fake clock: time only moves when a test sets clock_offset.
        self.base_time = 1_800_000_000
        self.clock_offset = 0
        self.log = io.StringIO()
        env = {"WORLD_EATS_ALLOWED_HOSTS": "127.0.0.1,localhost", "DATA_DIR": self.data_dir}
        env.update(self.env)
        self.config = config.load_config(env)
        self.prepare_data()
        self.app = server.App(
            self.config, address=("127.0.0.1", 0), static_dir=self.static_dir, hasher=fast_hasher(),
            clock=lambda: self.base_time + self.clock_offset, log_stream=self.log, **self.app_kwargs)
        self.thread = threading.Thread(target=self.app.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()
        self.port = self.app.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        self.origin = f"http://{self.host}"
        self.cookie_name = self.config.cookie_name

    def prepare_data(self):
        """Override to put files in self.data_dir before the server starts (e.g. an old schema)."""

    def tearDown(self):
        self.app.shutdown()
        self.app.server_close()
        self.thread.join(5)
        shutil.rmtree(self.data_dir, ignore_errors=True)
        shutil.rmtree(self.static_dir, ignore_errors=True)

    # --- HTTP ---

    def request(self, method, path, body=None, *, raw_body=None, token=None, cookie=None,
                origin="default", content_type="application/json", headers=None):
        """A normal client request through urllib. `body` is JSON-encoded; `raw_body` is sent as is."""
        data = raw_body if raw_body is not None else (
            json.dumps(body).encode("utf-8") if body is not None else None)
        if data is None and method in ("POST", "PATCH"):
            data = b""
        req = urllib.request.Request(f"http://{self.host}{path}", data=data, method=method)
        if method in ("POST", "PATCH"):
            if origin == "default":
                origin = self.origin
            if origin is not None:
                req.add_header("Origin", origin)
            if content_type is not None:
                req.add_header("Content-Type", content_type)
        if token is not None:
            req.add_header("Cookie", f"{self.cookie_name}={token}")
        if cookie is not None:
            req.add_header("Cookie", cookie)
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with _opener.open(req, timeout=20) as resp:
                return Response(resp.status, resp.headers, resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return Response(e.code, e.headers, e.read())

    def raw(self, data, timeout=10):
        """Sends raw bytes and reads the reply until the server closes the connection."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as sock:
            sock.sendall(data)
            chunks = []
            while True:
                try:
                    chunk = sock.recv(65536)
                except ConnectionResetError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        return parse_response(b"".join(chunks))

    def raw_request(self, method, path, headers=(), body=b"", host="default"):
        """Builds an HTTP/1.0 request by hand. `headers` is a list of (name, value) pairs."""
        lines = [f"{method} {path} HTTP/1.0"]
        if host == "default":
            host = self.host
        if host is not None:
            lines.append(f"Host: {host}")
        lines += [f"{name}: {value}" for name, value in headers]
        return self.raw(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)

    def add_route(self, path, methods):
        """Adds an exact-path test route ahead of the real ones."""
        self.app.routes.insert(0, (re.compile(re.escape(path)), methods))

    def post_headers(self, length, content_type="application/json"):
        return [("Origin", self.origin), ("Content-Type", content_type), ("Content-Length", str(length))]

    # --- Accounts and sessions ---

    def setup_admin(self, username="admin", password=PASSWORD, display_name="Admin"):
        resp = self.request("POST", "/api/setup", {
            "username": username, "display_name": display_name, "password": password})
        self.assertEqual(resp.status, 201, resp)
        return cookie_value(resp.set_cookies(self.cookie_name)[0])

    def login(self, username="admin", password=PASSWORD, **kwargs):
        return self.request("POST", "/api/login", {"username": username, "password": password}, **kwargs)

    def login_token(self, username="admin", password=PASSWORD):
        resp = self.login(username, password)
        self.assertEqual(resp.status, 200, resp)
        return cookie_value(resp.set_cookies(self.cookie_name)[0])

    def session_user(self, token):
        return self.request("GET", "/api/session", token=token).json()["user"]

    # --- Direct database access (test seam) ---

    def db(self):
        conn = sqlite3.connect(self.app.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def insert_user(self, username, password=PASSWORD, is_admin=False, display_name=None):
        """A user with their own password and, like every account, a personal passport."""
        password_hash = self.app.hasher.hash(password)
        with self.db() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, display_name, password_hash, is_admin, created_at, avatar_seed) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (username, display_name or username, password_hash, int(is_admin), self.base_time,
                 db.new_avatar_seed()))
            passports.create_personal(conn, cur.lastrowid, self.base_time)
        return cur.lastrowid

    def delete_user_rows(self, *user_ids):
        """Deletes users straight from the database, with the passports they own (test seam)."""
        with self.db() as conn:
            for user_id in user_ids:
                conn.execute("DELETE FROM passports WHERE owner_id = ?", (user_id,))
                conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def seed_of(self, user_id):
        """The user's stored avatar seed."""
        with self.db() as conn:
            return conn.execute("SELECT avatar_seed FROM users WHERE id = ?", (user_id,)).fetchone()[0]

    def sessions_for(self, user_id):
        with self.db() as conn:
            return conn.execute("SELECT * FROM sessions WHERE user_id = ?", (user_id,)).fetchall()


def parse_response(data):
    head, _, body = data.partition(b"\r\n\r\n")
    status_line, _, header_block = head.partition(b"\r\n")
    parts = status_line.split(b" ", 2)
    status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
    headers = http.client.parse_headers(io.BytesIO(header_block + b"\r\n\r\n"))
    return Response(status, headers, body)


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CountingSemaphore:
    """Wraps the hashing semaphore to count acquisitions."""

    def __init__(self, inner):
        self.inner = inner
        self.acquired = 0
        self.lock = threading.Lock()

    def acquire(self, *args, **kwargs):
        with self.lock:
            self.acquired += 1
        return self.inner.acquire(*args, **kwargs)

    def release(self):
        self.inner.release()
