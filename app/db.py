"""SQLite storage: one connection per request, writes serialised by an app-level lock.

Schema changes are MIGRATIONS entries: (version, statements). On every start,
init_schema() runs, inside one BEGIN IMMEDIATE transaction, the statements of
every version above the database's PRAGMA user_version, then records that
version. Never edit a released version's statements; add a new version. Keep
statements safe on existing data: CREATE ... IF NOT EXISTS, and add columns
(with defaults) only after checking PRAGMA table_info.

Write transactions: only top-level request handlers open one (`with
req.write():`). Helpers that write take the connection and assume the caller
already holds the transaction; they never call write() themselves. Nesting
write() raises RuntimeError instead of deadlocking.
"""

import sqlite3
import threading
from contextlib import contextmanager

DB_NAME = "world-eats.db"

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


def any_users(conn):
    return bool(conn.execute("SELECT EXISTS(SELECT 1 FROM users)").fetchone()[0])
