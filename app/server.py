"""World Eats: pick a random country to eat from.

Stdlib only (http.server + sqlite3). Feature 1a: first-visit setup, login,
sessions and account settings; feature 1b: admin screen. See
.claude/specs/1a-login-basics.md and 1b-admin-screen.md.

Patterns for later features:
- ROUTES is an ordered list of (compiled regex, {method: Route(handler, ...options)}),
  matched with fullmatch against the raw path minus the query string; first
  match wins. Build literal parts with re.escape and ids with ID (ASCII digits
  only: never \\d, which matches other scripts' digits). A handler takes the
  request handler object plus the regex groups as strings (int() them after the
  match) and returns (status, json-able object).
- Routes need a logged-in user unless marked public=True. For a non-public route
  the dispatcher checks, in order: no session -> 401; must_change_password and
  not allow_pending -> 403; admin=True and not an admin -> 403. The handler can
  rely on req.user.
- Raise ApiError(status, message) for any client-facing error; it becomes
  JSON {"error": message}. Field validators in auth raise auth.Invalid(message),
  which becomes a 400; call them in the contract's field order.
- Admin writes call require_admin() first inside their write transaction (the
  admin may have been demoted, disabled or logged out since the session loaded).
- One SQLite connection per request (req.db()). Only top-level handlers open a
  write transaction, with `with req.write():` (app write lock + BEGIN IMMEDIATE);
  helpers take `conn` and run inside the caller's transaction. Nesting raises.
- Authenticated writes after a slow check (password verification, anything that
  takes time between loading the session and writing): inside the write
  transaction, call require_live_session() so a session revoked meanwhile gets
  401, and make the write conditional on the state that was checked (e.g.
  UPDATE ... WHERE password_hash = <the verified hash>).
"""

import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, NamedTuple

import auth
import db
from config import ConfigError, load_config, normalise_ip, split_host_port

STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

SECURITY_HEADERS = (
    ("Content-Security-Policy",
     "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
     "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
     "form-action 'self'; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
)

HANDLED_METHODS = frozenset({"GET", "HEAD", "POST", "PATCH"})
SINGLE_HEADERS = ("Host", "Origin", "Content-Length", "Content-Type", "Cookie")
CONTENT_LENGTH_RE = re.compile(r"[0-9]{1,5}")
HOST_PORT_RE = re.compile(r"[0-9]{1,5}")

MAX_BODY = 16384
HEADER_BUDGET = 16384  # request line + header lines + blank line, in bytes
MAX_HEADER_LINES = 50
READ_TIMEOUT = 15
BODY_DEADLINE = 15
SESSION_TTL = 2592000  # 30 days
RENEW_AFTER = 3600
MAX_COOKIE_VALUE = 100
TEMP_PASSWORD_TTL = 604800  # 7 days

# Generic texts for errors from the stdlib's parse_request() (its own messages can
# echo the request) and for our own send_error() calls.
STDLIB_ERRORS = {
    400: "Bad request",
    431: "Request headers too large",
    505: "HTTP version not supported",
}

LOCKED_MSG = "Too many failed attempts. Try again in a few minutes."


class ApiError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


class _CloseConnection(Exception):
    """The request body couldn't be read in time: close without a response."""


# --- Endpoints (contract section 7) ---

class Route(NamedTuple):
    handler: Callable
    public: bool = False  # False: 401 before the handler runs unless logged in
    renew: bool = True  # renew the session's expiry when it's used
    admin: bool = False  # 403 unless the user is an admin
    allow_pending: bool = False  # works while the user must change their password


def user_json(row):
    return {"id": row["id"], "username": row["username"],
            "display_name": row["display_name"], "is_admin": bool(row["is_admin"]),
            "must_change_password": bool(row["must_change_password"])}


def temp_password_expired(row, now):
    """Contract 1b §2: an admin-set password that hasn't been changed within 7 days."""
    return (bool(row["must_change_password"]) and row["temp_password_set_at"] is not None
            and now - row["temp_password_set_at"] >= TEMP_PASSWORD_TTL)


def require_live_session(req, conn):
    """Inside a write transaction: 401 if this request's session was revoked meanwhile."""
    if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (req.session_id,)).fetchone() is None:
        raise ApiError(401, "Not logged in")


def require_admin(req, conn):
    """Contract 1b §4: the caller is still a live, enabled admin with no pending password change.

    First thing inside every admin write transaction, and as a read right before an unlock.
    """
    row = conn.execute(
        "SELECT users.is_admin, users.disabled, users.must_change_password "
        "FROM sessions JOIN users ON users.id = sessions.user_id "
        "WHERE sessions.id = ? AND sessions.expires_at > ?",
        (req.session_id, req.app.now())).fetchone()
    if row is None:
        raise ApiError(401, "Not logged in")
    if not is_usable_admin(row):
        raise ApiError(403, "Admins only")


def is_usable_admin(row):
    """An admin who can use the admin screen now: enabled, with no pending password change."""
    return row["is_admin"] == 1 and row["disabled"] == 0 and row["must_change_password"] == 0


def other_usable_admins(conn, user_id):
    # A pending admin doesn't count: their temporary password may expire before they ever log in.
    return conn.execute(
        "SELECT COUNT(*) FROM users "
        "WHERE is_admin = 1 AND disabled = 0 AND must_change_password = 0 AND id != ?",
        (user_id,)).fetchone()[0]


def is_last_usable_admin(conn, row):
    return is_usable_admin(row) and other_usable_admins(conn, row["id"]) == 0


def api_session(req):
    if not db.any_users(req.db()):
        return 200, {"setup_required": True, "user": None}
    return 200, {"setup_required": False, "user": user_json(req.user) if req.user else None}


def api_setup(req):
    username = auth.require_username(req.body.get("username"))
    display_name = auth.clean_display_name(req.body.get("display_name"))
    password = auth.require_new_password(req.body.get("password"))
    conn = req.db()
    if db.any_users(conn):
        raise ApiError(409, "Setup is already complete")
    password_hash = req.app.hasher.hash(password)
    now = req.app.now()
    with req.write():
        cur = conn.execute(
            "INSERT INTO users (username, display_name, password_hash, is_admin, created_at, last_active_at) "
            "SELECT ?, ?, ?, 1, ?, ? WHERE NOT EXISTS (SELECT 1 FROM users)",
            (username, display_name, password_hash, now, now))
        if cur.rowcount != 1:
            raise ApiError(409, "Setup is already complete")
        user_id = cur.lastrowid
        token = create_session(conn, user_id, now)
    req.issue_session(token)
    req.app.log(f"Setup completed by {username}")
    return 201, {"user": user_json({"id": user_id, "username": username, "display_name": display_name,
                                    "is_admin": 1, "must_change_password": 0})}


def api_login(req):
    conn = req.db()
    if not db.any_users(conn):
        raise ApiError(409, "Setup is required")
    username = req.body.get("username")
    password = req.body.get("password")
    user_key = username.lower() if auth.valid_username(username) else None
    ip = auth.ip_key(req.client_ip())
    lockout = req.app.lockout
    if not lockout.reserve(user_key, ip):
        raise ApiError(429, LOCKED_MSG)
    outcome = auth.NO_RESULT
    try:
        user = None
        if user_key is not None:
            user = conn.execute(
                "SELECT id, username, display_name, is_admin, must_change_password, disabled, "
                "temp_password_set_at, password_hash FROM users WHERE username = ?", (username,)).fetchone()
        if user is not None and auth.usable_password(password):
            ok = req.app.hasher.verify(password, user["password_hash"])
            # A disabled account or an expired temporary password is a wrong password,
            # after the same hashing work.
            ok = ok and not user["disabled"] and not temp_password_expired(user, req.app.now())
        else:
            ok = req.app.hasher.burn(password)
        if not ok:
            outcome = auth.FAILED
            raise ApiError(401, "Wrong username or password")
        now = req.app.now()
        with req.write():
            # An admin may have reset or disabled the account while the password was hashing.
            # Refused here, the attempt is NO_RESULT: no failure added, none cleared.
            current = conn.execute("SELECT password_hash, disabled FROM users WHERE id = ?",
                                   (user["id"],)).fetchone()
            if current is None or current["password_hash"] != user["password_hash"] or current["disabled"]:
                raise ApiError(401, "Wrong username or password")
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            token = create_session(conn, user["id"], now)
            conn.execute("UPDATE users SET last_active_at = ? WHERE id = ?", (now, user["id"]))
        outcome = auth.SUCCEEDED
    finally:
        lockout.finish(user_key, ip, outcome)
    req.issue_session(token)
    return 200, {"user": user_json(user)}


def api_logout(req):
    if req.session_id is not None:
        with req.write():
            req.db().execute("DELETE FROM sessions WHERE id = ?", (req.session_id,))
    req.clear_session_cookie()
    return 200, {"ok": True}


def api_update_account(req):
    if "display_name" not in req.body:
        raise ApiError(400, "Nothing to update")
    display_name = auth.clean_display_name(req.body["display_name"])
    with req.write():
        req.db().execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name, req.user["id"]))
    return 200, {"user": {**user_json(req.user), "display_name": display_name}}


def check_own_password(req, password, wrong_message):
    """Throttled check of the logged-in user's password, sharing the login username key.

    Returns the stored hash that was verified, for a conditional write afterwards.
    """
    row = req.db().execute(
        "SELECT password_hash, must_change_password, temp_password_set_at FROM users WHERE id = ?",
        (req.user["id"],)).fetchone()
    if row is None:
        raise ApiError(401, "Not logged in")
    key = req.user["username"].lower()
    lockout = req.app.lockout
    if not lockout.reserve(key, None):
        raise ApiError(429, LOCKED_MSG)
    outcome = auth.NO_RESULT
    try:
        if auth.usable_password(password):
            ok = req.app.hasher.verify(password, row["password_hash"])
            ok = ok and not temp_password_expired(row, req.app.now())
        else:
            ok = req.app.hasher.burn(password)
        outcome = auth.SUCCEEDED if ok else auth.FAILED
    finally:
        lockout.finish(key, None, outcome)
    if not ok:
        raise ApiError(403, wrong_message)
    return row["password_hash"]


def api_change_password(req):
    new_password = auth.require_new_password(req.body.get("new_password"))
    current_password = req.body.get("current_password")
    verified_hash = check_own_password(req, current_password, "Current password is wrong")
    # Both values come from the client, so this reveals nothing about the stored password;
    # it stops a user "changing" back to a temporary password the admin knows.
    if new_password == current_password:
        raise ApiError(400, "New password must be different from the current one")
    new_hash = req.app.hasher.hash(new_password)
    user_id = req.user["id"]
    with req.write():
        conn = req.db()
        require_live_session(req, conn)
        cur = conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0, temp_password_set_at = NULL "
            "WHERE id = ? AND password_hash = ?", (new_hash, user_id, verified_hash))
        if cur.rowcount != 1:
            raise ApiError(403, "Current password is wrong")
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND id != ?", (user_id, req.session_id))
    return 200, {"ok": True}


def api_logout_everywhere(req):
    with req.write():
        req.db().execute("DELETE FROM sessions WHERE user_id = ?", (req.user["id"],))
    req.clear_session_cookie()
    return 200, {"ok": True}


def api_delete_account(req):
    check_own_password(req, req.body.get("password"), "Password is wrong")
    user_id = req.user["id"]
    # Admin status and the admin count are read inside the transaction so two
    # admins deleting themselves at once can't both pass the last-admin check.
    with req.write():
        conn = req.db()
        require_live_session(req, conn)
        row = conn.execute("SELECT id, is_admin, disabled, must_change_password FROM users WHERE id = ?",
                           (user_id,)).fetchone()
        if row is not None and is_last_usable_admin(conn, row):
            raise ApiError(409, "You're the only admin, so this account can't be deleted")
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    req.clear_session_cookie()
    return 200, {"ok": True}


# --- Admin endpoints (contract 1b, section 6) ---

# Named columns for the admin user object (never SELECT *); fixed strings, no input.
ADMIN_USER_SELECT = ("SELECT id, username, display_name, is_admin, disabled, must_change_password, "
                     "temp_password_set_at, created_at, last_active_at FROM users")
FIND_USER_SQL = ADMIN_USER_SELECT + " WHERE id = ?"
LIST_USERS_SQL = ADMIN_USER_SELECT + " ORDER BY username COLLATE NOCASE"


def find_user(conn, user_id):
    return conn.execute(FIND_USER_SQL, (user_id,)).fetchone()


def require_user(conn, user_id):
    row = find_user(conn, user_id)
    if row is None:
        raise ApiError(404, "User not found")
    return row


def admin_user_json(req, row):
    return {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
            "is_admin": bool(row["is_admin"]), "disabled": bool(row["disabled"]),
            "locked": req.app.lockout.is_locked(row["username"].lower()),
            "must_change_password": bool(row["must_change_password"]),
            "temp_password_expired": temp_password_expired(row, req.app.now()),
            "created_at": row["created_at"], "last_active_at": row["last_active_at"]}


def log_admin_action(req, message):
    """`message` names the target with log_safe(); the admin's username is appended."""
    req.app.log(f"{message} by {log_safe(req.user['username'])}")


def api_admin_users(req):
    rows = req.db().execute(LIST_USERS_SQL).fetchall()
    return 200, {"users": [admin_user_json(req, row) for row in rows]}


def api_admin_create_user(req):
    username = auth.require_username(req.body.get("username"))
    display_name = auth.clean_display_name(req.body.get("display_name"))
    password = auth.require_new_password(req.body.get("password"))
    is_admin = req.body.get("is_admin", False)
    if type(is_admin) is not bool:
        raise ApiError(400, "is_admin must be true or false")
    conn = req.db()
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise ApiError(409, "That username is taken")
    password_hash = req.app.hasher.hash(password)
    now = req.app.now()
    with req.write():
        require_admin(req, conn)
        try:
            cur = conn.execute(
                "INSERT INTO users (username, display_name, password_hash, is_admin, created_at, disabled, "
                "must_change_password, temp_password_set_at, last_active_at) "
                "VALUES (?, ?, ?, ?, ?, 0, 1, ?, NULL)",
                (username, display_name, password_hash, int(is_admin), now, now))
        except sqlite3.IntegrityError:
            raise ApiError(409, "That username is taken") from None
        row = find_user(conn, cur.lastrowid)
    log_admin_action(req, f"User {log_safe(username)} created")
    return 201, {"user": admin_user_json(req, row)}


def api_admin_update_user(req, user_id):
    if "display_name" not in req.body:
        raise ApiError(400, "Nothing to update")
    display_name = auth.clean_display_name(req.body["display_name"])
    user_id = int(user_id)
    conn = req.db()
    with req.write():
        require_admin(req, conn)
        cur = conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name, user_id))
        if cur.rowcount != 1:
            raise ApiError(404, "User not found")
        row = find_user(conn, user_id)
    return 200, {"user": admin_user_json(req, row)}


def api_admin_reset_password(req, user_id):
    user_id = int(user_id)
    if user_id == req.user["id"]:
        raise ApiError(403, "Change your own password in Account settings")
    new_password = auth.require_new_password(req.body.get("new_password"))
    conn = req.db()
    require_user(conn, user_id)
    password_hash = req.app.hasher.hash(new_password)
    now = req.app.now()
    with req.write():
        require_admin(req, conn)
        cur = conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 1, temp_password_set_at = ? "
            "WHERE id = ?", (password_hash, now, user_id))
        if cur.rowcount != 1:
            raise ApiError(404, "User not found")
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        row = find_user(conn, user_id)
    req.app.lockout.clear_user(row["username"].lower())
    log_admin_action(req, f"Password reset for {log_safe(row['username'])}")
    return 200, {"user": admin_user_json(req, row)}


def api_admin_disable(req, user_id):
    user_id = int(user_id)
    if user_id == req.user["id"]:
        raise ApiError(403, "You can't disable your own account")
    conn = req.db()
    with req.write():
        require_admin(req, conn)
        row = require_user(conn, user_id)
        if is_last_usable_admin(conn, row):
            raise ApiError(409, "This is the last admin, so it can't be disabled")
        conn.execute("UPDATE users SET disabled = 1 WHERE id = ?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        row = find_user(conn, user_id)
    log_admin_action(req, f"User {log_safe(row['username'])} disabled")
    return 200, {"user": admin_user_json(req, row)}


def set_user_flag(req, user_id, sql):
    """Enable and promote: a flag change with no extra rules. `sql` is a fixed UPDATE ... WHERE id = ?."""
    conn = req.db()
    with req.write():
        require_admin(req, conn)
        require_user(conn, user_id)
        conn.execute(sql, (user_id,))
        return find_user(conn, user_id)


def api_admin_enable(req, user_id):
    row = set_user_flag(req, int(user_id), "UPDATE users SET disabled = 0 WHERE id = ?")
    log_admin_action(req, f"User {log_safe(row['username'])} enabled")
    return 200, {"user": admin_user_json(req, row)}


def api_admin_promote(req, user_id):
    row = set_user_flag(req, int(user_id), "UPDATE users SET is_admin = 1 WHERE id = ?")
    log_admin_action(req, f"User {log_safe(row['username'])} promoted")
    return 200, {"user": admin_user_json(req, row)}


def api_admin_demote(req, user_id):
    user_id = int(user_id)
    conn = req.db()
    with req.write():
        require_admin(req, conn)
        row = require_user(conn, user_id)
        if is_last_usable_admin(conn, row):
            raise ApiError(409, "You're the only admin, so you can't remove your own admin rights")
        conn.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (user_id,))
        row = find_user(conn, user_id)
    log_admin_action(req, f"User {log_safe(row['username'])} demoted")
    return 200, {"user": admin_user_json(req, row)}


def api_admin_unlock(req, user_id):
    conn = req.db()
    row = require_user(conn, int(user_id))
    # The re-check is a plain read (unlock writes no rows), run immediately before the
    # clear as the contract asks, so it comes after the 404 lookup rather than before it.
    require_admin(req, conn)
    req.app.lockout.clear_user(row["username"].lower())
    return 200, {"user": admin_user_json(req, row)}


def api_admin_delete(req, user_id):
    user_id = int(user_id)
    if user_id == req.user["id"]:
        raise ApiError(403, "Delete your own account in Account settings")
    confirm = req.body.get("confirm_username")
    conn = req.db()
    with req.write():
        require_admin(req, conn)
        row = require_user(conn, user_id)
        if not isinstance(confirm, str) or confirm.lower() != row["username"].lower():
            raise ApiError(400, "Type the username to confirm")
        if is_last_usable_admin(conn, row):
            raise ApiError(409, "This is the last admin, so it can't be deleted")
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    req.app.lockout.clear_user(row["username"].lower())
    log_admin_action(req, f"User {log_safe(row['username'])} deleted")
    return 200, {"ok": True}


def literal(path):
    return re.escape(path)


ID = "([1-9][0-9]{0,9})"  # ASCII digits only; int() is applied after the match
ADMIN_USER = literal("/api/admin/users/") + ID


def admin_route(handler):
    return Route(handler, admin=True)


# Setup and login ignore any incoming session, and logout ends it, so none renew.
ROUTES = [(re.compile(pattern), methods) for pattern, methods in [
    (literal("/api/session"), {"GET": Route(api_session, public=True)}),
    (literal("/api/setup"), {"POST": Route(api_setup, public=True, renew=False)}),
    (literal("/api/login"), {"POST": Route(api_login, public=True, renew=False)}),
    (literal("/api/logout"), {"POST": Route(api_logout, public=True, renew=False)}),
    (literal("/api/account"), {"PATCH": Route(api_update_account)}),
    (literal("/api/account/password"), {"POST": Route(api_change_password, allow_pending=True)}),
    (literal("/api/account/logout-everywhere"), {"POST": Route(api_logout_everywhere, allow_pending=True)}),
    (literal("/api/account/delete"), {"POST": Route(api_delete_account)}),
    (literal("/api/admin/users"), {"GET": admin_route(api_admin_users),
                                   "POST": admin_route(api_admin_create_user)}),
    (ADMIN_USER, {"PATCH": admin_route(api_admin_update_user)}),
    (ADMIN_USER + literal("/password"), {"POST": admin_route(api_admin_reset_password)}),
    (ADMIN_USER + literal("/disable"), {"POST": admin_route(api_admin_disable)}),
    (ADMIN_USER + literal("/enable"), {"POST": admin_route(api_admin_enable)}),
    (ADMIN_USER + literal("/promote"), {"POST": admin_route(api_admin_promote)}),
    (ADMIN_USER + literal("/demote"), {"POST": admin_route(api_admin_demote)}),
    (ADMIN_USER + literal("/unlock"), {"POST": admin_route(api_admin_unlock)}),
    (ADMIN_USER + literal("/delete"), {"POST": admin_route(api_admin_delete)}),
]]


# --- Sessions (contract section 5) ---

def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(conn, user_id, now):
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, last_used_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)", (token_hash(token), user_id, now, now, now + SESSION_TTL))
    return token


# --- Host header (contract section 3) ---

def parse_host_header(value):
    """Returns (lower-cased host, port or None), or None if it doesn't parse.

    A port is split off only if it is 1-5 ASCII digits; otherwise the whole value
    is the host (and won't match any allowed entry).
    """
    parts = split_host_port(value)
    if parts is None:
        return None
    host, port = parts
    if port is not None and HOST_PORT_RE.fullmatch(port):
        return (host.lower(), int(port)) if host else None
    if value.startswith("["):
        return None if port is not None else (host.lower(), None)
    return (value.lower(), None) if value else None


def host_allowed(value, allowed):
    parsed = parse_host_header(value)
    if parsed is None:
        return False
    host, port = parsed
    return any(host == a_host and (a_port is None or a_port == port) for a_host, a_port in allowed)


def log_safe(text, limit=200):
    """Escapes control and non-ASCII characters so request data can't forge log lines."""
    return "".join(c if " " <= c <= "~" else f"\\x{ord(c):02x}" for c in text[:limit])


def _reject_constant(name):
    raise ValueError(f"{name} is not allowed")


# --- Request handler ---

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    timeout = READ_TIMEOUT

    @property
    def app(self):
        return self.server

    def version_string(self):
        return "WorldEats"

    # Logging: only method, path without query string, and status.

    def _log_path(self):
        raw = getattr(self, "raw_path", None) or getattr(self, "path", None) or "-"
        return raw.split("?", 1)[0]

    def log_request(self, code="-", size="-"):
        path = self._log_path()
        if path == "/health":
            return
        method = self.command or "-"
        self.app.log(f"{log_safe(method, 20)} {log_safe(path)} {int(code)}")

    def log_error(self, format, *args):
        pass  # the status line is logged by log_request; args can contain the request line

    def log_message(self, format, *args):
        pass

    # Request processing.

    def handle_one_request(self):
        self.raw_path = None
        self.cookie_out = None
        self.conn = None
        self.command = None
        self.request_version = "HTTP/1.0"
        try:
            head = self.read_head()
            if head is None:
                self.close_connection = True
                return
            if head is False:
                self.send_error(431)
                return
            self.raw_requestline = head[0]
            # parse_request() reads the headers from what read_head() already took;
            # the real stream is restored for the body.
            real_rfile = self.rfile
            self.rfile = io.BytesIO(b"".join(head[1:]))
            try:
                parsed = self.parse_request()
            finally:
                self.rfile = real_rfile
            if not parsed:
                return
            # parse_request() collapses a leading '//'; routing uses the path exactly as sent.
            self.raw_path = self.requestline.split()[1]
            if self.request_version == "HTTP/0.9":
                self.send_error(400)
                return
            if self.command in ("POST", "PATCH"):
                if self.app.reaper.is_reaped(self.request):
                    self.close_connection = True
                    return
            elif not self.app.reaper.finish_reading(self.request):
                self.close_connection = True
                return
            self.dispatch()
            self.wfile.flush()
        except OSError:
            # Timeouts, resets and reaper shutdowns: drop the connection quietly.
            self.close_connection = True

    def read_head(self):
        """Reads the request line and headers within the header budget.

        Returns the list of lines (request line first, blank line last), False if
        the budget or line limit is exceeded, or None if the client stopped early.
        """
        lines = []
        total = 0
        while True:
            line = self.rfile.readline(HEADER_BUDGET - total + 1)
            total += len(line)
            if total > HEADER_BUDGET:
                return False
            if not line.endswith(b"\n"):
                return None  # EOF (or a reaper shutdown) before the head was complete
            lines.append(line)
            if len(lines) > 1 and line in (b"\r\n", b"\n"):
                return lines
            if len(lines) == 1 and line in (b"\r\n", b"\n"):
                return None  # no request line
            if len(lines) - 1 > MAX_HEADER_LINES:
                return False

    def dispatch(self):
        try:
            if self.command in ("GET", "HEAD") and self.raw_path == "/health":
                self.health()
                return
            self.route()
        except _CloseConnection:
            self.close_connection = True
        except ApiError as e:
            self.send_json(e.status, {"error": e.message, **e.extra})
        except auth.Invalid as e:
            self.send_json(400, {"error": str(e)})
        except auth.Busy:
            self.send_json(503, {"error": "Server busy, try again"})
        except (ConnectionError, TimeoutError):
            raise
        except Exception as e:
            self.app.log_exception(e)
            self.cookie_out = None
            self.send_json(500, {"error": "Internal error"})
        finally:
            if self.conn is not None:
                self.conn.close()

    def route(self):
        for name in SINGLE_HEADERS:
            if len(self.headers.get_all(name) or ()) > 1:
                raise ApiError(400, "Bad request")
        host = self.headers.get("Host")
        if host is None or not host_allowed(host, self.app.config.allowed_hosts):
            raise ApiError(421, "Unknown host")
        method = self.command
        if method not in HANDLED_METHODS:
            raise ApiError(405, "Method not allowed")
        if self.headers.get_all("Transfer-Encoding"):
            raise ApiError(411, "Content-Length required")
        self.app.warn_untrusted_forwarding(self.client_address[0], self.headers)

        path = self.raw_path.split("?", 1)[0]
        writes = method in ("POST", "PATCH")
        route = None
        args = ()
        if path.startswith("/api/"):
            route, args = self.find_route(path, method)
        elif writes:
            raise ApiError(405, "Method not allowed")

        if writes:
            self.body = self.read_json_body(host)
        else:
            length = self.headers.get("Content-Length")
            if length is not None and length != "0":
                raise ApiError(400, "Bad request")
            self.body = {}

        if route is None:
            self.serve_static(path)
            return
        self.load_session(renew=route.renew)
        if not route.public:
            if self.user is None:
                raise ApiError(401, "Not logged in")
            if self.user["must_change_password"] and not route.allow_pending:
                raise ApiError(403, "You must change your password first", {"password_change_required": True})
            if route.admin and not self.user["is_admin"]:
                raise ApiError(403, "Admins only")
        status, payload = route.handler(self, *args)
        self.send_json(status, payload)

    def find_route(self, path, method):
        """The first route whose pattern matches the whole path: (Route, path groups), or 404/405."""
        for pattern, methods in self.app.routes:
            match = pattern.fullmatch(path)
            if match is None:
                continue
            route = methods.get("GET" if method == "HEAD" else method)
            if route is None:
                raise ApiError(405, "Method not allowed")
            return route, match.groups()
        raise ApiError(404, "Not found")

    def read_json_body(self, host):
        origin = self.headers.get("Origin")
        if origin is None or origin.lower() not in ("http://" + host.lower(), "https://" + host.lower()):
            raise ApiError(403, "Cross-origin request blocked")
        media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise ApiError(415, "Content-Type must be application/json")
        length = self.headers.get("Content-Length")
        if length is None:
            raise ApiError(411, "Content-Length required")
        if not CONTENT_LENGTH_RE.fullmatch(length):
            raise ApiError(400, "Invalid Content-Length")
        length = int(length)
        if length > MAX_BODY:
            raise ApiError(413, "Request too large")
        data = self.read_exactly(length)
        if not self.app.reaper.finish_reading(self.request):
            raise _CloseConnection()
        if not data:
            return {}
        try:
            body = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ApiError(400, "Invalid JSON") from None
        if not isinstance(body, dict):
            raise ApiError(400, "Invalid JSON")
        return body

    def read_exactly(self, length):
        deadline = time.monotonic() + self.app.body_deadline
        chunks = []
        remaining = length
        try:
            while remaining:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise _CloseConnection()
                self.connection.settimeout(min(READ_TIMEOUT, left))
                chunk = self.rfile.read1(remaining)
                if not chunk:
                    raise _CloseConnection()
                chunks.append(chunk)
                remaining -= len(chunk)
        except OSError:
            raise _CloseConnection() from None
        self.connection.settimeout(READ_TIMEOUT)
        return b"".join(chunks)

    # Sessions.

    def load_session(self, renew):
        self.user = None
        self.session_id = None
        name = self.app.config.cookie_name
        values = []
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                values.append(value)
        if len(values) != 1:
            return  # none, or ambiguous: no session and nothing cleared
        token = values[0]
        row = None
        if token and len(token) <= MAX_COOKIE_VALUE:
            now = self.app.now()
            row = self.db().execute(
                "SELECT sessions.id AS session_id, sessions.last_used_at, users.id, users.username, "
                "users.display_name, users.is_admin, users.must_change_password "
                "FROM sessions JOIN users ON users.id = sessions.user_id "
                "WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.disabled = 0",
                (token_hash(token), now)).fetchone()
        if row is None:
            self.clear_session_cookie()
            return
        self.user = row
        self.session_id = row["session_id"]
        if renew and now - row["last_used_at"] >= RENEW_AFTER:
            with self.write():
                conn = self.db()
                conn.execute(
                    "UPDATE sessions SET last_used_at = ?, expires_at = ? WHERE id = ?",
                    (now, now + SESSION_TTL, self.session_id))
                conn.execute("UPDATE users SET last_active_at = ? WHERE id = ?", (now, row["id"]))
            self.cookie_out = self.session_cookie(token, SESSION_TTL)

    def session_cookie(self, value, max_age):
        parts = [f"{self.app.config.cookie_name}={value}", "HttpOnly", "SameSite=Lax", "Path=/",
                 f"Max-Age={max_age}"]
        if self.app.config.secure_cookies:
            parts.append("Secure")
        return "; ".join(parts)

    def issue_session(self, token):
        self.cookie_out = self.session_cookie(token, SESSION_TTL)

    def clear_session_cookie(self):
        self.cookie_out = self.session_cookie("", 0)

    # Helpers for endpoints.

    def db(self):
        if self.conn is None:
            self.conn = db.connect(self.app.db_path)
        return self.conn

    def write(self):
        return db.write(self.db(), self.app.write_lock)

    def client_ip(self):
        return auth.client_ip(self.client_address[0], self.headers.get_all("X-Forwarded-For") or [],
                              self.app.config.trusted_proxies)

    # Responses.

    def health(self):
        conn = db.connect(self.app.db_path)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        self.send_body(200, b"ok", "text/plain; charset=utf-8", "no-store")

    def serve_static(self, path):
        entry = STATIC_FILES.get(path)
        if entry is None:
            raise ApiError(404, "Not found")
        filename, content_type = entry
        try:
            data = (self.app.static_dir / filename).read_bytes()
        except FileNotFoundError:
            raise ApiError(404, "Not found") from None
        self.send_body(200, data, content_type, "no-cache")

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_body(status, body, "application/json; charset=utf-8", "no-store")

    def send_body(self, status, body, content_type, cache_control):
        self.send_response(status)
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        if getattr(self, "cookie_out", None):
            self.send_header("Set-Cookie", self.cookie_out)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        """Every error the stdlib sends itself becomes JSON with the security headers."""
        if self.request_version in ("HTTP/0.9", "", None):
            self.request_version = "HTTP/1.0"  # so the status line and headers are sent
        self.close_connection = True
        self.cookie_out = None
        self.send_json(code, {"error": STDLIB_ERRORS.get(code, "Bad request")})


# --- Connection tracking ---

class Reaper:
    """Shuts down connections whose request hasn't been fully read `limit` seconds after accept.

    Reaped sockets are remembered, so a request whose head or body completed just
    as it was cut off is still dropped instead of dispatched.
    """

    def __init__(self, limit):
        self.limit = limit
        self.lock = threading.Lock()
        self.sockets = {}
        self.reaped = set()
        self.stopped = threading.Event()

    def track(self, sock):
        with self.lock:
            self.sockets[sock] = time.monotonic()

    def finish_reading(self, sock):
        """The request is fully read: stop tracking. False if it was already reaped."""
        with self.lock:
            if sock in self.reaped:
                return False
            self.sockets.pop(sock, None)
            return True

    def is_reaped(self, sock):
        with self.lock:
            return sock in self.reaped

    def forget(self, sock):
        with self.lock:
            self.sockets.pop(sock, None)
            self.reaped.discard(sock)

    def reap_once(self):
        now = time.monotonic()
        with self.lock:
            expired = [s for s, t in self.sockets.items() if now - t >= self.limit]
            for sock in expired:
                del self.sockets[sock]
                self.reaped.add(sock)
        for sock in expired:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def run(self):
        while not self.stopped.wait(1.0):
            self.reap_once()


class App(ThreadingHTTPServer):
    """The server. Keyword arguments are seams for tests; production uses the defaults."""

    daemon_threads = True
    request_queue_size = 128  # listen backlog; the stdlib default of 5 drops bursts of connections

    def __init__(self, config, address=None, static_dir=STATIC_DIR, hasher=None, clock=time.time,
                 log_stream=None, max_connections=64, request_deadline=20.0, body_deadline=BODY_DEADLINE):
        self.config = config
        self.db_path = os.path.join(config.data_dir, db.DB_NAME)
        self.static_dir = Path(static_dir)
        self.clock = clock
        self.log_stream = log_stream if log_stream is not None else sys.stderr
        self.log_lock = threading.Lock()
        self.write_lock = db.WriteLock()
        self.routes = [(pattern, dict(methods)) for pattern, methods in ROUTES]
        self.hasher = hasher if hasher is not None else auth.PasswordHasher()
        self.lockout = auth.Lockout(lambda: self.clock())
        self.connection_slots = threading.BoundedSemaphore(max_connections)
        self.reaper = Reaper(request_deadline)
        self.body_deadline = body_deadline
        self.xff_warned = False

        db.init_schema(self.db_path)
        conn = db.connect(self.db_path)
        try:
            if not db.any_users(conn):
                self.log("Setup required: no accounts exist")
        finally:
            conn.close()
        self.hasher.init_dummy()

        super().__init__(address or ("0.0.0.0", config.port), Handler)
        threading.Thread(target=self.reaper.run, name="reaper", daemon=True).start()

    def now(self):
        return int(self.clock())

    def log(self, message):
        with self.log_lock:
            self.log_stream.write(message + "\n")
            self.log_stream.flush()

    def log_exception(self, exc):
        # Server log only; responses get a generic 500.
        self.log("".join(traceback.format_exception(exc)).rstrip())

    def warn_untrusted_forwarding(self, peer, headers):
        if self.xff_warned or not headers.get_all("X-Forwarded-For"):
            return
        try:
            trusted = normalise_ip(ipaddress.ip_address(peer)) in self.config.trusted_proxies
        except ValueError:
            trusted = False
        if trusted:
            return
        with self.log_lock:
            if self.xff_warned:
                return
            self.xff_warned = True
        self.log(f"X-Forwarded-For received from {log_safe(peer)} but WORLD_EATS_TRUSTED_PROXIES "
                 "is not set; all clients share one lockout bucket")

    def process_request(self, request, client_address):
        if not self.connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        self.reaper.track(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.reaper.forget(request)
            self.connection_slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.reaper.forget(request)
            self.connection_slots.release()

    def handle_error(self, request, client_address):
        self.log("Unhandled error while processing a connection")
        self.log_exception(sys.exc_info()[1])

    def server_close(self):
        self.reaper.stopped.set()
        super().server_close()


def data_dir_writable(path):
    try:
        with tempfile.TemporaryFile(dir=path):
            return True
    except OSError:
        return False


def main():
    os.umask(0o077)
    try:
        config = load_config(os.environ)
    except ConfigError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    if not data_dir_writable(config.data_dir):
        print(f"DATA_DIR {config.data_dir} is not writable by UID {os.getuid()}", file=sys.stderr)
        sys.exit(1)
    app = App(config)
    app.log(f"World Eats listening on :{config.port}")
    app.serve_forever()


if __name__ == "__main__":
    main()
