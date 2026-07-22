"""The ``printer`` MCP tool surface ([printer_access.md §5]).

Read tools plus the one write, ``propose_command`` — the only tool in any server that changes
anything. Runtime reads return unavailable-with-reason when the printer is unreachable; they never
substitute a saved value. ``propose_command`` classifies statically, then hands the proposal to the
gate (injected; the concrete gate is the orchestrator's).
"""

from __future__ import annotations

from typing import Any, Callable

from ..indexing.responses import bounded
from . import danger
from .danger import Classification, Limits
from .moonraker import MoonrakerClient, PrinterUnreachable
from .tiers import unavailable_runtime

# gate(proposed_command, classification) -> outcome dict. The orchestrator supplies the real one.
Gate = Callable[[str, Classification], dict[str, Any]]


class PrinterTools:
    """The tools of the ``printer`` server, over one printer."""

    def __init__(
        self,
        client: MoonrakerClient,
        config_text: str = "",
        gate: Gate | None = None,
        snapshot_url: str | None = None,
    ) -> None:
        self._client = client
        self._config_text = config_text
        self._gate = gate
        self._snapshot_url = snapshot_url
        self._macros = danger.extract_macros(config_text)
        self._limits = danger.extract_limits(config_text)

    def get_status(self) -> dict[str, Any]:
        """Print state — idle, printing, paused, error — and progress."""
        return self._runtime_query({"print_stats": None}, "print_stats")

    def get_temperatures(self) -> dict[str, Any]:
        """Current and target temperatures for hotend, bed, and chamber, and fan speeds."""
        return self._runtime_query({"extruder": None, "heater_bed": None, "fan": None}, None)

    def get_position(self) -> dict[str, Any]:
        """Current toolhead position and homing state."""
        return self._runtime_query({"toolhead": None}, "toolhead")

    def get_config(self) -> dict[str, Any]:
        """Saved and configured configuration, from the snapshot ([printer_access.md §4])."""
        return bounded({"tier": "configured+saved", "config": self._config_text})

    def get_runtime_state(self) -> dict[str, Any]:
        """Live runtime values: mesh, applied offsets, tilt, runtime-set values."""
        return self._runtime_query(
            {"bed_mesh": None, "gcode_move": None, "z_tilt": None, "quad_gantry_level": None}, None
        )

    def get_logs(self) -> dict[str, Any]:
        """A bounded tail of the printer's log and any active error text."""
        try:
            text = self._client.get_logs()
        except PrinterUnreachable as exc:
            return bounded({"available": False, "reason": str(exc)})
        return bounded({"available": True, "log_tail": text})

    def capture_still(self) -> dict[str, Any]:
        """A webcam still — the caller stores it as an artifact."""
        from .webcam import WebcamUnavailable, capture_still

        try:
            data = capture_still(self._snapshot_url, self._client.transport)
        except WebcamUnavailable as exc:
            return bounded({"available": False, "reason": str(exc)})
        return bounded({"available": True, "bytes": len(data)})

    def propose_command(
        self, command: str, homed_axes: str | None = None, hotend_temp: float | None = None
    ) -> dict[str, Any]:
        """The only write: classify statically, then hand the proposal to the gate."""
        classification = danger.classify(
            command, self._macros, self._limits, homed_axes=homed_axes, hotend_temp=hotend_temp
        )
        if classification.refused:
            return bounded(
                {"executed": False, "refused": True, "reason": classification.reason}
            )
        if self._gate is None:
            return bounded(
                {"executed": False, "flags": list(classification.flags),
                 "note": "no gate configured"}
            )
        outcome = self._gate(command, classification)
        return bounded({"flags": list(classification.flags), **outcome})

    def _runtime_query(self, objects: dict[str, object], key: str | None) -> dict[str, Any]:
        try:
            result = self._client.query_objects(objects)
        except PrinterUnreachable as exc:
            unavailable = unavailable_runtime(str(exc))
            return bounded({"available": False, "reason": unavailable.reason})
        status = result.get("status", result)
        payload = status.get(key) if key is not None else status
        return bounded({"available": True, "tier": "runtime", "value": payload})
