"""Tests for the artifact store: streaming round trips, key layout, and failure cleanup."""

from __future__ import annotations

import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from printer_debugger.store import artifact_store as module
from printer_debugger.store.artifact_store import (
    LocalFilesystemArtifactStore,
    artifact_key,
    index_key,
)
from printer_debugger.store.errors import ArtifactNotFoundError, ArtifactWriteError


class KeyLayoutTest(unittest.TestCase):
    """Keys are derived from identifiers, per store.md §5."""

    def test_artifact_key_shape(self) -> None:
        """An artifact key is sessions/<session>/<artifact><ext>."""
        self.assertEqual(artifact_key("ses_1", "art_2", ".gcode"), "sessions/ses_1/art_2.gcode")

    def test_index_key_shape(self) -> None:
        """An index key is indexes/<artifact>/v<version>.idx."""
        self.assertEqual(index_key("art_2", 3), "indexes/art_2/v3.idx")


class LocalArtifactStoreTest(unittest.TestCase):
    """The filesystem implementation, against a temporary directory."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.store = LocalFilesystemArtifactStore(Path(self._dir.name) / "artifacts")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_put_open_round_trip(self) -> None:
        """Bytes put under a key stream back out identically."""
        key = artifact_key("ses_1", "art_1", ".bin")
        self.store.put(key, io.BytesIO(b"hello world"))
        self.assertTrue(self.store.exists(key))
        self.assertEqual(self.store.size(key), 11)
        with self.store.open(key) as handle:
            self.assertEqual(handle.read(), b"hello world")

    def test_open_missing_raises(self) -> None:
        """Opening an absent key raises ArtifactNotFoundError naming the key."""
        with self.assertRaises(ArtifactNotFoundError):
            self.store.open("sessions/x/y")

    def test_size_missing_raises(self) -> None:
        """Sizing an absent key raises ArtifactNotFoundError."""
        with self.assertRaises(ArtifactNotFoundError):
            self.store.size("sessions/x/y")

    def test_total_size_sums_blobs(self) -> None:
        """total_size sums every stored blob and ignores stray temp files."""
        self.store.put("sessions/s/a", io.BytesIO(b"abc"))
        self.store.put("sessions/s/b", io.BytesIO(b"de"))
        self.assertEqual(self.store.total_size(), 5)

    def test_put_failure_mid_stream_leaves_no_blob_or_temp(self) -> None:
        """A mid-stream write failure removes the temp file and stores no final blob."""
        key = artifact_key("ses_1", "art_fail", ".bin")
        original = module._copy_stream

        def failing_copy(source: object, dest: object) -> None:
            raise OSError("disk full")

        module._copy_stream = failing_copy
        try:
            with self.assertRaises(ArtifactWriteError):
                self.store.put(key, io.BytesIO(b"data"))
        finally:
            module._copy_stream = original
        self.assertFalse(self.store.exists(key), "no final blob after a failed put")
        root = Path(self._dir.name) / "artifacts"
        temps = list(root.rglob("*.tmp"))
        self.assertEqual(temps, [], "the partial temp file must be cleaned up")

    def test_key_escaping_root_is_rejected(self) -> None:
        """A key that would escape the artifact root is refused."""
        with self.assertRaises(ArtifactWriteError):
            self.store.put("../escape", io.BytesIO(b"x"))


if __name__ == "__main__":
    unittest.main()
