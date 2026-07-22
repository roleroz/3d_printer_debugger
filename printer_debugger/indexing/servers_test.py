"""Tests for the project and gcode tool surfaces: answers, bounds, and index round-trip."""

from __future__ import annotations

import unittest
from pathlib import Path

from printer_debugger.indexing import gcode, index_format, responses
from printer_debugger.indexing.gcode_server import GcodeTools
from printer_debugger.indexing.project_server import ProjectTools
from printer_debugger.indexing.threemf import Project

_DATA = Path(__file__).resolve().parent / "testdata"


class ProjectToolsTest(unittest.TestCase):
    """The project tools answer within bounds and carry the bounded marker."""

    def setUp(self) -> None:
        self._stored: list[tuple[bytes, str]] = []
        self.project = Project(str(_DATA / "project.3mf"))
        self.tools = ProjectTools(
            self.project, artifact_sink=lambda data, name: self._store(data, name)
        )

    def tearDown(self) -> None:
        self.project.close()

    def _store(self, data: bytes, name: str) -> str:
        self._stored.append((data, name))
        return f"art://{name}"

    def test_settings_by_key(self) -> None:
        """A settings value is returned by key with the bounded marker."""
        result = self.tools.get_settings(key="printer_settings_id")
        self.assertEqual(result["value"], "Voron v2 350mm3 0.6 nozzle")
        self.assertIn("bounded", result)

    def test_modified_settings_reports_unavailable(self) -> None:
        """get_modified_settings reports the diff unavailable and gives the preset names."""
        result = self.tools.get_modified_settings()
        self.assertFalse(result["available"])
        self.assertIn("Voron", str(result["preset_names"]["printer"]))

    def test_render_returns_artifact_reference(self) -> None:
        """A render is stored via the sink and returned as a reference."""
        result = self.tools.get_object_render(view="iso", width=64, height=64)
        self.assertTrue(result["artifact"].startswith("art://"))
        self.assertTrue(self._stored[0][0].startswith(b"\x89PNG"))

    def test_dimensions_and_identity(self) -> None:
        """Dimensions and printer identity are answered."""
        self.assertGreater(self.tools.get_object_dimensions()["height"], 0)
        self.assertIn("nozzle_diameter", self.tools.get_printer_identity())


class GcodeToolsTest(unittest.TestCase):
    """The gcode tools answer within bounds and refuse oversized requests."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (_DATA / "project.gcode").read_text(encoding="utf-8")
        cls.index = gcode.build_index(cls.text)

    def setUp(self) -> None:
        self.tools = GcodeTools(self.index, self.text)

    def test_layer_table_and_locate(self) -> None:
        """The layer table lists layers and locate resolves a Z to one of them."""
        table = self.tools.get_layer_table()
        self.assertEqual(table["count"], len(self.index.layers))
        located = self.tools.locate(z=self.index.layers[3].z)
        self.assertEqual(located["number"], 3)

    def test_get_commands_window(self) -> None:
        """A bounded window of commands is returned around a line."""
        result = self.tools.get_commands(line=300, window=20)
        self.assertLessEqual(len(result["commands"]), 20)

    def test_oversized_window_refused(self) -> None:
        """An over-ceiling window is refused with narrowing guidance, not truncated."""
        with self.assertRaises(responses.ToolError):
            self.tools.get_commands(line=300, window=10_000)

    def test_oversized_layer_span_refused(self) -> None:
        """An over-ceiling layer span is refused."""
        with self.assertRaises(responses.ToolError):
            self.tools.summarise_layers(0, 999)

    def test_state_at_matches_index(self) -> None:
        """get_state_at returns a reconstructed state dict for a line."""
        result = self.tools.get_state_at(self.index.total_lines // 2)
        self.assertIn("z", result["state"])

    def test_region_by_object(self) -> None:
        """A region query returns the commands for an object on a layer."""
        span = self.index.object_spans[0]
        result = self.tools.get_region(obj=span.obj[:10], layer=span.layer)
        self.assertGreater(len(result["commands"]), 0)


class IndexFormatTest(unittest.TestCase):
    """The index serialises and round-trips."""

    def test_round_trip_preserves_layers_and_state(self) -> None:
        """Serialising and reloading an index preserves layers and their start state."""
        text = (_DATA / "project.gcode").read_text(encoding="utf-8")
        index = gcode.build_index(text)
        restored = index_format.loads(index_format.dumps(index))
        self.assertEqual(len(restored.layers), len(index.layers))
        self.assertAlmostEqual(
            restored.layers[2].state_at_start.z, index.layers[2].state_at_start.z
        )
        self.assertTrue(index_format.is_current(index_format.dumps(index)))


if __name__ == "__main__":
    unittest.main()
