"""Precondition and hardware-requirement checking ([procedures.md §5]).

Failures are reported before starting, naming what is missing and what would fix it — never
discovered partway through. A hardware requirement failure makes the procedure unavailable on that
printer. The live-state checks need the printer reachable; an unreachable printer blocks the
procedure at precondition time rather than starting on indeterminate state.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import ProcedureDoc


@dataclass(frozen=True, slots=True)
class LiveState:
    """The live-state inputs precondition checks need."""

    idle: bool
    homed: bool
    material_loaded: bool = True


@dataclass(frozen=True, slots=True)
class PreconditionResult:
    """The outcome of checking a procedure's preconditions."""

    ok: bool
    unavailable: bool  # a hardware fact the user cannot change now.
    failures: tuple[str, ...]


def check(
    doc: ProcedureDoc, config_text: str, live: LiveState | None
) -> PreconditionResult:
    """Check a procedure's hardware requirements and live-state preconditions."""
    failures: list[str] = []

    # Hardware requirements are checked against the (offline-capable) config snapshot.
    for requirement in doc.hardware_requirements:
        if not _hardware_present(requirement, config_text):
            return PreconditionResult(
                ok=False,
                unavailable=True,
                failures=(f"unavailable on this printer: requires {requirement}",),
            )

    # Live-state checks need the printer reachable.
    if live is None:
        return PreconditionResult(
            ok=False,
            unavailable=False,
            failures=("the printer is unreachable; this procedure needs it to run",),
        )
    if not live.idle:
        failures.append("the printer is currently printing; wait until it is idle")
    if not live.homed:
        failures.append("the printer is not homed; home it first")
    if not live.material_loaded:
        failures.append("the required material is not loaded; load it")

    return PreconditionResult(ok=not failures, unavailable=False, failures=tuple(failures))


def _hardware_present(requirement: str, config_text: str) -> bool:
    """Whether a required hardware component appears in the configuration snapshot."""
    text = config_text.lower()
    keywords = {
        "accelerometer": ("adxl345", "lis2dw", "lis3dh", "[resonance_tester]"),
    }
    for term, markers in keywords.items():
        if term in requirement.lower():
            return any(marker in text for marker in markers)
    # A requirement with no known keyword is treated as satisfied (not a hardware gate we model).
    return True
