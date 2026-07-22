"""G-code single-pass indexer.

One forward pass emitting a structure whose entries carry byte offsets, plus a full machine-state
snapshot at the start of every layer so state at any point is reconstructed by replaying from the
containing layer's start ([file_indexing.md §4](../../docs/design/file_indexing.md)). Layers are
inferred from extruding-Z increases, since this slicer emits no explicit layer-change comments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from .gcode_state import MachineState, apply, is_extruding_move, parse_command

FORMAT_VERSION = 1
_Z_EPS = 1e-4


@dataclass(frozen=True, slots=True)
class Layer:
    """One printed layer: its Z, byte and line ranges, and the state at its start."""

    number: int
    z: float
    start_line: int
    end_line: int
    start_offset: int
    end_offset: int
    state_at_start: MachineState
    extrusion: float  # filament extruded within the layer.
    features: tuple[str, ...]
    speed_min: float
    speed_max: float


@dataclass(frozen=True, slots=True)
class Event:
    """A notable command: a temperature/fan change, retraction, Z-hop, or tool change."""

    kind: str
    line: int
    offset: int
    detail: str
    layer: int


@dataclass(frozen=True, slots=True)
class ObjectSpan:
    """A contiguous run of lines printing one object, attributed by slicer markers."""

    obj: str
    layer: int
    start_line: int
    end_line: int


@dataclass(slots=True)
class GcodeIndex:
    """The built index over a G-code file."""

    total_bytes: int
    total_lines: int
    header_text: str
    config_text: str
    layers: list[Layer] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    object_spans: list[ObjectSpan] = field(default_factory=list)
    anomalies: list[int] = field(default_factory=list)
    thumbnails: list[dict[str, object]] = field(default_factory=list)
    format_version: int = FORMAT_VERSION

    def locate_layer_by_z(self, z: float) -> Layer | None:
        """Return the layer whose Z is nearest at or below ``z``."""
        candidates = [layer for layer in self.layers if layer.z <= z + _Z_EPS]
        return candidates[-1] if candidates else (self.layers[0] if self.layers else None)

    def locate_layer_by_line(self, line: int) -> Layer | None:
        """Return the layer containing a given line number."""
        for layer in self.layers:
            if layer.start_line <= line <= layer.end_line:
                return layer
        return None


def build_index(text: str) -> GcodeIndex:
    """Index G-code text in a single forward pass."""
    builder = _Builder()
    return builder.run(text)


class _Builder:
    """Accumulates the index across the pass; kept separate to keep ``build_index`` readable."""

    def __init__(self) -> None:
        self.state = MachineState()
        self.layers: list[Layer] = []
        self.events: list[Event] = []
        self.object_spans: list[ObjectSpan] = []
        self.thumbnails: list[dict[str, object]] = []
        self._layer_open: dict[str, object] | None = None
        self._current_object: dict[str, object] | None = None
        self._header: list[str] = []
        self._config: list[str] = []
        self._in_header = False
        self._in_config = False

    def run(self, text: str) -> GcodeIndex:
        offset = 0
        for line_no, line in enumerate(text.splitlines(keepends=True)):
            raw = line.rstrip("\n")
            self._scan_blocks(raw)
            self._scan_object_markers(raw, line_no)
            command = parse_command(raw)
            if command is not None:
                new_state = apply(self.state, command)
                self._maybe_open_layer(command, new_state, line_no, offset)
                self._record_events(command, new_state, line_no, offset)
                self.state = new_state
            offset += len(line.encode("utf-8"))

        self._close_layer(len(text.splitlines()), offset)
        self._close_object(len(text.splitlines()))
        index = GcodeIndex(
            total_bytes=offset,
            total_lines=len(text.splitlines()),
            header_text="\n".join(self._header),
            config_text="\n".join(self._config),
            layers=self.layers,
            events=self.events,
            object_spans=self.object_spans,
            thumbnails=self.thumbnails,
        )
        index.anomalies = _detect_anomalies(index.layers)
        return index

    # -- blocks and thumbnails -------------------------------------------------------------

    def _scan_blocks(self, raw: str) -> None:
        stripped = raw.strip()
        if stripped == "; HEADER_BLOCK_START":
            self._in_header = True
            return
        if stripped == "; HEADER_BLOCK_END":
            self._in_header = False
            return
        if stripped == "; CONFIG_BLOCK_START":
            self._in_config = True
            return
        if stripped == "; CONFIG_BLOCK_END":
            self._in_config = False
            return
        if self._in_header:
            self._header.append(stripped.lstrip("; ").rstrip())
        elif self._in_config:
            self._config.append(stripped.lstrip("; ").rstrip())
        if stripped.startswith("; thumbnail begin"):
            parts = stripped.split()
            if len(parts) >= 4:
                self.thumbnails.append({"dimensions": parts[3], "data": []})
        elif stripped.startswith(";") and self.thumbnails and isinstance(
            self.thumbnails[-1].get("data"), list
        ):
            body = stripped.lstrip("; ").rstrip()
            if body and body not in ("thumbnail end",) and "=" not in body[:1]:
                if all(c.isalnum() or c in "+/=" for c in body):
                    self.thumbnails[-1]["data"].append(body)  # type: ignore[union-attr]

    def _scan_object_markers(self, raw: str, line_no: int) -> None:
        stripped = raw.strip()
        if stripped.startswith("; printing object"):
            self._close_object(line_no)
            name = stripped[len("; printing object") :].strip()
            self._current_object = {"name": name, "start": line_no}
        elif stripped.startswith("; stop printing object"):
            self._close_object(line_no)

    def _close_object(self, line_no: int) -> None:
        if self._current_object is not None:
            layer = self._layer_open["number"] if self._layer_open else -1
            self.object_spans.append(
                ObjectSpan(
                    obj=str(self._current_object["name"]),
                    layer=int(layer),  # type: ignore[arg-type]
                    start_line=int(self._current_object["start"]),  # type: ignore[arg-type]
                    end_line=line_no,
                )
            )
            self._current_object = None

    # -- layers ----------------------------------------------------------------------------

    def _maybe_open_layer(
        self, command, new_state: MachineState, line_no: int, offset: int
    ) -> None:
        if not is_extruding_move(command):
            return
        z = round(new_state.z, 4)
        if self._layer_open is not None and abs(z - float(self._layer_open["z"])) <= _Z_EPS:
            return
        self._close_layer(line_no, offset)
        self._layer_open = {
            "number": len(self.layers),
            "z": z,
            "start_line": line_no,
            "start_offset": offset,
            "state_at_start": self.state,
            "start_extrusion": self.state.extrusion_total,
            "features": [],
            "speed_min": new_state.feedrate,
            "speed_max": new_state.feedrate,
        }

    def _close_layer(self, line_no: int, offset: int) -> None:
        if self._layer_open is None:
            return
        open_layer = self._layer_open
        state_at_start: MachineState = open_layer["state_at_start"]  # type: ignore[assignment]
        self.layers.append(
            Layer(
                number=int(open_layer["number"]),  # type: ignore[arg-type]
                z=float(open_layer["z"]),  # type: ignore[arg-type]
                start_line=int(open_layer["start_line"]),  # type: ignore[arg-type]
                end_line=line_no,
                start_offset=int(open_layer["start_offset"]),  # type: ignore[arg-type]
                end_offset=offset,
                state_at_start=state_at_start,
                extrusion=self.state.extrusion_total - float(open_layer["start_extrusion"]),
                features=tuple(open_layer["features"]),  # type: ignore[arg-type]
                speed_min=float(open_layer["speed_min"]),  # type: ignore[arg-type]
                speed_max=float(open_layer["speed_max"]),  # type: ignore[arg-type]
            )
        )
        self._layer_open = None

    # -- events ----------------------------------------------------------------------------

    def _record_events(self, command, new_state: MachineState, line_no: int, offset: int) -> None:
        word = command.word
        layer = self._layer_open["number"] if self._layer_open else -1
        layer = int(layer)  # type: ignore[arg-type]
        if word in ("M104", "M109") and new_state.extruder_target != self.state.extruder_target:
            self._add_event(
                "temperature", line_no, offset,
                f"extruder {new_state.extruder_target}", layer,
            )
        elif word in ("M140", "M190") and new_state.bed_target != self.state.bed_target:
            self._add_event("temperature", line_no, offset, f"bed {new_state.bed_target}", layer)
        elif word in ("M106", "M107") and new_state.fan_speed != self.state.fan_speed:
            self._add_event("fan", line_no, offset, f"fan {new_state.fan_speed}", layer)
        elif word.startswith("T") and word[1:].isdigit():
            self._add_event("tool_change", line_no, offset, word, layer)
        elif (
            command.word == "G1"
            and "E" in command.params
            and "X" not in command.params
            and "Y" not in command.params
        ):
            delta = command.params["E"] - (self.state.e if self.state.absolute_extrusion else 0.0)
            if delta < 0:
                self._add_event("retraction", line_no, offset, f"{delta:.4f}", layer)
        elif command.word in ("G0", "G1") and "Z" in command.params and "E" not in command.params:
            if new_state.z > self.state.z:
                self._add_event("z_hop", line_no, offset, f"z {new_state.z}", layer)

    def _add_event(self, kind: str, line: int, offset: int, detail: str, layer: int) -> None:
        self.events.append(Event(kind=kind, line=line, offset=offset, detail=detail, layer=layer))


def _detect_anomalies(layers: list[Layer]) -> list[int]:
    """Flag layers whose extrusion departs sharply from the median of their neighbours."""
    if len(layers) < 4:
        return []
    extrusions = [layer.extrusion for layer in layers]
    mid = median(extrusions)
    if mid <= 0:
        return []
    flagged: list[int] = []
    for layer in layers[1:-1]:
        ratio = layer.extrusion / mid
        if ratio < 0.25 or ratio > 4.0:
            flagged.append(layer.number)
    return flagged


def reconstruct_state(index: GcodeIndex, text: str, target_line: int) -> MachineState:
    """Reconstruct machine state at a line by replaying from the containing layer's snapshot.

    Bounds the work to one layer regardless of file size ([file_indexing.md §4.2]).
    """
    layer = index.locate_layer_by_line(target_line)
    lines = text.splitlines()
    if layer is None:
        state = MachineState()
        start = 0
    else:
        state = layer.state_at_start
        start = layer.start_line
    for line_no in range(start, min(target_line + 1, len(lines))):
        command = parse_command(lines[line_no])
        if command is not None:
            state = apply(state, command)
    return state


def full_replay(text: str, target_line: int) -> MachineState:
    """Reconstruct state by replaying from the start of the file — the ground truth for tests."""
    state = MachineState()
    lines = text.splitlines()
    for line_no in range(0, min(target_line + 1, len(lines))):
        command = parse_command(lines[line_no])
        if command is not None:
            state = apply(state, command)
    return state
