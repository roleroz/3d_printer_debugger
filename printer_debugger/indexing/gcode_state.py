"""G-code parsing and cumulative machine-state reconstruction.

The pass interprets absolute/relative positioning (``G90``/``G91``), absolute/relative extrusion
(``M82``/``M83``), and arc moves (``G2``/``G3``) so coordinates and extrusion totals are correct
rather than assuming linear absolute moves
([file_indexing.md §4](../../docs/design/file_indexing.md)).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

_WORD = re.compile(r"([A-Za-z])(-?\d*\.?\d+)")
_COMMAND = re.compile(r"^\s*([GMT]\d+)")


@dataclass(frozen=True, slots=True)
class Command:
    """A parsed G-code command: its word (e.g. ``G1``) and its lettered parameters."""

    word: str
    params: dict[str, float]


def parse_command(line: str) -> Command | None:
    """Parse one line into a command, or None if it is a comment or blank."""
    code = line.split(";", 1)[0].strip()
    if not code:
        return None
    match = _COMMAND.match(code)
    if match is None:
        return None
    word = match.group(1)
    params = {letter.upper(): float(value) for letter, value in _WORD.findall(code[len(word):])}
    return Command(word=word, params=params)


@dataclass(frozen=True, slots=True)
class MachineState:
    """A full snapshot of cumulative machine state at a point in the file."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    e: float = 0.0
    feedrate: float = 0.0
    absolute_positioning: bool = True
    absolute_extrusion: bool = True
    extruder_target: float = 0.0
    bed_target: float = 0.0
    fan_speed: float = 0.0
    tool: int = 0
    extrusion_total: float = 0.0  # cumulative extruded filament, monotonic even under resets.


def apply(state: MachineState, command: Command) -> MachineState:
    """Return the state after applying a command. Pure: does not mutate ``state``."""
    word = command.word
    p = command.params
    if word in ("G0", "G1", "G2", "G3"):
        return _apply_move(state, p)
    if word == "G90":
        return replace(state, absolute_positioning=True)
    if word == "G91":
        return replace(state, absolute_positioning=False)
    if word == "M82":
        return replace(state, absolute_extrusion=True)
    if word == "M83":
        return replace(state, absolute_extrusion=False)
    if word == "G92":
        # Reset coordinate origins without moving. Extrusion_total is preserved (it is physical).
        return replace(
            state,
            x=p.get("X", state.x),
            y=p.get("Y", state.y),
            z=p.get("Z", state.z),
            e=p.get("E", state.e),
        )
    if word in ("M104", "M109"):
        return replace(state, extruder_target=p.get("S", state.extruder_target))
    if word in ("M140", "M190"):
        return replace(state, bed_target=p.get("S", state.bed_target))
    if word == "M106":
        return replace(state, fan_speed=p.get("S", 255.0))
    if word == "M107":
        return replace(state, fan_speed=0.0)
    if word.startswith("T") and word[1:].isdigit():
        return replace(state, tool=int(word[1:]))
    return state


def _apply_move(state: MachineState, p: dict[str, float]) -> MachineState:
    if state.absolute_positioning:
        x = p.get("X", state.x)
        y = p.get("Y", state.y)
        z = p.get("Z", state.z)
    else:
        x = state.x + p.get("X", 0.0)
        y = state.y + p.get("Y", 0.0)
        z = state.z + p.get("Z", 0.0)

    extra = 0.0
    if "E" in p:
        if state.absolute_extrusion:
            new_e = p["E"]
            extra = max(0.0, new_e - state.e)
            e = new_e
        else:
            new_e = p["E"]
            extra = max(0.0, new_e)
            e = state.e + new_e
    else:
        e = state.e

    return replace(
        state,
        x=x,
        y=y,
        z=z,
        e=e,
        feedrate=p.get("F", state.feedrate),
        extrusion_total=state.extrusion_total + extra,
    )


def is_extruding_move(command: Command) -> bool:
    """Whether a command deposits filament while moving in XY."""
    if command.word not in ("G1", "G2", "G3"):
        return False
    return "E" in command.params and ("X" in command.params or "Y" in command.params)
