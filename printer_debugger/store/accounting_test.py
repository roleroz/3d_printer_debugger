"""Tests for storage accounting: the three figures and the per-kind breakdown."""

from __future__ import annotations

import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from printer_debugger.store.accounting import compute_accounting
from printer_debugger.store.artifact_store import LocalFilesystemArtifactStore
from printer_debugger.store.db import Database
from printer_debugger.store.models import ArtifactKind
from printer_debugger.store.structured_store import StructuredStore


class AccountingTest(unittest.TestCase):
    """Accounting reports database size, artifact total, and bytes by kind."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        base = Path(self._dir.name)
        self.db = Database(base / "test.db")
        self.db.migrate()
        self.store = StructuredStore(self.db)
        self.artifacts = LocalFilesystemArtifactStore(base / "artifacts")

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_accounting_reports_all_three_figures(self) -> None:
        """Database bytes are positive, artifact total matches, and the kind breakdown is right."""
        session = self.store.create_session(name="s")
        self.artifacts.put("sessions/s/a.gcode", io.BytesIO(b"x" * 300))
        self.store.add_artifact(
            session_id=session.id,
            kind=ArtifactKind.GCODE,
            blob_key="sessions/s/a.gcode",
            size_bytes=300,
            content_type="text/x.gcode",
        )
        self.artifacts.put("sessions/s/p.jpg", io.BytesIO(b"y" * 50))
        self.store.add_artifact(
            session_id=session.id,
            kind=ArtifactKind.PHOTO,
            blob_key="sessions/s/p.jpg",
            size_bytes=50,
            content_type="image/jpeg",
        )
        report = compute_accounting(self.store, self.artifacts, self.db.path)
        self.assertGreater(report.database_bytes, 0)
        self.assertEqual(report.artifact_bytes, 350)
        self.assertEqual(report.artifact_bytes_by_kind, {"gcode": 300, "photo": 50})


if __name__ == "__main__":
    unittest.main()
