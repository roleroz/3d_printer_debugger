"""Tests for .3mf parsing, geometry measurements, and rendering against the real fixture."""

from __future__ import annotations

import unittest
from pathlib import Path

from printer_debugger.indexing import geometry, rendering
from printer_debugger.indexing.threemf import Project

_PROJECT = Path(__file__).resolve().parent / "testdata" / "project.3mf"


class ThreeMfTest(unittest.TestCase):
    """Parsing the real OrcaSlicer project recovers its settings, objects, and mesh."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.project = Project(str(_PROJECT))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.project.close()

    def test_preset_names_present_not_diff(self) -> None:
        """Preset names are read; the project records the lineage, not the overridden set."""
        presets = self.project.preset_names()
        self.assertIn("Voron", str(presets["printer"]))
        self.assertIsNotNone(presets["print"])

    def test_printer_identity(self) -> None:
        """The identification inputs — nozzle and printable area — are extracted."""
        identity = self.project.printer_identity()
        self.assertEqual(identity["printer_settings_id"], "Voron v2 350mm3 0.6 nozzle")
        self.assertIsNotNone(identity["printable_area"])

    def test_objects_and_thumbnails(self) -> None:
        """The named object and embedded thumbnails are found."""
        objects = self.project.objects()
        self.assertTrue(any("rack_cable_manager" in o.name for o in objects))
        self.assertTrue(self.project.thumbnails())

    def test_mesh_loads(self) -> None:
        """The mesh loads as non-empty vertex and triangle arrays."""
        vertices, triangles = self.project.load_mesh()
        self.assertGreater(vertices.shape[0], 100)
        self.assertGreater(triangles.shape[0], 100)
        self.assertEqual(vertices.shape[1], 3)
        self.assertEqual(triangles.shape[1], 3)

    def test_plate_layout_has_footprint(self) -> None:
        """Plate layout reports an XY footprint for the object."""
        layout = self.project.plate_layout()
        self.assertTrue(layout)
        self.assertIn("min_xy", layout[0])


class GeometryTest(unittest.TestCase):
    """Measurements are plausible for a real part."""

    @classmethod
    def setUpClass(cls) -> None:
        with Project(str(_PROJECT)) as project:
            cls.vertices, cls.triangles = project.load_mesh()

    def test_measurements_plausible(self) -> None:
        """Height matches the header's max_z (~5.1mm) and volume is positive."""
        m = geometry.measure(self.vertices, self.triangles)
        self.assertGreater(m.volume, 0)
        self.assertGreater(m.width, 0)
        self.assertGreater(m.height, 0)
        self.assertGreaterEqual(m.max_overhang_degrees, 0.0)

    def test_empty_mesh_reports_unusable(self) -> None:
        """An empty mesh raises NoUsableMeshError rather than returning nonsense."""
        import numpy as np

        with self.assertRaises(geometry.NoUsableMeshError):
            geometry.measure(np.empty((0, 3)), np.empty((0, 3), dtype=int))


class RenderingTest(unittest.TestCase):
    """Rendering produces a valid PNG of the requested size for each named view."""

    @classmethod
    def setUpClass(cls) -> None:
        with Project(str(_PROJECT)) as project:
            cls.vertices, cls.triangles = project.load_mesh()

    def test_named_views_render_valid_png(self) -> None:
        """Each named view yields PNG bytes; the render is not blank."""
        for view in ("front", "top", "iso"):
            png = rendering.render(self.vertices, self.triangles, view=view, width=96, height=96)
            self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"), f"{view} is not a PNG")
            self.assertGreater(len(png), 100)

    def test_unknown_view_rejected(self) -> None:
        """An unknown view name is rejected."""
        with self.assertRaises(ValueError):
            rendering.render(self.vertices, self.triangles, view="sideways")

    def test_empty_mesh_render_rejected(self) -> None:
        """Rendering an empty mesh raises rather than producing a blank image silently."""
        import numpy as np

        with self.assertRaises(ValueError):
            rendering.render_angle(np.empty((0, 3)), np.empty((0, 3), dtype=int), 0, 0)


if __name__ == "__main__":
    unittest.main()
