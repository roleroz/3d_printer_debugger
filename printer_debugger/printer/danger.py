"""Static danger classification of a pending command ([printer_access.md §6]).

Run here, independent of the model's description of its own request. Macros are expanded from the
config snapshot before classification, so a calibration macro is judged by its body, not refused as
unknown; a command that is neither a categorised command nor a defined macro is refused outright.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Categorised built-in commands. Motion is checked against homing and limits; the rest are known.
_MOTION = {"G0", "G1", "G2", "G3"}
_HOMING = {"G28", "G29", "G32"}
_SAFE_GCODE = {
    "G4", "G10", "G11", "G90", "G91", "G92", "M82", "M83", "M84", "M104", "M105",
    "M106", "M107", "M109", "M114", "M115", "M117", "M118", "M140", "M190", "M204",
    "M220", "M221", "M400", "M112",
}
# Recognised extended commands, including the calibration catalog's built-ins.
_KNOWN_EXTENDED = {
    "SET_HEATER_TEMPERATURE", "SET_GCODE_OFFSET", "SET_VELOCITY_LIMIT", "SET_PRESSURE_ADVANCE",
    "SET_FAN_SPEED", "SET_FILAMENT_SENSOR", "TURN_OFF_HEATERS", "PID_CALIBRATE",
    "SHAPER_CALIBRATE", "PROBE_CALIBRATE", "QUAD_GANTRY_LEVEL", "Z_TILT_ADJUST",
    "BED_MESH_CALIBRATE", "BED_MESH_CLEAR", "BED_MESH_PROFILE", "CALIBRATE_Z",
    "SCREWS_TILT_CALCULATE", "MEASURE_AXES_NOISE", "TEST_RESONANCES", "AXIS_TWIST_COMPENSATION",
    "SAVE_CONFIG", "RESTART", "FIRMWARE_RESTART", "STATUS", "HELP", "QUERY_PROBE",
    "GET_POSITION", "SAVE_GCODE_STATE", "RESTORE_GCODE_STATE",
}
_HEATER_SAFETY = re.compile(r"\b(max_temp|min_temp|verify_heater)\b")
_FLOAT = re.compile(r"([XYZE])(-?\d*\.?\d+)")


@dataclass(frozen=True, slots=True)
class Limits:
    """Axis limits and the minimum extrude temperature, from the config snapshot."""

    position_min: dict[str, float] = field(default_factory=dict)
    position_max: dict[str, float] = field(default_factory=dict)
    min_extrude_temp: float = 170.0


@dataclass(frozen=True, slots=True)
class Classification:
    """The result of classifying a command."""

    flags: tuple[str, ...]
    refused: bool
    reason: str | None = None


def extract_macros(config_text: str) -> dict[str, list[str]]:
    """Return each ``[gcode_macro NAME]`` body as a list of its G-code lines, keyed by NAME."""
    macros: dict[str, list[str]] = {}
    current: str | None = None
    in_gcode = False
    for line in config_text.splitlines():
        header = re.match(r"^\[gcode_macro\s+(\S+)\]", line.strip())
        if header is not None:
            current = header.group(1).upper()
            macros[current] = []
            in_gcode = False
            continue
        if line.strip().startswith("[") and current is not None:
            current = None
            in_gcode = False
            continue
        if current is None:
            continue
        if re.match(r"^\s*gcode\s*:", line):
            in_gcode = True
            rest = line.split(":", 1)[1].strip()
            if rest:
                macros[current].append(rest)
            continue
        if in_gcode and (line.startswith(" ") or line.startswith("\t")):
            body = line.strip()
            if body and not body.startswith("#"):
                macros[current].append(body)
    return macros


def extract_limits(config_text: str) -> Limits:
    """Pull axis position limits and min_extrude_temp from the config snapshot."""
    position_min: dict[str, float] = {}
    position_max: dict[str, float] = {}
    min_extrude = 170.0
    section: str | None = None
    for line in config_text.splitlines():
        stripped = line.strip()
        header = re.match(r"^\[([^\]]+)\]", stripped)
        if header is not None:
            section = header.group(1).split()[0]
            continue
        kv = re.match(r"^(\w+)\s*[:=]\s*(-?\d*\.?\d+)", stripped)
        if kv is None or section is None:
            continue
        key, value = kv.group(1), float(kv.group(2))
        is_stepper = section.startswith("stepper_")
        axis = section.replace("stepper_", "").upper() if is_stepper else None
        if axis and key == "position_min":
            position_min[axis] = value
        elif axis and key == "position_max":
            position_max[axis] = value
        elif key == "min_extrude_temp":
            min_extrude = value
    return Limits(
        position_min=position_min, position_max=position_max, min_extrude_temp=min_extrude
    )


def classify(
    command: str,
    macros: dict[str, list[str]],
    limits: Limits,
    *,
    homed_axes: str | None = None,
    hotend_temp: float | None = None,
    _depth: int = 0,
) -> Classification:
    """Classify a command for danger, expanding macros against the config snapshot."""
    word = command.strip().split()[0].upper() if command.strip() else ""
    if not word:
        return Classification(flags=(), refused=False)

    if word in macros and _depth < 10:
        flags: set[str] = set()
        for line in macros[word]:
            sub = classify(
                line, macros, limits, homed_axes=homed_axes, hotend_temp=hotend_temp,
                _depth=_depth + 1,
            )
            if sub.refused:
                return sub  # an unknown command inside a macro refuses the whole thing
            flags.update(sub.flags)
        return Classification(flags=tuple(sorted(flags)), refused=False)

    if word in _MOTION:
        return Classification(
            flags=tuple(_motion_flags(command, limits, homed_axes, hotend_temp)), refused=False
        )
    if word in _HOMING or word in _SAFE_GCODE or word in _KNOWN_EXTENDED:
        extra = ["heater safety parameters"] if _HEATER_SAFETY.search(command) else []
        return Classification(flags=tuple(extra), refused=False)
    if _HEATER_SAFETY.search(command):
        return Classification(flags=("heater safety parameters",), refused=False)

    return Classification(
        flags=(),
        refused=True,
        reason=f"'{word}' is neither a categorised command nor a defined macro",
    )


def _motion_flags(
    command: str, limits: Limits, homed_axes: str | None, hotend_temp: float | None
) -> list[str]:
    flags: list[str] = []
    axes = {letter: float(value) for letter, value in _FLOAT.findall(command)}
    moved = [a for a in ("X", "Y", "Z") if a in axes]
    if moved and homed_axes is not None:
        homed = homed_axes.upper()
        if any(a not in homed for a in moved):
            flags.append("movement without homing")
    for axis in ("X", "Y", "Z"):
        if axis in axes:
            lo = limits.position_min.get(axis)
            hi = limits.position_max.get(axis)
            if (lo is not None and axes[axis] < lo) or (hi is not None and axes[axis] > hi):
                flags.append("beyond configured limits")
                break
    if "E" in axes and hotend_temp is not None and hotend_temp < limits.min_extrude_temp:
        flags.append("extrusion below safe temperature")
    return flags
