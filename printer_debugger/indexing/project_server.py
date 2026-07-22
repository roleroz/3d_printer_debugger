"""The ``project`` MCP tool surface over a ``.3mf`` index.

Bounded, targeted views over the project ([file_indexing.md §5.1]). ``get_object_render`` and
``get_object_dimensions`` are the intended-geometry surface: the render is what the model sets
against a photo, the dimensions answer "what should this measure".
"""

from __future__ import annotations

from typing import Any, Callable

from . import geometry, rendering
from .geometry import NoUsableMeshError
from .responses import ToolError, bounded
from .threemf import Project

# A callback the composition root supplies to persist a rendered image and return a reference.
ArtifactSink = Callable[[bytes, str], str]


class ProjectTools:
    """The tools of the ``project`` server, over one project."""

    def __init__(self, project: Project, artifact_sink: ArtifactSink | None = None) -> None:
        self._project = project
        self._sink = artifact_sink

    def get_settings(self, group: str | None = None, key: str | None = None) -> dict[str, Any]:
        """Process, filament, or printer settings, whole or by key."""
        settings = self._project.settings()
        if key is not None:
            return bounded({"key": key, "value": settings.get(key)})
        if group is not None:
            subset = {k: v for k, v in settings.items() if k.startswith(group)}
            return bounded({"group": group, "settings": subset})
        # The whole settings map is large; return the keys and require a group or key to drill in.
        return bounded(
            {"keys": sorted(settings.keys()), "note": "request a key or group for values"}
        )

    def get_modified_settings(self) -> dict[str, Any]:
        """What differs from the preset — unavailable without the preset library ([§3])."""
        return bounded(
            {
                "available": False,
                "preset_names": self._project.preset_names(),
                "reason": "OrcaSlicer does not record the diff; the preset library is future work",
            }
        )

    def get_objects(self) -> dict[str, Any]:
        """Objects, counts, and placement."""
        objects = [{"name": o.name, "model": o.model_path} for o in self._project.objects()]
        return bounded({"count": len(objects), "objects": objects})

    def get_plate_layout(self) -> dict[str, Any]:
        """Object footprints in plate coordinates, for photo matching."""
        return bounded({"plate": self._project.plate_layout()})

    def get_printer_identity(self) -> dict[str, Any]:
        """Preset name, nozzle diameter, printable area, limits — the session-binding fields."""
        return bounded(self._project.printer_identity())

    def get_object_dimensions(self, object_index: int = 0) -> dict[str, Any]:
        """An object's bounding box, height, footprint, volume, and overhang extents."""
        vertices, triangles = self._load(object_index)
        try:
            m = geometry.measure(vertices, triangles)
        except NoUsableMeshError as exc:
            raise ToolError(str(exc), "no measurements available for this object") from exc
        return bounded(
            {
                "min_xyz": m.min_xyz,
                "max_xyz": m.max_xyz,
                "width": m.width,
                "depth": m.depth,
                "height": m.height,
                "volume": m.volume,
                "footprint_area": m.footprint_area,
                "max_overhang_degrees": m.max_overhang_degrees,
            }
        )

    def get_object_render(
        self, view: str = "iso", object_index: int = 0, width: int = 256, height: int = 256
    ) -> dict[str, Any]:
        """An object's intended geometry rendered from a viewpoint, as an artifact reference."""
        vertices, triangles = self._load(object_index)
        try:
            png = rendering.render(vertices, triangles, view=view, width=width, height=height)
        except ValueError as exc:
            raise ToolError(str(exc), "choose a known view and a non-empty object") from exc
        reference = self._sink(png, f"render_{view}.png") if self._sink else None
        return bounded(
            {"view": view, "width": width, "height": height, "artifact": reference,
             "png_bytes": None if self._sink else len(png)}
        )

    def get_thumbnail(self, index: int = 0) -> dict[str, Any]:
        """A preview image as an artifact reference."""
        thumbnails = self._project.thumbnails()
        if index >= len(thumbnails):
            raise ToolError(
                f"thumbnail {index} does not exist ({len(thumbnails)} present)",
                "request a lower index",
            )
        name, data = thumbnails[index]
        reference = self._sink(data, name) if self._sink else None
        return bounded({"name": name, "artifact": reference, "bytes": len(data)})

    def _load(self, object_index: int):
        objects = self._project.objects()
        if not objects:
            raise ToolError("project has no objects", "nothing to measure or render")
        if object_index >= len(objects):
            raise ToolError(
                f"object {object_index} does not exist ({len(objects)} present)",
                "request a lower object index",
            )
        return self._project.load_mesh(objects[object_index].model_path)
