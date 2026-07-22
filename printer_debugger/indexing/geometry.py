"""Mesh measurements: the numeric half of the intended-geometry surface.

Bounding box, height, footprint, volume, and overhang extents, as bounded numbers, for questions
like how tall a feature should be ([file_indexing.md §3.1](../../docs/design/file_indexing.md)).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class Measurements:
    """An object's intended dimensions."""

    min_xyz: tuple[float, float, float]
    max_xyz: tuple[float, float, float]
    width: float
    depth: float
    height: float
    volume: float
    footprint_area: float
    max_overhang_degrees: float


class NoUsableMeshError(Exception):
    """The mesh is empty or degenerate, so measurements and renders cannot be produced."""


def measure(vertices: np.ndarray, triangles: np.ndarray) -> Measurements:
    """Compute an object's measurements from its mesh."""
    if vertices.size == 0 or triangles.size == 0:
        raise NoUsableMeshError("mesh has no vertices or triangles")
    mn = vertices.min(axis=0)
    mx = vertices.max(axis=0)
    extents = mx - mn
    return Measurements(
        min_xyz=(float(mn[0]), float(mn[1]), float(mn[2])),
        max_xyz=(float(mx[0]), float(mx[1]), float(mx[2])),
        width=float(extents[0]),
        depth=float(extents[1]),
        height=float(extents[2]),
        volume=_volume(vertices, triangles),
        footprint_area=_footprint_area(vertices, triangles),
        max_overhang_degrees=_max_overhang(vertices, triangles),
    )


def _volume(vertices: np.ndarray, triangles: np.ndarray) -> float:
    """Signed-tetrahedron volume of a closed mesh, in cubic millimetres."""
    v0 = vertices[triangles[:, 0]]
    v1 = vertices[triangles[:, 1]]
    v2 = vertices[triangles[:, 2]]
    signed = np.einsum("ij,ij->i", v0, np.cross(v1, v2)) / 6.0
    return float(abs(signed.sum()))


def _footprint_area(vertices: np.ndarray, triangles: np.ndarray) -> float:
    """Downward-projected area of the mesh, approximating the plate footprint."""
    v0 = vertices[triangles[:, 0]][:, :2]
    v1 = vertices[triangles[:, 1]][:, :2]
    v2 = vertices[triangles[:, 2]][:, :2]
    cross = (v1[:, 0] - v0[:, 0]) * (v2[:, 1] - v0[:, 1]) - (v1[:, 1] - v0[:, 1]) * (
        v2[:, 0] - v0[:, 0]
    )
    # Sum only downward-facing (negative-Z-normal) projected areas ≈ the contact silhouette.
    normals = _face_normals(vertices, triangles)
    down = normals[:, 2] < 0
    return float(np.abs(cross[down]).sum() / 2.0)


def _max_overhang(vertices: np.ndarray, triangles: np.ndarray) -> float:
    """The steepest overhang angle in degrees (0 = vertical wall, 90 = flat downward face)."""
    normals = _face_normals(vertices, triangles)
    downward = normals[normals[:, 2] < 0]
    if downward.size == 0:
        return 0.0
    # Angle of the face below horizontal: arcsin(|normal_z|) for downward faces.
    angles = np.degrees(np.arcsin(np.clip(-downward[:, 2], 0.0, 1.0)))
    return float(angles.max())


def _face_normals(vertices: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    """Unit normals for each triangle."""
    v0 = vertices[triangles[:, 0]]
    v1 = vertices[triangles[:, 1]]
    v2 = vertices[triangles[:, 2]]
    normals = np.cross(v1 - v0, v2 - v0)
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    lengths[lengths == 0] = 1.0
    return normals / lengths
