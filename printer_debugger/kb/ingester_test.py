"""Tests for the ingester: upsert, degraded, name rejection, removal, and the view."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from printer_debugger.kb import extraction
from printer_debugger.kb.ingester import KbIngester
from printer_debugger.kb.models import SectionExtraction
from printer_debugger.store.db import Database
from printer_debugger.store.structured_store import StructuredStore

_EXAMPLE = Path(__file__).resolve().parents[2] / "docs" / "examples" / "printer_definition.md"
_TRIDENT = Path(__file__).resolve().parent / "testdata" / "trident"


def _example_extractor(text: str) -> SectionExtraction:
    """Realistic stub: a section is a printer iff it lists a Hostname; pull name/address/path."""
    lines = text.splitlines()
    heading = lines[0].lstrip("#").strip() if lines else ""
    if "Hostname:" not in text:
        return SectionExtraction(is_printer=False)
    address = config_path = None
    for line in lines:
        if "Hostname:" in line:
            address = line.split("Hostname:", 1)[1].strip()
        if "Config files:" in line:
            config_path = line.split("Config files:", 1)[1].strip()
    return SectionExtraction(True, name=heading, address=address, config_path=config_path)


class IngesterTest(unittest.TestCase):
    """The ingester turns the document into printer and config records."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.db = Database(Path(self._dir.name) / "t.db")
        self.db.migrate()
        self.store = StructuredStore(self.db)
        self.ingester = KbIngester(self.store, config_base="/data")
        self._original = extraction._extract_section

    def tearDown(self) -> None:
        extraction._extract_section = self._original
        self.db.close()
        self._dir.cleanup()

    def _use(self, extractor) -> None:
        extraction._extract_section = extractor

    def test_example_document_upserts_two_printers_and_shares_the_rest(self) -> None:
        """The example document yields the two printers and keeps the other sections as context."""
        self._use(_example_extractor)
        outcome = self.ingester.ingest(_EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(set(outcome.printers_upserted), {"Voron Trident 300", "Voron Switchwire"})
        self.assertIn("Slicer", outcome.shared_context_headings)
        self.assertIn("OrcaSlicer", self.ingester.shared_context())
        trident = self.store.get_printer_by_name("Voron Trident 300")
        assert trident is not None
        self.assertEqual(trident.address, "trident")

    def test_degraded_when_config_path_absent(self) -> None:
        """A printer with an address but no config path is stored degraded and stays usable."""
        self._use(lambda text: SectionExtraction(True, name="P1", address="p1.local"))
        outcome = self.ingester.ingest("# T\n## P1\n- Hostname: p1.local\n")
        printer = self.store.get_printer_by_name("P1")
        assert printer is not None
        self.assertEqual(printer.status.value, "degraded")
        self.assertEqual(printer.missing, ("config_path",))
        self.assertIn("P1", outcome.printers_degraded)

    def test_name_absent_section_not_stored_and_others_survive(self) -> None:
        """A nameless printer section stores no row, is reported, and does not fail the ingest."""

        def extractor(text: str) -> SectionExtraction:
            heading = text.splitlines()[0].lstrip("#").strip()
            if heading == "Nameless":
                return SectionExtraction(True, name=None, address="x")
            return SectionExtraction(True, name="Good", address="good.local")

        self._use(extractor)
        outcome = self.ingester.ingest("# T\n## Nameless\n- Hostname: x\n## Named\n- Hostname: y\n")
        self.assertEqual(len(self.store.list_printers()), 1)
        self.assertEqual(self.store.list_printers()[0].name, "Good")
        self.assertTrue(outcome.unnamed_sections)
        self.assertIn("section 1", outcome.unnamed_sections[0])

    def test_removal_flags_absent_and_reappearance_clears_it(self) -> None:
        """A printer dropped from the document keeps its row with absent_since; return clears it."""
        self._use(
            lambda text: SectionExtraction(True, name="P1", address="p1.local")
            if "Hostname:" in text
            else SectionExtraction(is_printer=False)
        )
        self.ingester.ingest("# T\n## P1\n- Hostname: p1.local\n")
        printer_id = self.store.get_printer_by_name("P1").id

        # Re-ingest with the printer removed.
        self.ingester.ingest("# T\n## Slicer\nOrca\n")
        removed = self.store.get_printer(printer_id)
        assert removed is not None
        self.assertIsNotNone(removed.absent_since)

        # Re-ingest with it back.
        self.ingester.ingest("# T\n## P1\n- Hostname: p1.local\n")
        restored = self.store.get_printer(printer_id)
        assert restored is not None
        self.assertIsNone(restored.absent_since)

    def test_upsert_keeps_identity_across_edits(self) -> None:
        """Editing a printer's section keeps the same row id, so sessions stay valid."""
        self._use(lambda text: SectionExtraction(True, name="P1", address="p1.local"))
        self.ingester.ingest("# T\n## P1\n- Hostname: p1.local\nnote: a\n")
        first_id = self.store.get_printer_by_name("P1").id
        self.ingester.ingest("# T\n## P1\n- Hostname: p1.local\nnote: b\n")
        self.assertEqual(self.store.get_printer_by_name("P1").id, first_id)

    def test_assemble_view_with_snapshot_and_discrepancies(self) -> None:
        """The orchestrator view carries the section text, shared context, snapshot, and issues."""

        def extractor(text: str) -> SectionExtraction:
            heading = text.splitlines()[0].lstrip("#").strip()
            if heading == "Trident":
                return SectionExtraction(True, name="Trident", address="trident",
                                         config_path=str(_TRIDENT))
            return SectionExtraction(is_printer=False)

        self._use(extractor)
        self.ingester.ingest(f"# T\n## Trident\n- Hostname: trident\n## Slicer\nOrcaSlicer\n")
        printer_id = self.store.get_printer_by_name("Trident").id
        view = self.ingester.assemble_view(printer_id)
        self.assertIn("Trident", view.section_text)
        self.assertIn("OrcaSlicer", view.shared_context)
        self.assertEqual(view.snapshot_source, "files")
        self.assertTrue(view.discrepancies)


if __name__ == "__main__":
    unittest.main()
