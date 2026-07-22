"""Tests for connection handling, the migration runner, and the statement splitter."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from printer_debugger.store import db as db_module
from printer_debugger.store.db import Database, _split_statements, code_schema_version
from printer_debugger.store.errors import DatabaseNewerThanCodeError, MigrationError


class MigrationTest(unittest.TestCase):
    """Migrations apply in order, record their versions, and are all-or-nothing."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.path = Path(self._dir.name) / "test.db"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_migrate_fresh_database_applies_all(self) -> None:
        """A fresh database gains every table and reports the code's schema version."""
        database = Database(self.path)
        result = database.migrate()
        self.assertEqual(result, code_schema_version())
        with database.read() as conn:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for expected in ("printer", "session", "approval", "procedure_result", "section_cache"):
            self.assertIn(expected, tables)
        database.close()

    def test_migrate_is_idempotent(self) -> None:
        """Running migrate twice applies nothing the second time and keeps the version."""
        database = Database(self.path)
        first = database.migrate()
        self.assertEqual(database.current_version(), first)
        second = database.migrate()
        self.assertEqual(second, first)
        with database.read() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()["c"]
        self.assertEqual(count, code_schema_version())
        database.close()

    def test_current_version_zero_on_fresh_database(self) -> None:
        """A database with no schema_version table reports version 0."""
        database = Database(self.path)
        self.assertEqual(database.current_version(), 0)
        database.close()

    def test_database_newer_than_code_is_fatal(self) -> None:
        """A schema_version above the code's maximum is refused with both versions named."""
        database = Database(self.path)
        database.migrate()
        with database.write() as conn:
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (9999, "2026-07-21T00:00:00.000Z"),
            )
        with self.assertRaises(DatabaseNewerThanCodeError) as ctx:
            database.migrate()
        self.assertEqual(ctx.exception.database_version, 9999)
        database.close()

    def test_migration_failure_rolls_back(self) -> None:
        """A failing migration leaves the version unchanged and no partial schema."""
        original = db_module._read_migration
        applied_versions: list[int] = []

        def failing_read(path: Path) -> str:
            applied_versions.append(1)
            return "CREATE TABLE ok (id TEXT); CREATE TABLE bad (;"

        db_module._read_migration = failing_read
        try:
            database = Database(self.path)
            with self.assertRaises(MigrationError):
                database.migrate()
            self.assertEqual(database.current_version(), 0)
            with database.read() as conn:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='ok'"
                ).fetchone()
            self.assertIsNone(row, "no table from a rolled-back migration should survive")
            database.close()
        finally:
            db_module._read_migration = original


class StatementSplitterTest(unittest.TestCase):
    """The migration statement splitter respects comments and string literals."""

    def test_splits_on_semicolons(self) -> None:
        """Two statements separated by a semicolon split into two."""
        self.assertEqual(
            _split_statements("CREATE TABLE a (x); CREATE TABLE b (y);"),
            ["CREATE TABLE a (x)", "CREATE TABLE b (y)"],
        )

    def test_ignores_line_comments(self) -> None:
        """A -- comment is stripped and does not become a statement."""
        self.assertEqual(
            _split_statements("-- a comment\nCREATE TABLE a (x);"),
            ["CREATE TABLE a (x)"],
        )

    def test_semicolon_inside_string_is_not_a_boundary(self) -> None:
        """A semicolon inside a quoted literal does not split the statement."""
        self.assertEqual(
            _split_statements("INSERT INTO a VALUES ('x; y'); SELECT 1;"),
            ["INSERT INTO a VALUES ('x; y')", "SELECT 1"],
        )


class PragmaTest(unittest.TestCase):
    """Connections are opened with the pragmas store.md §6 requires."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.path = Path(self._dir.name) / "test.db"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_wal_and_foreign_keys_enabled(self) -> None:
        """A connection reports WAL journal mode and foreign-key enforcement on."""
        database = Database(self.path)
        with database.read() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")
        self.assertEqual(fk, 1)
        database.close()

    def test_foreign_keys_are_enforced(self) -> None:
        """A reference to a missing row is rejected, proving foreign keys are active."""
        database = Database(self.path)
        database.migrate()
        with self.assertRaises(sqlite3.IntegrityError):
            with database.write() as conn:
                conn.execute(
                    "INSERT INTO config_snapshot (id, printer_id, source, captured_at, contents) "
                    "VALUES ('cfg_1', 'prn_missing', 'files', '2026-07-21T00:00:00.000Z', '{}')"
                )
        database.close()


if __name__ == "__main__":
    unittest.main()
