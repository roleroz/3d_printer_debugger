"""The ``gcode`` MCP tool surface over a G-code index ([file_indexing.md §5.2]).

Every tool bounds its own response; queries are offset lookups plus a bounded re-read, never a full
scan.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from . import gcode
from .gcode import GcodeIndex
from .responses import ToolError, bounded

_MAX_WINDOW_LINES = 400
_MAX_LAYER_SPAN = 50


class GcodeTools:
    """The tools of the ``gcode`` server, over one indexed file."""

    def __init__(self, index: GcodeIndex, text: str) -> None:
        self._index = index
        self._text = text
        self._lines = text.splitlines()

    def index_status(self) -> dict[str, Any]:
        """Whether the index is ready, and progress if not (always ready once built)."""
        return bounded({"ready": True, "layers": len(self._index.layers)})

    def get_header(self) -> dict[str, Any]:
        """Slicer header and configuration block."""
        return bounded(
            {"header": self._index.header_text, "config": self._index.config_text}
        )

    def get_layer_table(self) -> dict[str, Any]:
        """Layer count; Z, byte range, and line range per layer."""
        table = [
            {
                "number": layer.number,
                "z": layer.z,
                "line_range": [layer.start_line, layer.end_line],
                "byte_range": [layer.start_offset, layer.end_offset],
                "extrusion": layer.extrusion,
            }
            for layer in self._index.layers
        ]
        return bounded({"count": len(table), "layers": table})

    def locate(
        self, z: float | None = None, line: int | None = None
    ) -> dict[str, Any]:
        """Layer containing a given Z height or line number."""
        if z is not None:
            layer = self._index.locate_layer_by_z(z)
        elif line is not None:
            layer = self._index.locate_layer_by_line(line)
        else:
            raise ToolError("locate needs a z or a line", "pass z= or line=")
        if layer is None:
            raise ToolError("no layer matches", "check the file has printed layers")
        return bounded({"number": layer.number, "z": layer.z,
                        "line_range": [layer.start_line, layer.end_line]})

    def summarise_layers(self, start: int, end: int) -> dict[str, Any]:
        """Features, speeds, and extrusion totals across a layer range."""
        if end - start > _MAX_LAYER_SPAN:
            raise ToolError(
                f"range of {end - start} layers exceeds {_MAX_LAYER_SPAN}",
                "request a smaller layer range",
            )
        summaries = [
            {
                "number": layer.number,
                "z": layer.z,
                "extrusion": layer.extrusion,
                "speed_range": [layer.speed_min, layer.speed_max],
            }
            for layer in self._index.layers
            if start <= layer.number <= end
        ]
        return bounded({"layers": summaries})

    def get_commands(self, line: int, window: int = 40) -> dict[str, Any]:
        """Raw commands in a bounded window around a line."""
        if window > _MAX_WINDOW_LINES:
            raise ToolError(
                f"window of {window} lines exceeds {_MAX_WINDOW_LINES}",
                "request a smaller window",
            )
        start = max(0, line - window // 2)
        end = min(len(self._lines), line + window // 2)
        return bounded(
            {"start_line": start, "end_line": end, "commands": self._lines[start:end]}
        )

    def get_region(self, obj: str, layer: int) -> dict[str, Any]:
        """Commands printing a given object on a given layer."""
        spans = [
            span
            for span in self._index.object_spans
            if span.obj.startswith(obj) and span.layer == layer
        ]
        if not spans:
            raise ToolError(
                f"no span for object {obj!r} on layer {layer}",
                "check the object name and layer via get_objects/get_layer_table",
            )
        span = spans[0]
        commands = self._lines[span.start_line : span.end_line]
        complete = len(commands) <= _MAX_WINDOW_LINES
        if not complete:
            commands = commands[:_MAX_WINDOW_LINES]
        return bounded(
            {
                "object": span.obj,
                "layer": layer,
                "start_line": span.start_line,
                "commands": commands,
                "note": None
                if complete
                else "region longer than the window; page the rest with get_commands by line",
            },
            complete=complete,
        )

    def get_state_at(self, line: int) -> dict[str, Any]:
        """Reconstructed machine state at a point."""
        state = gcode.reconstruct_state(self._index, self._text, line)
        return bounded({"line": line, "state": asdict(state)})

    def get_events(
        self, start_layer: int = 0, end_layer: int | None = None, kind: str | None = None
    ) -> dict[str, Any]:
        """Temperature, fan, retraction, Z-hop, and tool-change events in a layer range."""
        end = end_layer if end_layer is not None else len(self._index.layers)
        events = [
            {"kind": e.kind, "line": e.line, "detail": e.detail, "layer": e.layer}
            for e in self._index.events
            if start_layer <= e.layer <= end and (kind is None or e.kind == kind)
        ]
        return bounded({"events": events}, complete=len(events) < 500)

    def get_anomalies(self) -> dict[str, Any]:
        """Layers departing from their neighbours."""
        return bounded({"anomalous_layers": self._index.anomalies})

    def get_thumbnail(self, index: int = 0) -> dict[str, Any]:
        """An embedded preview's dimensions and data length."""
        if index >= len(self._index.thumbnails):
            raise ToolError(
                f"thumbnail {index} does not exist ({len(self._index.thumbnails)} present)",
                "request a lower index",
            )
        thumb = self._index.thumbnails[index]
        data = thumb.get("data", [])
        return bounded(
            {"dimensions": thumb.get("dimensions"),
             "data_lines": len(data) if isinstance(data, list) else 0}
        )
