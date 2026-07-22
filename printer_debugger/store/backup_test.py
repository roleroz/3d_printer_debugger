"""Tests for backup and restore: consistent copy, ordering, round trip, and failure."""

from __future__ import annotations

import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from printer_debugger.store import backup as backup_module
from printer_debugger.store.artifact_store import LocalFilesystemArtifactStore
from printer_debugger.store.backup import backup, restore
from printer_debugger.store.db import Database
from printer_debugger.store.errors import StoreError
from printer_debugger.store.models import PrinterStatus
from printer_debugger.store.structured_store import StructuredStore


class BackupTest(unittest.TestCase):
    """Backup produces a consistent, restorable pair."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.base = Path(self._dir.name)
        self.db = Database(self.base / "live.db")
        self.db.migrate()
        self.store = StructuredStore(self.db)
        self.artifact_root = self.base / "artifacts"
        self.artifacts = LocalFilesystemArtifactStore(self.artifact_root)

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_backup_and_restore_round_trip(self) -> None:
        """A backed-up database and artifacts restore into a working copy with the data intact."""
        printer = self.store.create_printer(
            name="Voron", kb_section="s", kb_content_hash="h", status=PrinterStatus.COMPLETE
        )
        self.artifacts.put("sessions/s/a.bin", io.BytesIO(b"payload"))
        dest = self.base / "backup"
        backup(self.db, self.artifact_root, dest)
        self.assertTrue((dest / "database.db").exists())
        self.assertTrue((dest / "artifacts" / "sessions" / "s" / "a.bin").exists())

        # Restore into a fresh location and confirm the data survived.
        restored_db_path = self.base / "restored.db"
        restored_artifacts = self.base / "restored_artifacts"
        restore(dest, restored_db_path, restored_artifacts)
        restored = StructuredStore(Database(restored_db_path))
        fetched = restored.get_printer(printer.id)
        assert fetched is not None
        self.assertEqual(fetched.name, "Voron")
        with LocalFilesystemArtifactStore(restored_artifacts).open("sessions/s/a.bin") as handle:
            self.assertEqual(handle.read(), b"payload")

    def test_restore_without_database_is_fatal(self) -> None:
        """Restoring from a directory with no database file raises rather than half-restoring."""
        empty = self.base / "empty_backup"
        empty.mkdir()
        with self.assertRaises(StoreError):
            restore(empty, self.base / "x.db", self.base / "x_artifacts")

    def test_backup_failure_surfaces_as_store_error(self) -> None:
        """A failure during the artifact sync is surfaced as a StoreError."""
        original = backup_module._copytree

        def failing_copytree(source: Path, dest: Path) -> None:
            raise OSError("no space")

        backup_module._copytree = failing_copytree
        try:
            self.artifacts.put("sessions/s/a.bin", io.BytesIO(b"payload"))
            with self.assertRaises(StoreError):
                backup(self.db, self.artifact_root, self.base / "backup2")
        finally:
            backup_module._copytree = original


if __name__ == "__main__":
    unittest.main()
