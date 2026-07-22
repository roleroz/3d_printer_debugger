"""Tests for the document watcher: startup, change detection, and read failures."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from printer_debugger.kb.models import IngestOutcome
from printer_debugger.kb.watcher import DocumentMissingError, DocumentWatcher


class _FakeIngester:
    """Records each ingest call so the watcher's behaviour can be asserted."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def ingest(self, document: str) -> IngestOutcome:
        self.calls.append(document)
        return IngestOutcome(messages=(f"ingested {len(document)} chars",))


class WatcherTest(unittest.TestCase):
    """The watcher ingests at startup and only re-ingests on a content change."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.path = Path(self._dir.name) / "printers.md"
        self.ingester = _FakeIngester()
        self.watcher = DocumentWatcher(self.path, self.ingester, sleep=lambda _: None)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_missing_document_at_startup_is_fatal(self) -> None:
        """Starting with no document raises DocumentMissingError."""
        with self.assertRaises(DocumentMissingError):
            self.watcher.start()

    def test_start_ingests_once(self) -> None:
        """Startup ingests the document exactly once."""
        self.path.write_text("# doc\n## P\nHostname: x\n", encoding="utf-8")
        self.watcher.start()
        self.assertEqual(len(self.ingester.calls), 1)

    def test_poll_ingests_only_on_change(self) -> None:
        """Polling re-ingests when the content hash changes and not otherwise."""
        self.path.write_text("v1", encoding="utf-8")
        self.watcher.start()
        self.assertIsNone(self.watcher.poll())  # unchanged
        self.path.write_text("v2", encoding="utf-8")
        self.assertIsNotNone(self.watcher.poll())  # changed
        self.assertEqual(len(self.ingester.calls), 2)

    def test_unreadable_mid_run_keeps_last_good(self) -> None:
        """A document that disappears mid-run reports and does not crash or re-ingest."""
        self.path.write_text("v1", encoding="utf-8")
        self.watcher.start()
        self.path.unlink()
        outcome = self.watcher.poll()
        assert outcome is not None
        self.assertIn("unreadable", outcome.messages[0])
        self.assertEqual(len(self.ingester.calls), 1)


if __name__ == "__main__":
    unittest.main()
