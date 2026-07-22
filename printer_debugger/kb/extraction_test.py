"""Tests for extraction: normalisation and content-hash caching for every section kind."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from printer_debugger.kb import extraction
from printer_debugger.kb.models import SectionExtraction
from printer_debugger.store.db import Database
from printer_debugger.store.structured_store import StructuredStore


class NormalisationTest(unittest.TestCase):
    """Addresses and paths are normalised on the way in ([kb_ingestion.md §3.3])."""

    def test_address_strips_backticks_and_parenthetical(self) -> None:
        """A hostname in a code span with trailing prose reduces to the bare host."""
        self.assertEqual(
            extraction.normalize_address("`trident` (resolvable via DNS)"), "trident"
        )

    def test_config_path_rebased_on_config_base_not_home(self) -> None:
        """A leading ~ is replaced by the config base, not the container's home."""
        self.assertEqual(
            extraction.normalize_config_path("`~/git/printers_config/trident/`", "/data"),
            "/data/git/printers_config/trident",
        )

    def test_absolute_path_passes_through(self) -> None:
        """An absolute path is left as-is."""
        self.assertEqual(extraction.normalize_config_path("/etc/klipper", "/data"), "/etc/klipper")

    def test_none_stays_none(self) -> None:
        """A missing value normalises to None."""
        self.assertIsNone(extraction.normalize_address(None))
        self.assertIsNone(extraction.normalize_config_path(None, "/data"))


class CacheTest(unittest.TestCase):
    """Extraction is cached by section hash for every section kind ([kb_ingestion.md §2.2])."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.db = Database(Path(self._dir.name) / "t.db")
        self.db.migrate()
        self.store = StructuredStore(self.db)
        self._calls: list[str] = []
        self._original = extraction._extract_section

        def counting_extractor(text: str) -> SectionExtraction:
            self._calls.append(text)
            return SectionExtraction(is_printer="printer" in text.lower(), name="X")

        extraction._extract_section = counting_extractor

    def tearDown(self) -> None:
        extraction._extract_section = self._original
        self.db.close()
        self._dir.cleanup()

    def test_unchanged_section_is_not_re_extracted(self) -> None:
        """A repeat of the same text hits the cache and does not call the model again."""
        extraction.extract_with_cache(self.store, "## A printer section")
        extraction.extract_with_cache(self.store, "## A printer section")
        self.assertEqual(len(self._calls), 1)

    def test_non_printer_section_is_also_cached(self) -> None:
        """A non-printer section is cached too, so it is not re-sent on the next change."""
        extraction.extract_with_cache(self.store, "## Slicer\nOrcaSlicer")
        extraction.extract_with_cache(self.store, "## Slicer\nOrcaSlicer")
        self.assertEqual(len(self._calls), 1)

    def test_changed_section_is_re_extracted_without_invalidating_others(self) -> None:
        """A changed section calls the model; an unchanged sibling stays cached."""
        extraction.extract_with_cache(self.store, "## printer one")
        extraction.extract_with_cache(self.store, "## slicer")
        extraction.extract_with_cache(self.store, "## printer one edited")
        extraction.extract_with_cache(self.store, "## slicer")  # still cached
        self.assertEqual(len(self._calls), 3)


if __name__ == "__main__":
    unittest.main()
