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


class MigrationTests(DbTestCase):
    def test_fresh_database(self):
        db.init_schema(self.path)
        self.assertEqual(self.version(), 1)
        conn = db.connect(self.path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertEqual(tables, {"users", "sessions"})
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()

    def test_existing_version_1_untouched(self):
        db.init_schema(self.path)
        conn = db.connect(self.path)
        conn.execute("INSERT INTO users (username, display_name, password_hash, created_at) "
                     "VALUES ('kept', 'Kept', 'x', 1)")
        conn.close()
        with open(self.path, "rb") as f:
            before = f.read()
        db.init_schema(self.path)
        db.init_schema(self.path)
        self.assertEqual(self.version(), 1)
        conn = db.connect(self.path)
        try:
            self.assertEqual(conn.execute("SELECT username FROM users").fetchall()[0][0], "kept")
        finally:
            conn.close()
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_only_newer_versions_run(self):
        db.init_schema(self.path)
        # version 1 is already applied, so its (here invalid) statements must not run again
        migrations = [(1, ["NOT VALID SQL"]),
                      (2, ["CREATE TABLE IF NOT EXISTS extra (id INTEGER PRIMARY KEY)"])]
        db.init_schema(self.path, migrations)
        self.assertEqual(self.version(), 2)
        db.init_schema(self.path, migrations)
        self.assertEqual(self.version(), 2)
        conn = db.connect(self.path)
        try:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'extra'").fetchone())
        finally:
            conn.close()

    def test_failed_migration_rolls_back(self):
        db.init_schema(self.path)
        bad = db.MIGRATIONS + [(2, ["CREATE TABLE half (id INTEGER)", "NOT VALID SQL"])]
        with self.assertRaises(sqlite3.Error):
            db.init_schema(self.path, bad)
        self.assertEqual(self.version(), 1)
        conn = db.connect(self.path)
        try:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'half'").fetchone())
        finally:
            conn.close()


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
