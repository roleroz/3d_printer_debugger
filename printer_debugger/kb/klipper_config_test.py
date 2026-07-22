"""Tests for the Klipper config parser against a real config tree."""

from __future__ import annotations

import unittest
from pathlib import Path

from printer_debugger.kb import klipper_config
from printer_debugger.kb.config_import import read_and_merge

_TRIDENT = Path(__file__).resolve().parent / "testdata" / "trident"


class KlipperConfigTest(unittest.TestCase):
    """Parsing keeps active, saved, and commented values distinct."""

    def setUp(self) -> None:
        self.merged = read_and_merge(_TRIDENT)
        self.config = klipper_config.parse(self.merged)

    def test_includes_are_expanded(self) -> None:
        """The [include bed.cfg] directive pulls in the bed section's active values."""
        self.assertIn("heater_bed", self.config.file_sections)
        self.assertEqual(self.config.file_sections["heater_bed"]["max_temp"], "110")

    def test_active_values_parsed(self) -> None:
        """A normal key: value pair is captured under its section."""
        self.assertEqual(self.config.file_sections["printer"]["kinematics"], "corexy")
        self.assertEqual(self.config.file_sections["extruder"]["rotation_distance"], "22.6789")

    def test_save_config_block_parsed_separately(self) -> None:
        """The SAVE_CONFIG block is captured in saved_sections, not file_sections."""
        self.assertEqual(self.config.saved_sections["extruder"]["pid_kp"], "25.900")
        self.assertEqual(self.config.saved_sections["heater_bed"]["pid_kp"], "54.201")

    def test_effective_prefers_saved(self) -> None:
        """The effective value for a key present in both is the SAVE_CONFIG one."""
        self.assertEqual(self.config.effective()["extruder"]["pid_kp"], "25.900")

    def test_commented_values_kept_and_prose_ignored(self) -> None:
        """Commented key: value pairs are retained; prose comments are not."""
        keys = {(c.section, c.key) for c in self.config.comments}
        self.assertIn(("heater_bed", "pid_kp"), keys)
        self.assertNotIn(("heater_bed", "This"), keys)

    def test_gear_ratio_value_keeps_internal_colon(self) -> None:
        """A value containing a colon splits only on the first separator."""
        config = klipper_config.parse("[extruder]\ngear_ratio: 50:10\n")
        self.assertEqual(config.file_sections["extruder"]["gear_ratio"], "50:10")


if __name__ == "__main__":
    unittest.main()
