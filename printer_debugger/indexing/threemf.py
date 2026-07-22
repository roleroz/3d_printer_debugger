"""Parse an OrcaSlicer ``.3mf`` project.

A ``.3mf`` is a zip of XML, JSON, and mesh geometry. Settings, metadata, and thumbnails are read
into a small structure; the mesh is read on demand for the geometry tools and never folded into
the index ([file_indexing.md §3](../../docs/design/file_indexing.md)). An OrcaSlicer project does
not record which settings were overridden and names its presets without embedding them, so this
exposes the preset names and resolved values; the modified-from-preset diff is future work.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass

import numpy as np

_SETTINGS = "Metadata/project_settings.config"
_MODEL_SETTINGS = "Metadata/model_settings.config"


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """An object on the plate: its name and the mesh resource that holds its geometry."""

    name: str
    model_path: str


class Project:
    """Read access to a ``.3mf`` project's settings, metadata, thumbnails, and meshes."""

    def __init__(self, path: str) -> None:
        self._zip = zipfile.ZipFile(path)
        self._settings_cache: dict | None = None

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> "Project":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- settings --------------------------------------------------------------------------

    def settings(self) -> dict:
        """The full resolved settings map from ``project_settings.config``."""
        if self._settings_cache is None:
            self._settings_cache = json.loads(self._zip.read(_SETTINGS))
        return self._settings_cache

    def preset_names(self) -> dict[str, object]:
        """The named presets the project derives from (not embedded — see the module docstring)."""
        settings = self.settings()
        return {
            "print": settings.get("print_settings_id"),
            "filament": settings.get("filament_settings_id"),
            "printer": settings.get("printer_settings_id"),
        }

    def printer_identity(self) -> dict[str, object]:
        """The identification inputs: preset name, nozzle diameter, printable area, limits."""
        settings = self.settings()
        return {
            "printer_settings_id": settings.get("printer_settings_id"),
            "nozzle_diameter": settings.get("nozzle_diameter"),
            "printable_area": settings.get("printable_area"),
            "printable_height": settings.get("printable_height"),
            "max_print_speed": settings.get("max_print_speed"),
        }

    def get_setting(self, key: str) -> object:
        """Return a single setting value, or None."""
        return self.settings().get(key)

    # -- objects and plate -----------------------------------------------------------------

    def objects(self) -> list[ObjectInfo]:
        """List the objects on the plate, by name, with the mesh resource for each."""
        model_paths = sorted(
            name for name in self._zip.namelist() if name.startswith("3D/Objects/")
        )
        names = self._object_names()
        result: list[ObjectInfo] = []
        for i, model_path in enumerate(model_paths):
            name = names[i] if i < len(names) else model_path.rsplit("/", 1)[-1]
            result.append(ObjectInfo(name=name, model_path=model_path))
        if not result and names:
            # Single combined model file: attribute all named objects to 3dmodel.model.
            for name in names:
                result.append(ObjectInfo(name=name, model_path="3D/3dmodel.model"))
        return result

    def _object_names(self) -> list[str]:
        try:
            raw = self._zip.read(_MODEL_SETTINGS)
        except KeyError:
            return []
        root = ET.fromstring(raw)
        names: list[str] = []
        for obj in root.iter():
            if _localname(obj.tag) != "object":
                continue
            for meta in obj:
                if _localname(meta.tag) == "metadata" and meta.get("key") == "name":
                    names.append(meta.get("value", ""))
                    break
        return names

    def plate_layout(self) -> list[dict[str, object]]:
        """Object footprints in plate coordinates, for photo-to-plate matching.

        Footprint is the object's XY bounding box; a single plate is assumed ([§3]).
        """
        layout: list[dict[str, object]] = []
        for info in self.objects():
            vertices, _ = self.load_mesh(info.model_path)
            if vertices.size == 0:
                continue
            xy_min = vertices[:, :2].min(axis=0)
            xy_max = vertices[:, :2].max(axis=0)
            layout.append(
                {
                    "object": info.name,
                    "min_xy": [float(xy_min[0]), float(xy_min[1])],
                    "max_xy": [float(xy_max[0]), float(xy_max[1])],
                }
            )
        return layout

    # -- thumbnails and mesh ---------------------------------------------------------------

    def thumbnails(self) -> list[tuple[str, bytes]]:
        """The embedded preview PNGs, as (name, bytes)."""
        return [
            (name.rsplit("/", 1)[-1], self._zip.read(name))
            for name in self._zip.namelist()
            if name.startswith("Metadata/") and name.endswith(".png")
        ]

    def load_mesh(self, model_path: str | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Load a mesh's vertices (Nx3) and triangles (Mx3) as numpy arrays.

        Read on demand from the retained artifact, never held in the index.
        """
        if model_path is None:
            objects = self.objects()
            if not objects:
                return np.empty((0, 3)), np.empty((0, 3), dtype=int)
            model_path = objects[0].model_path
        root = ET.fromstring(self._zip.read(model_path))
        vertices: list[tuple[float, float, float]] = []
        triangles: list[tuple[int, int, int]] = []
        for element in root.iter():
            local = _localname(element.tag)
            if local == "vertex":
                vertices.append(
                    (float(element.get("x", 0)), float(element.get("y", 0)),
                     float(element.get("z", 0)))
                )
            elif local == "triangle":
                triangles.append(
                    (int(element.get("v1", 0)), int(element.get("v2", 0)),
                     int(element.get("v3", 0)))
                )
        return (
            np.array(vertices, dtype=float) if vertices else np.empty((0, 3)),
            np.array(triangles, dtype=int) if triangles else np.empty((0, 3), dtype=int),
        )
