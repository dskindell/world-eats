"""Schema migrations and the write-transaction lock."""

import os
import shutil
import sqlite3
import tempfile
import unittest

from tests.helpers import db


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="we-db-")
        self.path = os.path.join(self.dir, db.DB_NAME)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def version(self):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()


LATEST = db.MIGRATIONS[-1][0]
VERSION_1 = db.MIGRATIONS[:1]  # the 1a schema, unchanged since release


class MigrationTests(DbTestCase):
    def columns(self, table="users"):
        conn = sqlite3.connect(self.path)
        try:
            return {r[1]: r for r in conn.execute("SELECT * FROM pragma_table_info(?)", (table,))}
        finally:
            conn.close()

    def test_fresh_database(self):
        db.init_schema(self.path)
        self.assertEqual(LATEST, 2)
        self.assertEqual(self.version(), 2)
        conn = db.connect(self.path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertEqual(tables, {"users", "sessions"})
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()
        self.assertTrue({"disabled", "must_change_password", "last_active_at",
                         "temp_password_set_at"} <= set(self.columns()))

    def test_existing_latest_version_untouched(self):
        db.init_schema(self.path)
        conn = db.connect(self.path)
        conn.execute("INSERT INTO users (username, display_name, password_hash, created_at) "
                     "VALUES ('kept', 'Kept', 'x', 1)")
        conn.close()
        with open(self.path, "rb") as f:
            before = f.read()
        db.init_schema(self.path)
        db.init_schema(self.path)
        self.assertEqual(self.version(), LATEST)
        conn = db.connect(self.path)
        try:
            self.assertEqual(conn.execute("SELECT username FROM users").fetchall()[0][0], "kept")
        finally:
            conn.close()
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_only_newer_versions_run(self):
        db.init_schema(self.path)
        # versions up to LATEST are already applied, so these (here invalid) statements must not run again
        migrations = [(v, ["NOT VALID SQL"]) for v, _ in db.MIGRATIONS] + [
            (LATEST + 1, ["CREATE TABLE IF NOT EXISTS extra (id INTEGER PRIMARY KEY)"])]
        db.init_schema(self.path, migrations)
        self.assertEqual(self.version(), LATEST + 1)
        db.init_schema(self.path, migrations)
        self.assertEqual(self.version(), LATEST + 1)
        conn = db.connect(self.path)
        try:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'extra'").fetchone())
        finally:
            conn.close()

    def test_failed_migration_rolls_back(self):
        db.init_schema(self.path)
        bad = db.MIGRATIONS + [(LATEST + 1, ["CREATE TABLE half (id INTEGER)", "NOT VALID SQL"])]
        with self.assertRaises(sqlite3.Error):
            db.init_schema(self.path, bad)
        self.assertEqual(self.version(), LATEST)
        conn = db.connect(self.path)
        try:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'half'").fetchone())
        finally:
            conn.close()


class Version2MigrationTests(DbTestCase):
    """Schema version 2 (contract 1b section 2) applied to a 1a (version 1) database."""

    def setUp(self):
        super().setUp()
        db.init_schema(self.path, VERSION_1)
        conn = db.connect(self.path)
        try:
            conn.executescript("""
                INSERT INTO users (id, username, display_name, password_hash, is_admin, created_at)
                    VALUES (1, 'admin', 'Admin', 'h1', 1, 100), (2, 'bob', 'Bob', 'h2', 0, 200),
                           (3, 'carol', 'Carol', 'h3', 0, 300);
                -- admin: two sessions, one of them expired; bob: only an expired one; carol: none
                INSERT INTO sessions (token_hash, user_id, created_at, last_used_at, expires_at)
                    VALUES ('a', 1, 100, 500, 9999999999), ('b', 1, 100, 400, 450),
                           ('c', 2, 200, 250, 260);
            """)
        finally:
            conn.close()

    def users(self):
        conn = db.connect(self.path)
        try:
            return {r["username"]: dict(r) for r in conn.execute(
                "SELECT username, display_name, password_hash, is_admin, created_at, disabled, "
                "must_change_password, last_active_at, temp_password_set_at FROM users")}
        finally:
            conn.close()

    def test_upgrade(self):
        self.assertEqual(self.version(), 1)
        db.init_schema(self.path)
        self.assertEqual(self.version(), 2)
        users = self.users()
        self.assertEqual(users["admin"], {
            "username": "admin", "display_name": "Admin", "password_hash": "h1", "is_admin": 1,
            "created_at": 100, "disabled": 0, "must_change_password": 0, "last_active_at": 500,
            "temp_password_set_at": None})
        self.assertEqual(users["bob"]["last_active_at"], 250, "backfilled from an expired session too")
        self.assertIsNone(users["carol"]["last_active_at"])
        conn = db.connect(self.path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 3,
                             "the migration doesn't touch sessions")
        finally:
            conn.close()

    def test_new_rows_get_defaults(self):
        db.init_schema(self.path)
        conn = db.connect(self.path)
        try:
            conn.execute("INSERT INTO users (username, display_name, password_hash, created_at) "
                         "VALUES ('dave', 'Dave', 'h', 1)")
        finally:
            conn.close()
        dave = self.users()["dave"]
        self.assertEqual((dave["disabled"], dave["must_change_password"], dave["last_active_at"],
                          dave["temp_password_set_at"]), (0, 0, None, None))

    def test_running_twice_is_a_no_op(self):
        db.init_schema(self.path)
        with open(self.path, "rb") as f:
            before = f.read()
        db.init_schema(self.path)
        self.assertEqual(self.version(), 2)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_add_column_skips_existing_column(self):
        # A column that already exists (e.g. added by hand) doesn't fail the migration.
        conn = db.connect(self.path)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
        finally:
            conn.close()
        db.init_schema(self.path)
        self.assertEqual(self.version(), 2)

    def test_failure_leaves_version_1(self):
        bad = db.MIGRATIONS[:1] + [(2, db.MIGRATIONS[1][1] + ["NOT VALID SQL"])]
        with self.assertRaises(sqlite3.Error):
            db.init_schema(self.path, bad)
        self.assertEqual(self.version(), 1)
        conn = db.connect(self.path)
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM pragma_table_info('users')")}
        finally:
            conn.close()
        self.assertNotIn("disabled", names, "ALTER TABLE rolled back with the rest")


class WriteLockTests(DbTestCase):
    def test_nested_write_raises(self):
        db.init_schema(self.path)
        conn = db.connect(self.path)
        lock = db.WriteLock()
        try:
            with self.assertRaises(RuntimeError):
                with db.write(conn, lock):
                    conn.execute("INSERT INTO users (username, display_name, password_hash, created_at) "
                                 "VALUES ('outer', 'O', 'x', 1)")
                    with db.write(conn, lock):
                        pass
            # the outer transaction was rolled back and the lock released
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
            with db.write(conn, lock):
                conn.execute("INSERT INTO users (username, display_name, password_hash, created_at) "
                             "VALUES ('after', 'A', 'x', 1)")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        finally:
            conn.close()

    def test_lock_serialises_threads(self):
        import threading
        lock = db.WriteLock()
        order = []
        with lock:
            t = threading.Thread(target=lambda: (lock.__enter__(), order.append("other"), lock.__exit__()))
            t.start()
            t.join(0.2)
            self.assertTrue(t.is_alive(), "another thread waits instead of raising")
            order.append("first")
        t.join(5)
        self.assertEqual(order, ["first", "other"])
