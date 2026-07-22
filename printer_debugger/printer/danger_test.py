"""Tests for danger classification: each condition, macro expansion, and unknown-command refusal."""

from __future__ import annotations

import unittest

from printer_debugger.printer import danger
from printer_debugger.printer.danger import Limits, classify

_CONFIG = """
[stepper_x]
position_min: 0
position_max: 300
[stepper_y]
position_min: 0
position_max: 300
[stepper_z]
position_min: 0
position_max: 250
[extruder]
min_extrude_temp: 170
[gcode_macro CLEAN_NOZZLE]
gcode:
    G28
    G1 X10 Y10 Z5 F3000
[gcode_macro BAD_MACRO]
gcode:
    TOTALLY_UNKNOWN_COMMAND
[gcode_macro COLD_PURGE]
gcode:
    G1 E5 F100
"""


class DangerTest(unittest.TestCase):
    """The classifier flags dangerous commands and refuses unknown ones."""

    def setUp(self) -> None:
        self.macros = danger.extract_macros(_CONFIG)
        self.limits = danger.extract_limits(_CONFIG)

    def test_limits_and_macros_extracted(self) -> None:
        """Axis limits and macro bodies are pulled from the config snapshot."""
        self.assertEqual(self.limits.position_max["X"], 300.0)
        self.assertIn("CLEAN_NOZZLE", self.macros)
        self.assertIn("G28", self.macros["CLEAN_NOZZLE"])

    def test_movement_without_homing_flagged(self) -> None:
        """A move on an unhomed axis is flagged."""
        result = classify("G1 X100 Y100", self.macros, self.limits, homed_axes="")
        self.assertIn("movement without homing", result.flags)

    def test_beyond_limits_flagged(self) -> None:
        """A target outside the configured axis limits is flagged."""
        result = classify("G1 X400", self.macros, self.limits, homed_axes="XYZ")
        self.assertIn("beyond configured limits", result.flags)

    def test_cold_extrusion_flagged(self) -> None:
        """An extrusion below the minimum extrude temperature is flagged."""
        result = classify("G1 E5", self.macros, self.limits, hotend_temp=25.0)
        self.assertIn("extrusion below safe temperature", result.flags)

    def test_heater_safety_flagged(self) -> None:
        """A command touching heater safety parameters is flagged."""
        result = classify("SET_HEATER_TEMPERATURE max_temp=350", self.macros, self.limits)
        self.assertIn("heater safety parameters", result.flags)

    def test_known_calibration_command_not_refused(self) -> None:
        """A built-in calibration command is recognised, not refused as unknown."""
        result = classify("SHAPER_CALIBRATE", self.macros, self.limits)
        self.assertFalse(result.refused)

    def test_unknown_command_refused(self) -> None:
        """A command that is neither categorised nor a defined macro is refused outright."""
        result = classify("FROBNICATE_THE_WIDGET", self.macros, self.limits)
        self.assertTrue(result.refused)

    def test_macro_expanded_and_classified_by_body(self) -> None:
        """A macro is judged by its body: CLEAN_NOZZLE homes then moves, so it is not refused."""
        result = classify("CLEAN_NOZZLE", self.macros, self.limits, homed_axes="XYZ")
        self.assertFalse(result.refused)

    def test_macro_containing_unknown_is_refused(self) -> None:
        """A macro whose body contains an unknown command is refused as a whole."""
        result = classify("BAD_MACRO", self.macros, self.limits)
        self.assertTrue(result.refused)

    def test_macro_expands_to_flag(self) -> None:
        """A macro that cold-extrudes surfaces the cold-extrusion flag from its body."""
        result = classify("COLD_PURGE", self.macros, self.limits, hotend_temp=25.0)
        self.assertIn("extrusion below safe temperature", result.flags)


if __name__ == "__main__":
    unittest.main()
