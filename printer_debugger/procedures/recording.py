"""Recording procedure results and suggesting the knowledge-base edit ([procedures.md §7]).

A result is stored scoped by the database CHECK. For ``first_layer`` the mechanical Z-offset is a
saved-config value captured by the snapshot, not written to ``procedure_result`` — the row holds
only the filament-scoped values. A value from another printer may be offered as a starting point,
labelled as such, never presented as this printer's value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..store.models import Procedure, ProcedureResult
from ..store.structured_store import StructuredStore

# Keys that belong to the printer (saved config), not to a filament-scoped first_layer row.
_FIRST_LAYER_PRINTER_KEYS = {"z_offset", "probe_z_offset"}


def record(
    store: StructuredStore,
    *,
    session_id: str,
    printer_id: str,
    procedure: Procedure,
    values: Mapping[str, Any],
    filament: str | None = None,
    evidence: Sequence[str] = (),
) -> ProcedureResult:
    """Record a result, dropping first_layer's printer-scoped Z-offset from the row."""
    stored_values = dict(values)
    if procedure is Procedure.FIRST_LAYER:
        stored_values = {
            key: value for key, value in stored_values.items()
            if key not in _FIRST_LAYER_PRINTER_KEYS
        }
    return store.add_procedure_result(
        session_id=session_id,
        printer_id=printer_id,
        procedure=procedure,
        values=stored_values,
        filament=filament,
        evidence=evidence,
    )


def suggested_kb_edit(
    procedure: Procedure, printer_name: str, values: Mapping[str, Any], date: str
) -> str:
    """Build the suggested calibration-status line for the user to apply to their document."""
    summary = ", ".join(f"{k}={v}" for k, v in values.items())
    return (
        f"- Calibration status (as of {date}): {procedure.value} run on {printer_name} "
        f"({summary}). Apply this to your document if you keep calibration notes there."
    )


def starting_point(
    store: StructuredStore, *, printer_id: str, procedure: Procedure, filament: str
) -> dict[str, Any] | None:
    """A value for the same filament on a different printer, labelled a starting point, or None.

    Never presented as this printer's value ([procedures.md §7.1]).
    """
    for result in store.list_procedure_results(procedure=procedure, filament=filament):
        if result.printer_id != printer_id:
            other = store.get_printer(result.printer_id)
            return {
                "values": dict(result.values),
                "from_printer": other.name if other else result.printer_id,
                "label": "starting point from another printer — re-tune, do not use as-is",
            }
    return None
