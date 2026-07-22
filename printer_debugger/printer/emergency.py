"""Emergency stop: ``M112`` via Moonraker's dedicated endpoint ([printer_access.md §2.3]).

Uses ``POST /printer/emergency_stop`` — which shuts the MCU down directly — rather than submitting
``M112`` as a gcode script that could sit behind a pending command. It bypasses the agent, the
gate, and any queue. A failure to send is reported immediately and unmistakably, never swallowed.
"""

from __future__ import annotations

from .moonraker import PrinterUnreachable, Transport, _urllib_transport


class EmergencyStopFailed(Exception):
    """The emergency stop could not be sent; surfaced loudly, never assumed to have worked."""


def emergency_stop(base_url: str, transport: Transport = _urllib_transport) -> None:
    """Fire the emergency stop. Raises EmergencyStopFailed if it could not be sent."""
    url = base_url.rstrip("/") + "/printer/emergency_stop"
    try:
        status, _ = transport("POST", url, b"")
    except PrinterUnreachable as exc:
        raise EmergencyStopFailed(f"could not reach the printer to stop it: {exc}") from exc
    if status not in (200, 204):
        raise EmergencyStopFailed(f"emergency stop returned {status}")
