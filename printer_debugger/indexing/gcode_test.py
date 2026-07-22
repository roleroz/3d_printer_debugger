"""Tests for the G-code indexer against the real fixture and hand-built arc/relative cases."""

from __future__ import annotations

import unittest
from pathlib import Path

from printer_debugger.indexing import gcode
from printer_debugger.indexing.gcode_state import MachineState, apply, parse_command

_GCODE = Path(__file__).resolve().parent / "testdata" / "project.gcode"


class RealFixtureTest(unittest.TestCase):
    """Indexing the real OrcaSlicer file recovers its structure."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _GCODE.read_text(encoding="utf-8")
        cls.index = gcode.build_index(cls.text)

    def test_header_and_config_captured(self) -> None:
        """The header and config blocks are captured verbatim."""
        self.assertIn("OrcaSlicer", self.index.header_text)
        self.assertIn("layer_height = 0.3", self.index.config_text)

    def test_layers_monotonic_and_plausible(self) -> None:
        """Layers are found, their Z increases monotonically, and the count matches the header."""
        self.assertGreaterEqual(len(self.index.layers), 15)
        zs = [layer.z for layer in self.index.layers]
        self.assertEqual(zs, sorted(zs))
        self.assertEqual(len(zs), len(set(zs)), "each layer has a distinct Z")

    def test_object_map_from_markers(self) -> None:
        """The slicer's object markers produce object spans."""
        names = {span.obj for span in self.index.object_spans}
        self.assertTrue(any("rack_cable_manager" in name for name in names))

    def test_events_present(self) -> None:
        """Temperature and fan events are recorded from the start G-code."""
        kinds = {event.kind for event in self.index.events}
        self.assertIn("temperature", kinds)
        self.assertIn("fan", kinds)

    def test_thumbnails_extracted(self) -> None:
        """At least one embedded thumbnail is captured."""
        self.assertTrue(self.index.thumbnails)

    def test_state_reconstruction_matches_full_replay(self) -> None:
        """Replaying from a layer snapshot equals a full replay, at several points."""
        total = self.index.total_lines
        for target in (total // 4, total // 2, (3 * total) // 4):
            fast = gcode.reconstruct_state(self.index, self.text, target)
            slow = gcode.full_replay(self.text, target)
            self.assertAlmostEqual(fast.x, slow.x, places=4)
            self.assertAlmostEqual(fast.z, slow.z, places=4)
            self.assertAlmostEqual(fast.extrusion_total, slow.extrusion_total, places=3)

    def test_locate_by_z_and_line(self) -> None:
        """Locate resolves a Z to a layer and a line to its containing layer."""
        layer = self.index.layers[5]
        self.assertEqual(self.index.locate_layer_by_z(layer.z).number, 5)
        mid_line = (layer.start_line + layer.end_line) // 2
        self.assertEqual(self.index.locate_layer_by_line(mid_line).number, 5)


class ArcAndRelativeTest(unittest.TestCase):
    """Arc moves and relative modes are interpreted rather than assumed linear-absolute."""

    def test_relative_extrusion_and_arc_end_position(self) -> None:
        """A G91/M83 arc file yields correct end coordinates and extrusion total."""
        text = "\n".join(
            [
                "G90",
                "M82",
                "G1 X10 Y10 E1 F1200",
                "M83",
                "G3 X20 Y10 I5 J0 E0.5",  # arc, relative extrusion
                "G1 X25 Y10 E0.3",
            ]
        )
        state = gcode.full_replay(text, 10)
        self.assertAlmostEqual(state.x, 25.0)
        self.assertAlmostEqual(state.y, 10.0)
        # Extrusion: 1 (absolute) + 0.5 + 0.3 (relative) = 1.8 total.
        self.assertAlmostEqual(state.extrusion_total, 1.8, places=4)

    def test_g92_resets_without_moving(self) -> None:
        """G92 resets the extrusion coordinate but does not add to the physical total."""
        state = MachineState()
        for line in ["G90", "M82", "G1 X5 E5", "G92 E0", "G1 X10 E5"]:
            command = parse_command(line)
            if command:
                state = apply(state, command)
        self.assertAlmostEqual(state.extrusion_total, 10.0, places=4)


if __name__ == "__main__":
    unittest.main()
