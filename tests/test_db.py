"""Schema migrations and the write-transaction lock."""

import os
import shutil
import sqlite3
import tempfile
import unittest

from tests.helpers import db

import search  # noqa: E402  (app/ is on sys.path via tests.helpers)


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="we-db-")
        self.path = os.path.join(self.dir, db.DB_NAME)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def query(self, sql, params=()):
        conn = db.connect(self.path)
        try:
            return [tuple(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def version(self):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()


LATEST = db.MIGRATIONS[-1][0]
VERSION_1 = db.MIGRATIONS[:1]  # the 1a schema, unchanged since release
VERSION_2 = db.MIGRATIONS[:2]  # the 1b schema
VERSION_3 = db.MIGRATIONS[:3]  # the feature 2 schema
VERSION_4 = db.MIGRATIONS[:4]  # the feature 3 schema
VERSION_5 = db.MIGRATIONS[:5]  # the feature 4 schema
VERSION_6 = db.MIGRATIONS[:6]  # the change 11a schema


class MigrationTests(DbTestCase):
    def columns(self, table="users"):
        conn = sqlite3.connect(self.path)
        try:
            return {r[1]: r for r in conn.execute("SELECT * FROM pragma_table_info(?)", (table,))}
        finally:
            conn.close()

    def test_fresh_database(self):
        db.init_schema(self.path)
        self.assertEqual(LATEST, 7)
        self.assertEqual(self.version(), 7)
        conn = db.connect(self.path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertEqual(tables, {"users", "sessions", "passports", "passport_members", "meals",
                                      "next_stops", "shared_next_stops", "sqlite_sequence"})
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

    def personal(self):
        return self.query("SELECT owner_id, name, is_personal FROM passports ORDER BY owner_id")

    def test_upgrade(self):
        db.init_schema(self.path, VERSION_3)
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
        db.init_schema(self.path, VERSION_3)
        with open(self.path, "rb") as f:
            before = f.read()
        db.init_schema(self.path, VERSION_3)
        self.assertEqual(self.version(), 3)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_steps_are_idempotent(self):
        # Re-running version 3's statements on a version-3 database creates nothing new.
        db.init_schema(self.path, VERSION_3)
        conn = db.connect(self.path)
        try:
            conn.execute("PRAGMA user_version = 2")
        finally:
            conn.close()
        db.init_schema(self.path, VERSION_3)
        self.assertEqual(self.version(), 3)
        self.assertEqual(len(self.personal()), 3)
        self.assertEqual(self.query("SELECT COUNT(*) FROM passport_members"), [(3,)])

    def test_owner_row_added_when_only_the_passport_exists(self):
        # A partly applied state (passport without its owner row) is completed, not duplicated.
        db.init_schema(self.path, VERSION_3)
        conn = db.connect(self.path)
        try:
            conn.execute("DELETE FROM passport_members WHERE user_id = 2")
            conn.execute("PRAGMA user_version = 2")
        finally:
            conn.close()
        db.init_schema(self.path, VERSION_3)
        self.assertEqual(len(self.personal()), 3)
        self.assertEqual(self.query("SELECT COUNT(*) FROM passport_members WHERE user_id = 2"), [(1,)])

    def test_fresh_database_has_no_passports(self):
        shutil.rmtree(self.dir)
        os.mkdir(self.dir)
        db.init_schema(self.path, VERSION_3)
        self.assertEqual(self.version(), 3)
        self.assertEqual(self.personal(), [])

    def test_failure_leaves_version_2(self):
        bad = VERSION_2 + [(3, db.MIGRATIONS[2][1] + ["NOT VALID SQL"])]
        with self.assertRaises(sqlite3.Error):
            db.init_schema(self.path, bad)
        self.assertEqual(self.version(), 2)
        self.assertEqual(self.query("SELECT name FROM sqlite_master WHERE name LIKE 'passport%'"), [])

    def test_constraints(self):
        db.init_schema(self.path, VERSION_3)
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
        db.init_schema(self.path, VERSION_3)
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


class Version4MigrationTests(DbTestCase):
    """Schema version 4 (contract 3 section 3) applied to a feature 2 (version 3) database."""

    def setUp(self):
        super().setUp()
        db.init_schema(self.path, VERSION_3)
        conn = db.connect(self.path)
        try:
            conn.executescript("""
                INSERT INTO users (id, username, display_name, password_hash, is_admin, created_at, disabled)
                    VALUES (1, 'admin', 'Admin', 'h1', 1, 100, 0), (2, 'bob', 'Bob', 'h2', 0, 200, 1);
                INSERT INTO passports (id, name, owner_id, is_personal, created_at)
                    VALUES (1, 'My passport', 1, 1, 100), (2, 'My passport', 2, 1, 200), (3, 'Trip', 1, 0, 300);
                INSERT INTO passport_members (passport_id, user_id, role, created_at)
                    VALUES (1, 1, 'participant', 100), (2, 2, 'participant', 200),
                           (3, 1, 'participant', 300), (3, 2, 'viewer', 300);
                UPDATE users SET default_passport_id = 3 WHERE id = 2;
            """)
        finally:
            conn.close()

    def snapshot(self):
        return [self.query("SELECT id, username, disabled, default_passport_id FROM users ORDER BY id"),
                self.query("SELECT id, name, owner_id, is_personal FROM passports ORDER BY id"),
                self.query("SELECT passport_id, user_id, role FROM passport_members ORDER BY id")]

    def test_upgrade(self):
        before = self.snapshot()
        db.init_schema(self.path, VERSION_4)
        self.assertEqual(self.version(), 4)
        self.assertEqual(self.snapshot(), before, "existing rows untouched")
        self.assertEqual(self.query("SELECT COUNT(*) FROM meals"), [(0,)])
        fks = self.query("SELECT \"table\", \"from\", \"to\", on_delete FROM pragma_foreign_key_list('meals')")
        self.assertEqual(sorted(fks), [("passport_members", "passport_id", "passport_id", "CASCADE"),
                                       ("passport_members", "user_id", "user_id", "CASCADE")])
        indexes = {r[0] for r in self.query("SELECT name FROM pragma_index_list('meals')")}
        self.assertTrue({"meals_member", "meals_country", "meals_user"} <= indexes)
        self.assertEqual(self.query("PRAGMA foreign_key_check"), [])

    def test_running_twice_is_a_no_op(self):
        db.init_schema(self.path, VERSION_4)
        with open(self.path, "rb") as f:
            before = f.read()
        db.init_schema(self.path, VERSION_4)
        self.assertEqual(self.version(), 4)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_steps_are_idempotent(self):
        db.init_schema(self.path, VERSION_4)
        conn = db.connect(self.path)
        try:
            conn.execute("INSERT INTO meals (passport_id, user_id, country_code, eaten_on, created_at, updated_at) "
                         "VALUES (3, 1, 'PE', '2026-10-11', 1, 1)")
            conn.execute("PRAGMA user_version = 3")
        finally:
            conn.close()
        db.init_schema(self.path, VERSION_4)
        self.assertEqual(self.version(), 4)
        self.assertEqual(self.query("SELECT passport_id, user_id, country_code FROM meals"), [(3, 1, "PE")])

    def test_failure_leaves_version_3(self):
        bad = VERSION_3 + [(4, db.MIGRATIONS[3][1] + ["NOT VALID SQL"])]
        with self.assertRaises(sqlite3.Error):
            db.init_schema(self.path, bad)
        self.assertEqual(self.version(), 3)
        self.assertEqual(self.query("SELECT name FROM sqlite_master WHERE name LIKE 'meals%'"), [])

    def test_constraints_and_cascades(self):
        db.init_schema(self.path, VERSION_4)
        conn = db.connect(self.path)
        try:
            def meal(passport_id, user_id, code="PE", eaten_on="2026-10-11", rating=None):
                return conn.execute(
                    "INSERT INTO meals (passport_id, user_id, country_code, eaten_on, rating, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, 1, 1)", (passport_id, user_id, code, eaten_on, rating))

            for bad in ((3, 1, "PER"), (3, 1, "PE", "2026-1-01"), (3, 1, "PE", "2026-10-11", 6),
                        (3, 1, "PE", "2026-10-11", 0), (1, 2)):  # (1, 2): bob isn't in passport 1
                with self.subTest(bad=bad), self.assertRaises(sqlite3.IntegrityError):
                    meal(*bad)
            meal(3, 1)
            meal(1, 1)
            meal(3, 2)  # the FK can't tell a viewer from a participant; the app checks the role
            conn.execute("DELETE FROM passport_members WHERE passport_id = 3 AND user_id = 2")
            self.assertEqual(self.query("SELECT passport_id, user_id FROM meals ORDER BY id"), [(3, 1), (1, 1)])
            conn.execute("DELETE FROM passports WHERE id = 3")
            self.assertEqual(self.query("SELECT passport_id, user_id FROM meals"), [(1, 1)])
            conn.execute("DELETE FROM passports WHERE id = 1")
            self.assertEqual(self.query("SELECT COUNT(*) FROM meals"), [(0,)])
            # AUTOINCREMENT: a deleted meal id is never reused
            last = meal(2, 2).lastrowid
            conn.execute("DELETE FROM meals WHERE id = ?", (last,))
            self.assertGreater(meal(2, 2).lastrowid, last)
        finally:
            conn.close()


class Version5MigrationTests(DbTestCase):
    """Schema version 5 (contract 4 section 3) applied to a feature 3 (version 4) database."""

    NOTES = "Line one\n  line two\n\nTrès bon"

    def setUp(self):
        super().setUp()
        db.init_schema(self.path, VERSION_4)
        conn = db.connect(self.path)
        try:
            conn.executescript("""
                INSERT INTO users (id, username, display_name, password_hash, is_admin, created_at)
                    VALUES (1, 'admin', 'Admin', 'h1', 1, 100), (2, 'bob', 'Bob', 'h2', 0, 200);
                INSERT INTO passports (id, name, owner_id, is_personal, created_at)
                    VALUES (1, 'My passport', 1, 1, 100), (2, 'My passport', 2, 1, 200), (3, 'Trip', 1, 0, 300);
                INSERT INTO passport_members (passport_id, user_id, role, created_at)
                    VALUES (1, 1, 'participant', 100), (2, 2, 'participant', 200),
                           (3, 1, 'participant', 300), (3, 2, 'participant', 300);
            """)
            meals = [(3, 1, "FR", "2026-10-01", "Crème Brûlée", "Chez Ødile", 5, self.NOTES),
                     (3, 2, "PL", "2026-10-02", "Pierogi", "Łódź", None, ""),
                     (1, 1, "PE", "2026-10-03", "", "", None, ""),
                     (2, 2, "SA", "2026-10-04", "\ufdfa" * 100, "ＣＥＶＩＣＨＥ", 3, "\ufdfa" * 2000)]
            conn.executemany("INSERT INTO meals (passport_id, user_id, country_code, eaten_on, dish, place, rating, "
                             "notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 7, 8)", meals)
        finally:
            conn.close()

    def meals(self):
        return self.query("SELECT id, passport_id, user_id, country_code, eaten_on, dish, place, rating, notes, "
                          "created_at, updated_at FROM meals ORDER BY id")

    def search_texts(self):
        return [r[0] for r in self.query("SELECT search_text FROM meals ORDER BY id")]

    def test_upgrade(self):
        before = self.meals()
        db.init_schema(self.path)
        self.assertEqual(self.version(), LATEST)
        self.assertEqual(self.meals(), before, "other columns unchanged")
        texts = self.search_texts()
        self.assertEqual(texts[:3], ["creme brulee\nchez odile\nline one line two tres bon",
                                     "pierogi\nlodz\n", "\n\n"])
        dish, place, notes = texts[3].split("\n")
        self.assertEqual((len(dish), place, len(notes)), (200, "ceviche", 4000), "cut at the caps")
        self.assertEqual(notes, search.fold("\ufdfa" * 2000)[:4000])
        for row, text in zip(before, texts):
            self.assertEqual(text, search.meal_search_text(row[5], row[6], row[8]))
        column = self.query("SELECT type, \"notnull\", dflt_value FROM pragma_table_info('meals') "
                            "WHERE name = 'search_text'")
        self.assertEqual(column, [("TEXT", 1, "''")])

    def test_new_rows_get_the_default(self):
        db.init_schema(self.path)
        conn = db.connect(self.path)
        try:
            conn.execute("INSERT INTO meals (passport_id, user_id, country_code, eaten_on, created_at, updated_at) "
                         "VALUES (3, 1, 'JP', '2026-10-11', 1, 1)")
        finally:
            conn.close()
        self.assertEqual(self.search_texts()[-1], "")

    def test_running_twice_is_a_no_op(self):
        db.init_schema(self.path)
        with open(self.path, "rb") as f:
            before = f.read()
        db.init_schema(self.path)
        self.assertEqual(self.version(), LATEST)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_steps_are_idempotent(self):
        db.init_schema(self.path)
        expected = self.search_texts()
        conn = db.connect(self.path)
        try:
            conn.execute("UPDATE meals SET search_text = 'stale'")
            conn.execute("PRAGMA user_version = 4")
        finally:
            conn.close()
        db.init_schema(self.path)
        self.assertEqual(self.version(), LATEST)
        self.assertEqual(self.search_texts(), expected, "the backfill recomputes every row")

    def test_empty_meals_table(self):
        conn = db.connect(self.path)
        try:
            conn.execute("DELETE FROM meals")
        finally:
            conn.close()
        db.init_schema(self.path)
        self.assertEqual(self.version(), LATEST)

    def test_failure_leaves_version_4(self):
        before = self.meals()
        bad = VERSION_4 + [(5, db.MIGRATIONS[4][1] + ["NOT VALID SQL"])]
        with self.assertRaises(sqlite3.Error):
            db.init_schema(self.path, bad)
        self.assertEqual(self.version(), 4)
        self.assertEqual(self.meals(), before)
        names = {r[0] for r in self.query("SELECT name FROM pragma_table_info('meals')")}
        self.assertNotIn("search_text", names, "ALTER TABLE rolled back with the rest")


SEED_GLOB = "[0-9a-f]" * 32


class Version6MigrationTests(DbTestCase):
    """Schema version 6 (contract 11a section 2) applied to a feature 4 (version 5) database."""

    SEED_RE = r"^[0-9a-f]{32}$"
    USERS = [(1, "admin", "Admin", "h1", 1, 100), (2, "bob", "Bob", "h2", 0, 200),
             (3, "Bob2", "Bob", "h3", 0, 300), (4, "dan", "Dan", "h4", 0, 400)]

    def setUp(self):
        super().setUp()
        self.populate(self.path)

    def populate(self, path):
        db.init_schema(path, VERSION_5)
        conn = db.connect(path)
        try:
            conn.executemany("INSERT INTO users (id, username, display_name, password_hash, is_admin, created_at) "
                             "VALUES (?, ?, ?, ?, ?, ?)", self.USERS)
            conn.execute("UPDATE users SET disabled = 1, must_change_password = 1, temp_password_set_at = 5, "
                         "last_active_at = 6 WHERE id = 4")
            conn.executescript("""
                INSERT INTO passports (id, name, owner_id, is_personal, created_at)
                    VALUES (1, 'My passport', 1, 1, 100), (2, 'My passport', 2, 1, 200),
                           (3, 'My passport', 3, 1, 300), (4, 'My passport', 4, 1, 400), (5, 'Trip', 1, 0, 500);
                INSERT INTO passport_members (passport_id, user_id, role, created_at)
                    VALUES (1, 1, 'participant', 100), (2, 2, 'participant', 200), (3, 3, 'participant', 300),
                           (4, 4, 'participant', 400), (5, 1, 'participant', 500), (5, 2, 'viewer', 500);
                INSERT INTO meals (passport_id, user_id, country_code, eaten_on, dish, search_text,
                                   created_at, updated_at)
                    VALUES (5, 1, 'PE', '2026-10-01', 'Ceviche', 'ceviche', 7, 8);
                INSERT INTO sessions (token_hash, user_id, created_at, last_used_at, expires_at)
                    VALUES ('t1', 1, 1, 2, 3);
                UPDATE users SET default_passport_id = 5 WHERE id = 1;
            """)
        finally:
            conn.close()

    def kept_rows(self):
        """Every pre-existing column of every table."""
        return {
            "users": self.query("SELECT id, username, display_name, password_hash, is_admin, created_at, disabled, "
                                "must_change_password, last_active_at, temp_password_set_at, default_passport_id "
                                "FROM users ORDER BY id"),
            "passports": self.query("SELECT id, name, owner_id, is_personal, created_at FROM passports ORDER BY id"),
            "members": self.query("SELECT id, passport_id, user_id, role, created_at FROM passport_members "
                                  "ORDER BY id"),
            "meals": self.query("SELECT id, passport_id, user_id, country_code, eaten_on, dish, place, rating, notes, "
                                "created_at, updated_at, search_text FROM meals ORDER BY id"),
            "sessions": self.query("SELECT id, token_hash, user_id, created_at, last_used_at, expires_at "
                                   "FROM sessions ORDER BY id"),
        }

    def seeds(self, path=None):
        conn = db.connect(path or self.path)
        try:
            return {row[0]: row[1] for row in conn.execute("SELECT id, avatar_seed FROM users ORDER BY id")}
        finally:
            conn.close()

    def assert_valid_seeds(self):
        seeds = self.seeds()
        self.assertEqual(len(seeds), len(self.USERS))
        for seed in seeds.values():
            self.assertRegex(seed, self.SEED_RE)
        self.assertEqual(len(set(seeds.values())), len(seeds), "one seed per user")
        self.assertEqual(self.query("SELECT COUNT(*) FROM users WHERE avatar_seed NOT GLOB ?", (SEED_GLOB,)),
                         [(0,)])

    def test_upgrade(self):
        before = self.kept_rows()
        db.init_schema(self.path, VERSION_6)
        self.assertEqual(self.version(), 6)
        self.assert_valid_seeds()
        self.assertEqual(self.kept_rows(), before, "existing rows unchanged")
        column = self.query("SELECT type, \"notnull\", dflt_value FROM pragma_table_info('users') "
                            "WHERE name = 'avatar_seed'")
        self.assertEqual(column, [("TEXT", 1, "''")])

    def test_seeds_are_random(self):
        """The same users in another database get other seeds: nothing is derived from the rows."""
        other = os.path.join(self.dir, "other.db")
        self.populate(other)
        db.init_schema(self.path, VERSION_6)
        db.init_schema(other, VERSION_6)
        first, second = self.seeds(), self.seeds(other)
        self.assertEqual(set(first), set(second))
        for user_id in first:
            self.assertNotEqual(first[user_id], second[user_id])

    def test_running_twice_is_a_no_op(self):
        db.init_schema(self.path, VERSION_6)
        seeds = self.seeds()
        with open(self.path, "rb") as f:
            before = f.read()
        db.init_schema(self.path, VERSION_6)
        self.assertEqual(self.version(), 6)
        self.assertEqual(self.seeds(), seeds)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_steps_are_idempotent(self):
        """Re-run from version 5: only empty seeds are filled, existing ones are kept."""
        db.init_schema(self.path, VERSION_6)
        seeds = self.seeds()
        conn = db.connect(self.path)
        try:
            conn.execute("UPDATE users SET avatar_seed = '' WHERE id = 3")
            conn.execute("PRAGMA user_version = 5")
        finally:
            conn.close()
        db.init_schema(self.path, VERSION_6)
        self.assertEqual(self.version(), 6)
        after = self.seeds()
        del seeds[3]
        self.assertNotEqual(after.pop(3), "")
        self.assertEqual(after, seeds)
        self.assert_valid_seeds()

    def test_empty_users_table(self):
        conn = db.connect(self.path)
        try:
            conn.executescript("DELETE FROM meals; DELETE FROM passport_members; DELETE FROM passports; "
                               "DELETE FROM sessions; DELETE FROM users;")
        finally:
            conn.close()
        db.init_schema(self.path, VERSION_6)
        self.assertEqual(self.version(), 6)
        self.assertEqual(self.seeds(), {})

    def test_column_default_is_empty(self):
        """Only the code's INSERTs set a seed; the column itself defaults to ''."""
        db.init_schema(self.path, VERSION_6)
        conn = db.connect(self.path)
        try:
            conn.execute("INSERT INTO users (username, display_name, password_hash, created_at) "
                         "VALUES ('raw', 'Raw', 'x', 1)")
        finally:
            conn.close()
        self.assertEqual(self.query("SELECT avatar_seed FROM users WHERE username = 'raw'"), [("",)])

    def test_failure_leaves_version_5(self):
        before = self.kept_rows()
        bad = VERSION_5 + [(6, db.MIGRATIONS[5][1] + ["NOT VALID SQL"])]
        with self.assertRaises(sqlite3.Error):
            db.init_schema(self.path, bad)
        self.assertEqual(self.version(), 5)
        self.assertEqual(self.kept_rows(), before)
        names = {r[0] for r in self.query("SELECT name FROM pragma_table_info('users')")}
        self.assertNotIn("avatar_seed", names, "ALTER TABLE rolled back with the rest")


class Version7MigrationTests(DbTestCase):
    """Schema version 7 (contract 11b section 2) applied to a change 11a (version 6) database."""

    USERS = Version6MigrationTests.USERS  # the same rows, populated by Version6MigrationTests.populate

    def setUp(self):
        super().setUp()
        Version6MigrationTests.populate(self, self.path)
        db.init_schema(self.path, VERSION_6)

    def kept_rows(self):
        return Version6MigrationTests.kept_rows(self) | {
            "seeds": self.query("SELECT id, avatar_seed FROM users ORDER BY id")}

    def objects(self):
        return self.query("SELECT type, name, tbl_name FROM sqlite_master "
                          "WHERE name LIKE '%next_stops%' ORDER BY name")

    def test_upgrade(self):
        self.assertEqual(self.version(), 6)
        self.assertEqual(self.objects(), [])
        before = self.kept_rows()
        db.init_schema(self.path)
        self.assertEqual(self.version(), 7)
        self.assertEqual(self.kept_rows(), before, "existing rows unchanged")
        self.assertEqual(self.objects(), [
            ("table", "next_stops", "next_stops"), ("index", "next_stops_user", "next_stops"),
            ("table", "shared_next_stops", "shared_next_stops"),
            ("index", "shared_next_stops_added_by", "shared_next_stops"),
            ("index", "sqlite_autoindex_next_stops_1", "next_stops"),
            ("index", "sqlite_autoindex_shared_next_stops_1", "shared_next_stops")])
        self.assertEqual(self.query("SELECT COUNT(*) FROM next_stops"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM shared_next_stops"), [(0,)])
        self.assertEqual(self.query("SELECT \"table\", \"from\", \"to\", on_delete FROM "
                                    "pragma_foreign_key_list('next_stops') ORDER BY seq"),
                         [("passport_members", "passport_id", "passport_id", "CASCADE"),
                          ("passport_members", "user_id", "user_id", "CASCADE")])
        self.assertEqual(self.query("SELECT \"table\", \"from\", \"to\", on_delete FROM "
                                    "pragma_foreign_key_list('shared_next_stops') ORDER BY \"from\""),
                         [("users", "added_by", "id", "SET NULL"), ("passports", "passport_id", "id", "CASCADE")])

    def test_running_twice_is_a_no_op(self):
        db.init_schema(self.path)
        self.add_entries()
        with open(self.path, "rb") as f:
            before = f.read()
        db.init_schema(self.path)
        self.assertEqual(self.version(), 7)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_steps_are_idempotent(self):
        """Re-run from version 6 with entries present: nothing changes, nothing fails."""
        db.init_schema(self.path)
        self.add_entries()
        entries = self.entries()
        conn = db.connect(self.path)
        try:
            conn.execute("PRAGMA user_version = 6")
        finally:
            conn.close()
        db.init_schema(self.path)
        self.assertEqual(self.version(), 7)
        self.assertEqual(self.entries(), entries)

    def test_failure_leaves_version_6(self):
        before = self.kept_rows()
        bad = VERSION_6 + [(7, db.MIGRATIONS[6][1] + ["NOT VALID SQL"])]
        with self.assertRaises(sqlite3.Error):
            db.init_schema(self.path, bad)
        self.assertEqual(self.version(), 6)
        self.assertEqual(self.kept_rows(), before)
        self.assertEqual(self.objects(), [], "CREATE TABLE rolled back with the rest")

    def add_entries(self):
        conn = db.connect(self.path)
        try:
            conn.execute("INSERT INTO next_stops (passport_id, user_id, country_code, created_at) "
                         "VALUES (5, 1, 'FR', 10), (5, 2, 'FR', 11)")  # user 2 is a viewer: FK allows it
            conn.execute("INSERT INTO shared_next_stops (passport_id, country_code, added_by, created_at) "
                         "VALUES (5, 'JP', 1, 12), (5, 'IT', 2, 13)")
        finally:
            conn.close()

    def entries(self):
        return (self.query("SELECT id, passport_id, user_id, country_code, created_at FROM next_stops ORDER BY id"),
                self.query("SELECT id, passport_id, country_code, added_by, created_at FROM shared_next_stops "
                           "ORDER BY id"))

    def test_constraints_and_cascades(self):
        db.init_schema(self.path)
        self.add_entries()
        conn = db.connect(self.path)
        try:
            for sql in ("INSERT INTO next_stops (passport_id, user_id, country_code, created_at) "
                        "VALUES (5, 1, 'FR', 1)",  # unique per list
                        "INSERT INTO next_stops (passport_id, user_id, country_code, created_at) "
                        "VALUES (5, 3, 'DE', 1)",  # not a member
                        "INSERT INTO next_stops (passport_id, user_id, country_code, created_at) "
                        "VALUES (5, 1, 'FRA', 1)",
                        "INSERT INTO shared_next_stops (passport_id, country_code, added_by, created_at) "
                        "VALUES (5, 'JP', 2, 1)",  # once per passport
                        "INSERT INTO shared_next_stops (passport_id, country_code, added_by, created_at) "
                        "VALUES (99, 'DE', 1, 1)",
                        "INSERT INTO shared_next_stops (passport_id, country_code, added_by, created_at) "
                        "VALUES (5, 'D', 1, 1)"):
                with self.subTest(sql=sql):
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(sql)
            # Membership delete takes the own entries; deleting the user nulls added_by.
            conn.execute("DELETE FROM passport_members WHERE passport_id = 5 AND user_id = 2")
            conn.execute("DELETE FROM passports WHERE owner_id = 2")
            conn.execute("DELETE FROM users WHERE id = 2")
        finally:
            conn.close()
        self.assertEqual(self.entries(), ([(1, 5, 1, "FR", 10)], [(1, 5, "JP", 1, 12), (2, 5, "IT", None, 13)]))
        conn = db.connect(self.path)
        try:
            conn.execute("DELETE FROM passports WHERE id = 5")
        finally:
            conn.close()
        self.assertEqual(self.entries(), ([], []))
