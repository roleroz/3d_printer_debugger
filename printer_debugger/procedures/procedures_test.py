"""Tests for precondition checking and result recording."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from printer_debugger.procedures import preconditions, recording
from printer_debugger.procedures.catalog import ProcedureDoc
from printer_debugger.procedures.preconditions import LiveState
from printer_debugger.store.db import Database
from printer_debugger.store.errors import ConstraintViolationError
from printer_debugger.store.models import PrinterStatus, Procedure
from printer_debugger.store.structured_store import StructuredStore

_ACCEL_DOC = ProcedureDoc(
    id="input_shaper", name="Input shaper", scope="printer", purpose="",
    preconditions=(), hardware_requirements=("an accelerometer (adxl345)",), steps=(),
    evidence="", interpretation="", results=(), records="",
)
_PLAIN_DOC = ProcedureDoc(
    id="pid_tune", name="PID", scope="printer", purpose="", preconditions=(),
    hardware_requirements=(), steps=(), evidence="", interpretation="", results=(), records="",
)


class PreconditionTest(unittest.TestCase):
    """Preconditions gate a procedure before it starts."""

    def test_hardware_absent_is_unavailable(self) -> None:
        """A missing accelerometer makes input shaper unavailable on that printer."""
        result = preconditions.check(
            _ACCEL_DOC, "[printer]\nkinematics: corexy\n", LiveState(True, True)
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.unavailable)

    def test_hardware_present_passes(self) -> None:
        """An accelerometer in the config satisfies the hardware requirement."""
        config = "[adxl345]\ncs_pin: PA4\n"
        result = preconditions.check(_ACCEL_DOC, config, LiveState(idle=True, homed=True))
        self.assertTrue(result.ok)

    def test_not_idle_or_homed_fails(self) -> None:
        """A printing or unhomed printer fails the live-state preconditions."""
        result = preconditions.check(_PLAIN_DOC, "", LiveState(idle=False, homed=False))
        self.assertFalse(result.ok)
        self.assertFalse(result.unavailable)
        self.assertEqual(len(result.failures), 2)

    def test_unreachable_blocks_at_precondition(self) -> None:
        """An unreachable printer blocks the procedure rather than starting on unknown state."""
        result = preconditions.check(_PLAIN_DOC, "", None)
        self.assertFalse(result.ok)
        self.assertIn("unreachable", result.failures[0])


class RecordingTest(unittest.TestCase):
    """Recording enforces scope and routes first_layer's Z-offset out of the row."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.db = Database(Path(self._dir.name) / "t.db")
        self.db.migrate()
        self.store = StructuredStore(self.db)
        self.printer = self.store.create_printer(
            name="Trident", kb_section="s", kb_content_hash="h", status=PrinterStatus.COMPLETE
        )
        self.session = self.store.create_session(name="s", printer_id=self.printer.id)

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_first_layer_zoffset_not_in_row(self) -> None:
        """A first_layer result stores filament values but not the mechanical Z-offset."""
        result = recording.record(
            self.store, session_id=self.session.id, printer_id=self.printer.id,
            procedure=Procedure.FIRST_LAYER,
            values={"z_offset": -1.2, "first_layer_temperature": 240}, filament="PETG",
        )
        self.assertNotIn("z_offset", result.values)
        self.assertIn("first_layer_temperature", result.values)

    def test_scope_check_enforced(self) -> None:
        """A machine-scoped result carrying a filament is rejected by the database."""
        with self.assertRaises(ConstraintViolationError):
            recording.record(
                self.store, session_id=self.session.id, printer_id=self.printer.id,
                procedure=Procedure.INPUT_SHAPER, values={"x": 1}, filament="PETG",
            )

    def test_suggested_edit_contains_context(self) -> None:
        """The suggested KB edit names the printer, procedure, values, and date."""
        text = recording.suggested_kb_edit(
            Procedure.TEMPERATURE, "Trident", {"nozzle_temperature": 245}, "2026-07-21"
        )
        self.assertIn("Trident", text)
        self.assertIn("2026-07-21", text)
        self.assertIn("nozzle_temperature=245", text)

    def test_starting_point_from_other_printer_labelled(self) -> None:
        """A value from another printer is offered as a labelled starting point."""
        other = self.store.create_printer(
            name="Switchwire", kb_section="s", kb_content_hash="h2", status=PrinterStatus.COMPLETE
        )
        other_session = self.store.create_session(name="o", printer_id=other.id)
        self.store.add_procedure_result(
            session_id=other_session.id, printer_id=other.id, procedure=Procedure.TEMPERATURE,
            values={"nozzle_temperature": 250}, filament="PETG",
        )
        suggestion = recording.starting_point(
            self.store, printer_id=self.printer.id, procedure=Procedure.TEMPERATURE, filament="PETG"
        )
        assert suggestion is not None
        self.assertEqual(suggestion["from_printer"], "Switchwire")
        self.assertIn("re-tune", suggestion["label"])


if __name__ == "__main__":
    unittest.main()
