"""SQLite storage: one connection per request, writes serialised by an app-level lock.

Schema changes are MIGRATIONS entries: (version, statements). On every start,
init_schema() runs, inside one BEGIN IMMEDIATE transaction, the statements of
every version above the database's PRAGMA user_version, then records that
version. Never edit a released version's statements; add a new version. Keep
statements safe on existing data: CREATE ... IF NOT EXISTS, and add columns
with add_column() (checks PRAGMA table_info first; NOT NULL columns need a default).
A statement is either an SQL string or a callable taking the connection.
Callable steps may import domain modules (e.g. search for migration 5), so
those modules must never import db.

Write transactions: only top-level request handlers open one (`with
req.write():`). Helpers that write take the connection and assume the caller
already holds the transaction; they never call write() themselves. Nesting
write() raises RuntimeError instead of deadlocking. A GET that needs several
SELECTs to agree (e.g. a membership check then the details) wraps them in
`with req.read():`, a plain deferred transaction.
"""

import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

import search

DB_NAME = "world-eats.db"

IDENTIFIER_RE = re.compile(r"[a-z_]+")


def add_column(table, column, declaration):
    """A migration step that adds a column unless it already exists."""
    if not (IDENTIFIER_RE.fullmatch(table) and IDENTIFIER_RE.fullmatch(column)):
        raise ValueError("bad identifier in migration")

    def run(conn):
        existing = {row[0] for row in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))}
        if column not in existing:
            # Identifiers and declaration are fixed strings from MIGRATIONS, never input.
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")  # security-lint: ok fixed identifiers from MIGRATIONS, checked against IDENTIFIER_RE
    return run


def backfill_search_text(conn):
    """Migration 5: meals.search_text from each meal's dish, place and notes."""
    rows = conn.execute("SELECT id, dish, place, notes FROM meals").fetchall()
    conn.executemany("UPDATE meals SET search_text = ? WHERE id = ?",
                     [(search.meal_search_text(row[1], row[2], row[3]), row[0]) for row in rows])


def backfill_avatar_seeds(conn):
    """Migration 6: a random avatar seed for every user without one. Fills only empty seeds."""
    rows = conn.execute("SELECT id FROM users WHERE avatar_seed = ''").fetchall()
    conn.executemany("UPDATE users SET avatar_seed = ? WHERE id = ?",
                     [(new_avatar_seed(), row[0]) for row in rows])


def new_avatar_seed():
    """Change 11a: 128 random bits as 32 lowercase hex characters. Never derived from input."""
    return secrets.token_hex(16)


MIGRATIONS = [
    (1, [
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at INTEGER NOT NULL,
            last_used_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS sessions_user_id ON sessions(user_id)",
    ]),
    (2, [
        add_column("users", "disabled", "INTEGER NOT NULL DEFAULT 0"),
        add_column("users", "must_change_password", "INTEGER NOT NULL DEFAULT 0"),
        add_column("users", "last_active_at", "INTEGER"),
        add_column("users", "temp_password_set_at", "INTEGER"),
        "UPDATE users SET last_active_at = "
        "(SELECT MAX(last_used_at) FROM sessions WHERE sessions.user_id = users.id)",
    ]),
    # Feature 2: passports. AUTOINCREMENT so ids of deleted rows are never reused.
    (3, [
        """CREATE TABLE IF NOT EXISTS passports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            is_personal INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS passports_one_personal ON passports(owner_id) WHERE is_personal = 1",
        "CREATE INDEX IF NOT EXISTS passports_owner ON passports(owner_id)",
        """CREATE TABLE IF NOT EXISTS passport_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            passport_id INTEGER NOT NULL REFERENCES passports(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('participant', 'viewer')),
            created_at INTEGER NOT NULL,
            UNIQUE (passport_id, user_id)
        )""",
        "CREATE INDEX IF NOT EXISTS passport_members_user ON passport_members(user_id)",
        add_column("users", "default_passport_id", "INTEGER REFERENCES passports(id) ON DELETE SET NULL"),
        lambda conn: conn.execute(
            "INSERT INTO passports (name, owner_id, is_personal, created_at) "
            "SELECT 'My passport', id, 1, ? FROM users WHERE NOT EXISTS "
            "(SELECT 1 FROM passports p WHERE p.owner_id = users.id AND p.is_personal = 1)",
            (int(time.time()),)),
        "INSERT INTO passport_members (passport_id, user_id, role, created_at) "
        "SELECT id, owner_id, 'participant', created_at FROM passports WHERE is_personal = 1 AND NOT EXISTS "
        "(SELECT 1 FROM passport_members m WHERE m.passport_id = passports.id AND m.user_id = passports.owner_id)",
    ]),
    # Feature 3: meals. The composite FK deletes a member's meals with their membership.
    (4, [
        """CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            passport_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            country_code TEXT NOT NULL CHECK (length(country_code) = 2),
            eaten_on TEXT NOT NULL CHECK (eaten_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
            dish TEXT NOT NULL DEFAULT '',
            place TEXT NOT NULL DEFAULT '',
            rating INTEGER CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
            notes TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY (passport_id, user_id) REFERENCES passport_members(passport_id, user_id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS meals_member ON meals(passport_id, user_id)",
        "CREATE INDEX IF NOT EXISTS meals_country ON meals(passport_id, country_code)",
        "CREATE INDEX IF NOT EXISTS meals_user ON meals(user_id)",
    ]),
    # Feature 4: folded meal text for search. Recomputed for every meal, so safe to re-run.
    (5, [
        add_column("meals", "search_text", "TEXT NOT NULL DEFAULT ''"),
        backfill_search_text,
    ]),
    # Change 11a: generated avatars. The seed is random, never from the username (usernames are reused).
    (6, [
        add_column("users", "avatar_seed", "TEXT NOT NULL DEFAULT ''"),
        backfill_avatar_seeds,
    ]),
]


class WriteLock:
    """The app write lock. Re-entry from the owning thread raises instead of deadlocking."""

    def __init__(self):
        self._lock = threading.Lock()
        self._owner = None

    def __enter__(self):
        if self._owner == threading.get_ident():
            raise RuntimeError("nested write transaction: helpers must use the caller's transaction")
        self._lock.acquire()
        self._owner = threading.get_ident()
        return self

    def __exit__(self, *exc):
        self._owner = None
        self._lock.release()
        return False


def connect(path):
    # isolation_level=None: no implicit transactions; writes use BEGIN IMMEDIATE explicitly.
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    # Deleted rows (names, meal notes) are overwritten, not left in free pages.
    conn.execute("PRAGMA secure_delete=ON")
    return conn


def init_schema(path, migrations=MIGRATIONS):
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            for version, statements in migrations:
                if version <= current:
                    continue
                for statement in statements:
                    if callable(statement):
                        statement(conn)
                    else:
                        conn.execute(statement)
                conn.execute("PRAGMA user_version = " + str(int(version)))  # security-lint: ok PRAGMA takes no ? parameters; int from MIGRATIONS, never input
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


@contextmanager
def write(conn, lock):
    """A write transaction: app lock held, BEGIN IMMEDIATE, rolled back on any exception."""
    with lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


@contextmanager
def read(conn):
    """A read transaction: several SELECTs see one consistent snapshot. No lock, no writes."""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")


def any_users(conn):
    return bool(conn.execute("SELECT EXISTS(SELECT 1 FROM users)").fetchone()[0])
