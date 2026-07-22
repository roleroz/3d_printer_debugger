"""Tests for discrepancy detection: each kind detected, matching values not reported."""

from __future__ import annotations

import unittest
from pathlib import Path

from printer_debugger.kb import klipper_config
from printer_debugger.kb.config_import import read_and_merge
from printer_debugger.kb.discrepancies import detect_between, detect_within
from printer_debugger.kb.models import DiscrepancyKind

_TRIDENT = Path(__file__).resolve().parent / "testdata" / "trident"


class WithinConfigDiscrepancyTest(unittest.TestCase):
    """The two single-config discrepancy kinds are detected in the fixture."""

    def setUp(self) -> None:
        self.config = klipper_config.parse(read_and_merge(_TRIDENT))
        self.found = detect_within(self.config)

    def test_saved_supersedes_file(self) -> None:
        """The stale file extruder pid_kp (20.0) vs SAVE_CONFIG (25.9) is flagged."""
        matches = [
            d
            for d in self.found
            if d.kind == DiscrepancyKind.SAVED_SUPERSEDES_FILE and d.key == "pid_kp"
        ]
        self.assertTrue(any(d.left == "20.000" and d.right == "25.900" for d in matches))

    def test_commented_differs_from_active(self) -> None:
        """The commented bed pid_kp (44.0) vs effective (54.201) is flagged."""
        matches = [
            d
            for d in self.found
            if d.kind == DiscrepancyKind.COMMENTED_DIFFERS_FROM_ACTIVE
            and d.section == "heater_bed"
        ]
        self.assertTrue(any(d.left == "44.000" and d.right == "54.201" for d in matches))

    def test_matching_value_not_reported(self) -> None:
        """A config with no disagreement produces no discrepancies."""
        clean = klipper_config.parse("[printer]\nkinematics: corexy\n")
        self.assertEqual(detect_within(clean), [])


class BetweenConfigDiscrepancyTest(unittest.TestCase):
    """Files-differ-from-live is detected when a live config disagrees."""

    def test_files_differ_from_live(self) -> None:
        """A key whose live value differs from the file value is flagged, matches are not."""
        files = klipper_config.parse("[printer]\nmax_accel: 3000\nmax_velocity: 300\n")
        live = klipper_config.parse("[printer]\nmax_accel: 5000\nmax_velocity: 300\n")
        found = detect_between(files, live)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, DiscrepancyKind.FILES_DIFFER_FROM_LIVE)
        self.assertEqual(found[0].key, "max_accel")
        self.assertEqual((found[0].left, found[0].right), ("3000", "5000"))


if __name__ == "__main__":
    unittest.main()
