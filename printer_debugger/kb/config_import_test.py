"""Tests for configuration import: include expansion, snapshotting, and read errors."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from printer_debugger.kb.config_import import ConfigReadError, import_local_config, read_and_merge
from printer_debugger.store.db import Database
from printer_debugger.store.models import ConfigSource, PrinterStatus
from printer_debugger.store.structured_store import StructuredStore

_TRIDENT = Path(__file__).resolve().parent / "testdata" / "trident"


class ReadAndMergeTest(unittest.TestCase):
    """read_and_merge expands includes and errors on a missing entry."""

    def test_merge_includes_bed(self) -> None:
        """The merged text contains content from the included bed.cfg."""
        merged = read_and_merge(_TRIDENT)
        self.assertIn("[heater_bed]", merged)
        self.assertIn("max_temp: 110", merged)

    def test_missing_entry_raises(self) -> None:
        """A config path with no printer.cfg raises ConfigReadError naming the file."""
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigReadError):
                read_and_merge(tmp)


class ImportLocalConfigTest(unittest.TestCase):
    """import_local_config writes a snapshot with detected discrepancies."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.db = Database(Path(self._dir.name) / "t.db")
        self.db.migrate()
        self.store = StructuredStore(self.db)
        self.printer = self.store.create_printer(
            name="Trident",
            kb_section="s",
            kb_content_hash="h",
            status=PrinterStatus.COMPLETE,
            address="trident",
            config_path=str(_TRIDENT),
        )

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_snapshot_written_with_discrepancies(self) -> None:
        """The snapshot records the files source and the detected discrepancies."""
        import_local_config(self.store, self.printer.id, _TRIDENT)
        snapshot = self.store.latest_config_snapshot(self.printer.id)
        assert snapshot is not None
        self.assertEqual(snapshot.source, ConfigSource.FILES)
        self.assertIn("[heater_bed]", snapshot.contents)
        self.assertTrue(snapshot.discrepancies, "the fixture has known discrepancies")


if __name__ == "__main__":
    unittest.main()
