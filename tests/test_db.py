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
VERSION_2 = db.MIGRATIONS[:2]  # the 1b schema


class MigrationTests(DbTestCase):
    def columns(self, table="users"):
        conn = sqlite3.connect(self.path)
        try:
            return {r[1]: r for r in conn.execute("SELECT * FROM pragma_table_info(?)", (table,))}
        finally:
            conn.close()

    def test_fresh_database(self):
        db.init_schema(self.path)
        self.assertEqual(LATEST, 3)
        self.assertEqual(self.version(), 3)
        conn = db.connect(self.path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertEqual(tables, {"users", "sessions", "passports", "passport_members", "sqlite_sequence"})
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
        db.init_schema(self.path, VERSION_2)
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
        db.init_schema(self.path, VERSION_2)
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
        db.init_schema(self.path, VERSION_2)
        with open(self.path, "rb") as f:
            before = f.read()
        db.init_schema(self.path, VERSION_2)
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
        db.init_schema(self.path, VERSION_2)
        self.assertEqual(self.version(), 2)

    def test_failure_leaves_version_1(self):
        bad = VERSION_1 + [(2, db.MIGRATIONS[1][1] + ["NOT VALID SQL"])]
        with self.assertRaises(sqlite3.Error):
            db.init_schema(self.path, bad)
        self.assertEqual(self.version(), 1)
        conn = db.connect(self.path)
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM pragma_table_info('users')")}
        finally:
            conn.close()
        self.assertNotIn("disabled", names, "ALTER TABLE rolled back with the rest")


class Version3MigrationTests(DbTestCase):
    """Schema version 3 (contract 2 section 2) applied to a 1b (version 2) database."""

    def setUp(self):
        super().setUp()
        db.init_schema(self.path, VERSION_2)
        conn = db.connect(self.path)
        try:
            conn.executescript("""
                INSERT INTO users (id, username, display_name, password_hash, is_admin, created_at,
                                   disabled, must_change_password)
                    VALUES (1, 'admin', 'Admin', 'h1', 1, 100, 0, 0), (2, 'bob', 'Bob', 'h2', 0, 200, 1, 0),
                           (3, 'carol', 'Carol', 'h3', 0, 300, 0, 1);
                INSERT INTO sessions (token_hash, user_id, created_at, last_used_at, expires_at)
                    VALUES ('a', 1, 100, 500, 9999999999), ('c', 3, 300, 350, 9999999999);
            """)
        finally:
            conn.close()

    def query(self, sql, params=()):
        conn = db.connect(self.path)
        try:
            return [tuple(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def personal(self):
        return self.query("SELECT owner_id, name, is_personal FROM passports ORDER BY owner_id")

    def test_upgrade(self):
        db.init_schema(self.path)
        self.assertEqual(self.version(), 3)
        # every user, disabled (bob) and pending (carol) included, has exactly one personal passport
        self.assertEqual(self.personal(), [(1, "My passport", 1), (2, "My passport", 1), (3, "My passport", 1)])
        self.assertEqual(self.query(
            "SELECT m.user_id, m.role FROM passport_members m JOIN passports p ON p.id = m.passport_id "
            "WHERE m.user_id = p.owner_id ORDER BY m.user_id"),
            [(1, "participant"), (2, "participant"), (3, "participant")])
        self.assertEqual(self.query("SELECT COUNT(*) FROM passport_members"), [(3,)])
        self.assertEqual(self.query("SELECT default_passport_id FROM users"), [(None,), (None,), (None,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM sessions"), [(2,)], "sessions untouched")
        self.assertEqual(self.query("SELECT username, password_hash, disabled, must_change_password "
                                    "FROM users ORDER BY id"),
                         [("admin", "h1", 0, 0), ("bob", "h2", 1, 0), ("carol", "h3", 0, 1)])

    def test_running_twice_is_a_no_op(self):
        db.init_schema(self.path)
        with open(self.path, "rb") as f:
            before = f.read()
        db.init_schema(self.path)
        self.assertEqual(self.version(), 3)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_steps_are_idempotent(self):
        # Re-running version 3's statements on a version-3 database creates nothing new.
        db.init_schema(self.path)
        conn = db.connect(self.path)
        try:
            conn.execute("PRAGMA user_version = 2")
        finally:
            conn.close()
        db.init_schema(self.path)
        self.assertEqual(self.version(), 3)
        self.assertEqual(len(self.personal()), 3)
        self.assertEqual(self.query("SELECT COUNT(*) FROM passport_members"), [(3,)])

    def test_owner_row_added_when_only_the_passport_exists(self):
        # A partly applied state (passport without its owner row) is completed, not duplicated.
        db.init_schema(self.path)
        conn = db.connect(self.path)
        try:
            conn.execute("DELETE FROM passport_members WHERE user_id = 2")
            conn.execute("PRAGMA user_version = 2")
        finally:
            conn.close()
        db.init_schema(self.path)
        self.assertEqual(len(self.personal()), 3)
        self.assertEqual(self.query("SELECT COUNT(*) FROM passport_members WHERE user_id = 2"), [(1,)])

    def test_fresh_database_has_no_passports(self):
        shutil.rmtree(self.dir)
        os.mkdir(self.dir)
        db.init_schema(self.path)
        self.assertEqual(self.version(), 3)
        self.assertEqual(self.personal(), [])

    def test_failure_leaves_version_2(self):
        bad = VERSION_2 + [(3, db.MIGRATIONS[2][1] + ["NOT VALID SQL"])]
        with self.assertRaises(sqlite3.Error):
            db.init_schema(self.path, bad)
        self.assertEqual(self.version(), 2)
        self.assertEqual(self.query("SELECT name FROM sqlite_master WHERE name LIKE 'passport%'"), [])

    def test_constraints(self):
        db.init_schema(self.path)
        conn = db.connect(self.path)
        try:
            # one personal passport per user
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO passports (name, owner_id, is_personal, created_at) "
                             "VALUES ('x', 1, 1, 1)")
            # an owner can't be deleted while they own a passport
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM users WHERE id = 1")
            # deleting a passport cascades memberships and clears defaults pointing at it
            cur = conn.execute("INSERT INTO passports (name, owner_id, created_at) VALUES ('Trip', 1, 1)")
            trip = cur.lastrowid
            conn.execute("INSERT INTO passport_members (passport_id, user_id, role, created_at) "
                         "VALUES (?, 1, 'participant', 1), (?, 3, 'viewer', 1)", (trip, trip))
            conn.execute("UPDATE users SET default_passport_id = ? WHERE id = 3", (trip,))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO passport_members (passport_id, user_id, role, created_at) "
                             "VALUES (?, 2, 'owner', 1)", (trip,))
            conn.execute("DELETE FROM passports WHERE id = ?", (trip,))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM passport_members WHERE passport_id = ?",
                                          (trip,)).fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT default_passport_id FROM users WHERE id = 3").fetchone()[0])
            # AUTOINCREMENT: the deleted id is not reused
            cur = conn.execute("INSERT INTO passports (name, owner_id, created_at) VALUES ('Next', 1, 1)")
            self.assertGreater(cur.lastrowid, trip)
        finally:
            conn.close()

    def test_secure_delete_on(self):
        db.init_schema(self.path)
        conn = db.connect(self.path)
        try:
            self.assertEqual(conn.execute("PRAGMA secure_delete").fetchone()[0], 1)
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
