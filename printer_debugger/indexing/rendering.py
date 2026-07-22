"""Headless software rasteriser for intended-geometry renders.

A shaded orthographic projection of the mesh from a viewpoint, rendered with numpy and encoded to
PNG with the standard library — no GL, so it works inside the Bazel sandbox and keeps the image
small. A shaded intended-shape view is what the vision model sets against a photo
([file_indexing.md §3.1](../../docs/design/file_indexing.md)).
"""

from __future__ import annotations

import struct
import zlib

import numpy as np

# Named viewpoints as (azimuth, elevation) degrees. Arbitrary angles are also accepted.
NAMED_VIEWS: dict[str, tuple[float, float]] = {
    "front": (0.0, 0.0),
    "back": (180.0, 0.0),
    "left": (90.0, 0.0),
    "right": (-90.0, 0.0),
    "top": (0.0, 90.0),
    "bottom": (0.0, -90.0),
    "iso": (-45.0, 35.264),
}
_AMBIENT = 0.25
_BACKGROUND = 32
_OBJECT_RGB = np.array([120, 170, 235], dtype=float)


def render(
    vertices: np.ndarray,
    triangles: np.ndarray,
    view: str = "iso",
    width: int = 256,
    height: int = 256,
) -> bytes:
    """Render a named view of a mesh to PNG bytes."""
    if view not in NAMED_VIEWS:
        raise ValueError(f"unknown view {view!r}; known: {sorted(NAMED_VIEWS)}")
    azimuth, elevation = NAMED_VIEWS[view]
    return render_angle(vertices, triangles, azimuth, elevation, width, height)


def render_angle(
    vertices: np.ndarray,
    triangles: np.ndarray,
    azimuth_deg: float,
    elevation_deg: float,
    width: int = 256,
    height: int = 256,
) -> bytes:
    """Render a mesh from an arbitrary viewpoint to PNG bytes."""
    if vertices.size == 0 or triangles.size == 0:
        raise ValueError("cannot render an empty mesh")
    right, up, forward = _basis(np.radians(azimuth_deg), np.radians(elevation_deg))

    screen_x = vertices @ right
    screen_y = vertices @ up
    depth = vertices @ forward

    image = _rasterise(
        screen_x, screen_y, depth, vertices, triangles, forward, width, height
    )
    return _encode_png(image)


def _basis(azimuth: float, elevation: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    forward = np.array(
        [
            np.cos(elevation) * np.cos(azimuth),
            np.cos(elevation) * np.sin(azimuth),
            np.sin(elevation),
        ]
    )
    world_up = np.array([0.0, 0.0, 1.0])
    if abs(forward[2]) > 0.999:
        world_up = np.array([0.0, 1.0, 0.0])
    right = np.cross(world_up, forward)
    right /= np.linalg.norm(right)
    up = np.cross(forward, right)
    return right, up, forward


def _rasterise(
    sx: np.ndarray,
    sy: np.ndarray,
    depth: np.ndarray,
    vertices: np.ndarray,
    triangles: np.ndarray,
    forward: np.ndarray,
    width: int,
    height: int,
) -> np.ndarray:
    margin = 0.08
    span_x = sx.max() - sx.min() or 1.0
    span_y = sy.max() - sy.min() or 1.0
    scale = (1 - 2 * margin) * min(width / span_x, height / span_y)
    px = (sx - sx.min()) * scale + (width - span_x * scale) / 2
    py = height - ((sy - sy.min()) * scale + (height - span_y * scale) / 2)

    image = np.full((height, width, 3), _BACKGROUND, dtype=np.uint8)
    zbuffer = np.full((height, width), np.inf)

    normals = _face_normals(vertices, triangles)
    shade = _AMBIENT + (1 - _AMBIENT) * np.clip(normals @ forward, 0.0, 1.0)

    for i in range(triangles.shape[0]):
        a, b, c = triangles[i]
        _fill_triangle(
            image,
            zbuffer,
            (px[a], py[a], depth[a]),
            (px[b], py[b], depth[b]),
            (px[c], py[c], depth[c]),
            float(shade[i]),
        )
    return image


def _fill_triangle(image, zbuffer, va, vb, vc, shade: float) -> None:
    xs = [va[0], vb[0], vc[0]]
    ys = [va[1], vb[1], vc[1]]
    min_x = max(int(np.floor(min(xs))), 0)
    max_x = min(int(np.ceil(max(xs))), image.shape[1] - 1)
    min_y = max(int(np.floor(min(ys))), 0)
    max_y = min(int(np.ceil(max(ys))), image.shape[0] - 1)
    if min_x > max_x or min_y > max_y:
        return
    denom = (vb[1] - vc[1]) * (va[0] - vc[0]) + (vc[0] - vb[0]) * (va[1] - vc[1])
    if abs(denom) < 1e-9:
        return
    ys_grid, xs_grid = np.mgrid[min_y : max_y + 1, min_x : max_x + 1]
    w0 = ((vb[1] - vc[1]) * (xs_grid - vc[0]) + (vc[0] - vb[0]) * (ys_grid - vc[1])) / denom
    w1 = ((vc[1] - va[1]) * (xs_grid - vc[0]) + (va[0] - vc[0]) * (ys_grid - vc[1])) / denom
    w2 = 1 - w0 - w1
    inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
    if not inside.any():
        return
    depth = w0 * va[2] + w1 * vb[2] + w2 * vc[2]
    region_z = zbuffer[min_y : max_y + 1, min_x : max_x + 1]
    visible = inside & (depth < region_z)
    if not visible.any():
        return
    region_z[visible] = depth[visible]
    colour = np.clip(_OBJECT_RGB * shade, 0, 255).astype(np.uint8)
    region_img = image[min_y : max_y + 1, min_x : max_x + 1]
    region_img[visible] = colour


def _face_normals(vertices: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    v0 = vertices[triangles[:, 0]]
    v1 = vertices[triangles[:, 1]]
    v2 = vertices[triangles[:, 2]]
    normals = np.cross(v1 - v0, v2 - v0)
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    lengths[lengths == 0] = 1.0
    return normals / lengths


def _encode_png(image: np.ndarray) -> bytes:
    """Encode an (H, W, 3) uint8 array as a PNG, using only the standard library."""
    height, width, _ = image.shape
    raw = bytearray()
    for row in image:
        raw.append(0)  # filter type 0 (none) per scanline
        raw.extend(row.tobytes())
    compressed = zlib.compress(bytes(raw), level=6)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
