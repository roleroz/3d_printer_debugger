"""Tests for document sectioning: heading-level detection, split, and preamble."""

from __future__ import annotations

import unittest
from pathlib import Path

from printer_debugger.kb.sectioning import section_level, split_sections

_EXAMPLE = Path(__file__).resolve().parents[2] / "docs" / "examples" / "printer_definition.md"


class SectionLevelTest(unittest.TestCase):
    """The section level is the most common heading level below a single title."""

    def test_title_then_common_subheadings(self) -> None:
        """A lone top heading is the title; the common level below it is the section level."""
        lines = ["# Title", "## A", "text", "## B", "### deep"]
        self.assertEqual(section_level(lines), 2)

    def test_all_top_level_headings(self) -> None:
        """When every heading is level 1, that is the section level."""
        lines = ["# A", "x", "# B", "y"]
        self.assertEqual(section_level(lines), 1)

    def test_no_headings_returns_none(self) -> None:
        """A document with no headings has no section level."""
        self.assertIsNone(section_level(["just text", "more text"]))


class SplitSectionsTest(unittest.TestCase):
    """Splitting produces a preamble plus the sections at the section level."""

    def test_preamble_and_sections(self) -> None:
        """Content before the first section heading is preamble; sections follow."""
        doc = "# Title\nintro\n## One\na\n## Two\nb\n"
        result = split_sections(doc)
        self.assertIn("intro", result.preamble)
        self.assertEqual([s.heading for s in result.sections], ["One", "Two"])
        self.assertEqual(result.sections[0].index, 0)
        self.assertIn("a", result.sections[0].text)

    def test_example_document_has_two_printers_worth_of_sections(self) -> None:
        """The example document splits into its eight ## sections with the two printers present."""
        result = split_sections(_EXAMPLE.read_text(encoding="utf-8"))
        headings = [s.heading for s in result.sections]
        self.assertIn("Voron Trident 300", headings)
        self.assertIn("Voron Switchwire", headings)
        self.assertIn("Slicer", headings)
        self.assertEqual(len(result.sections), 8)


if __name__ == "__main__":
    unittest.main()
