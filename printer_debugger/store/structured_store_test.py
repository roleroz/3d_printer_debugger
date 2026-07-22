"""Tests for the StructuredStore: every entity's happy path and its constraint branches."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from printer_debugger.store.db import Database
from printer_debugger.store.errors import ConstraintViolationError
from printer_debugger.store.models import (
    ApprovalDecision,
    ArtifactKind,
    BindingReason,
    ConfigSource,
    FileIndexKind,
    MessageRole,
    PrinterStatus,
    Procedure,
    TokenUsage,
)
from printer_debugger.store.structured_store import StructuredStore


class StructuredStoreTest(unittest.TestCase):
    """Exercises the typed store against real, migrated SQLite."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.db = Database(Path(self._dir.name) / "test.db")
        self.db.migrate()
        self.store = StructuredStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def _printer(self, name: str = "Voron 2.4") -> str:
        return self.store.create_printer(
            name=name,
            kb_section="## " + name,
            kb_content_hash="hash-" + name,
            status=PrinterStatus.COMPLETE,
            address="http://printer.local",
        ).id

    def _session(self, printer_id: str | None = None) -> str:
        return self.store.create_session(name="A session", printer_id=printer_id).id

    # -- printers --------------------------------------------------------------------------

    def test_create_and_fetch_printer(self) -> None:
        """A created printer is fetchable by id and by its unique name."""
        printer_id = self._printer()
        fetched = self.store.get_printer(printer_id)
        assert fetched is not None
        self.assertEqual(fetched.name, "Voron 2.4")
        self.assertEqual(fetched.status, PrinterStatus.COMPLETE)
        by_name = self.store.get_printer_by_name("Voron 2.4")
        assert by_name is not None
        self.assertEqual(by_name.id, printer_id)

    def test_duplicate_printer_name_rejected(self) -> None:
        """The UNIQUE name constraint rejects a second printer with the same name."""
        self._printer()
        with self.assertRaises(ConstraintViolationError):
            self._printer()

    def test_degraded_printer_records_missing(self) -> None:
        """A degraded printer stores the JSON array of missing values as a tuple."""
        printer = self.store.create_printer(
            name="Switchwire",
            kb_section="## Switchwire",
            kb_content_hash="h",
            status=PrinterStatus.DEGRADED,
            missing=["address", "config_path"],
        )
        fetched = self.store.get_printer(printer.id)
        assert fetched is not None
        self.assertEqual(fetched.missing, ("address", "config_path"))

    def test_mark_printer_absent_and_update_clears_it(self) -> None:
        """Marking a printer absent sets absent_since; re-ingest via update clears it."""
        printer_id = self._printer()
        self.store.mark_printer_absent(printer_id)
        self.assertIsNotNone(self.store.get_printer(printer_id).absent_since)
        self.store.update_printer(
            printer_id,
            kb_section="## Voron 2.4 (updated)",
            kb_content_hash="h2",
            status=PrinterStatus.COMPLETE,
            address="http://printer.local",
            config_path=None,
            missing=[],
        )
        refreshed = self.store.get_printer(printer_id)
        assert refreshed is not None
        self.assertIsNone(refreshed.absent_since)
        self.assertEqual(refreshed.kb_content_hash, "h2")

    # -- config snapshots ------------------------------------------------------------------

    def test_config_snapshots_accumulate_latest_wins(self) -> None:
        """Snapshots accumulate; latest_config_snapshot returns the most recent."""
        printer_id = self._printer()
        self.store.add_config_snapshot(
            printer_id=printer_id, source=ConfigSource.FILES, contents="v1"
        )
        latest = self.store.add_config_snapshot(
            printer_id=printer_id,
            source=ConfigSource.MOONRAKER,
            contents="v2",
            discrepancies=["pid differs"],
        )
        got = self.store.latest_config_snapshot(printer_id)
        assert got is not None
        self.assertEqual(got.id, latest.id)
        self.assertEqual(got.source, ConfigSource.MOONRAKER)
        self.assertEqual(got.discrepancies, ("pid differs",))
        self.assertEqual(len(self.store.list_config_snapshots(printer_id)), 2)

    # -- sessions and bindings -------------------------------------------------------------

    def test_session_lifecycle(self) -> None:
        """A session can be renamed, closed, and reopened, with state tracked."""
        session_id = self._session()
        self.store.rename_session(session_id, "Warping issue")
        self.store.close_session(session_id)
        closed = self.store.get_session(session_id)
        assert closed is not None
        self.assertEqual(closed.name, "Warping issue")
        self.assertEqual(closed.state.value, "closed")
        self.assertIsNotNone(closed.closed_at)
        self.store.reopen_session(session_id)
        reopened = self.store.get_session(session_id)
        assert reopened is not None
        self.assertEqual(reopened.state.value, "open")
        self.assertIsNone(reopened.closed_at)

    def test_list_sessions_by_recency(self) -> None:
        """list_sessions returns most-recently-active first."""
        first = self._session()
        second = self._session()
        self.store.touch_session(first)  # first becomes most recent
        ordered = [s.id for s in self.store.list_sessions()]
        self.assertEqual(ordered[0], first)
        self.assertIn(second, ordered)

    def test_bind_printer_sets_session_and_records_history(self) -> None:
        """Binding sets the session's printer and writes a binding row; reassign adds another."""
        printer_a = self._printer("A")
        printer_b = self._printer("B")
        session_id = self._session()
        self.store.bind_printer(session_id, printer_a, BindingReason.DETECTED)
        self.assertEqual(self.store.get_session(session_id).printer_id, printer_a)
        self.store.bind_printer(session_id, printer_b, BindingReason.REASSIGNED)
        self.assertEqual(self.store.get_session(session_id).printer_id, printer_b)
        bindings = self.store.list_bindings(session_id)
        self.assertEqual([b.printer_id for b in bindings], [printer_a, printer_b])
        self.assertEqual(bindings[1].reason, BindingReason.REASSIGNED)

    def test_accumulate_usage_adds(self) -> None:
        """Usage accumulates additively across turns; cost is never stored."""
        session_id = self._session()
        self.store.accumulate_usage(session_id, TokenUsage(input_tokens=10, output_tokens=5))
        self.store.accumulate_usage(
            session_id, TokenUsage(input_tokens=3, cache_read_tokens=7)
        )
        session = self.store.get_session(session_id)
        assert session is not None
        self.assertEqual(session.input_tokens, 13)
        self.assertEqual(session.output_tokens, 5)
        self.assertEqual(session.cache_read_tokens, 7)

    # -- messages --------------------------------------------------------------------------

    def test_messages_get_sequential_order(self) -> None:
        """Messages receive increasing seq numbers and list in that order."""
        session_id = self._session()
        self.store.add_message(session_id, MessageRole.USER, [{"type": "text", "text": "hi"}])
        self.store.add_message(session_id, MessageRole.ASSISTANT, [{"type": "text", "text": "yo"}])
        messages = self.store.list_messages(session_id)
        self.assertEqual([m.seq for m in messages], [0, 1])
        self.assertEqual(messages[0].content[0]["text"], "hi")

    # -- tool calls and approvals ----------------------------------------------------------

    def test_tool_call_start_finish(self) -> None:
        """A started tool call has a null finish time until finished."""
        session_id = self._session()
        call = self.store.start_tool_call(
            session_id=session_id, server="gcode", tool="get_header", arguments={"layer": 1}
        )
        self.assertIsNone(self.store.get_tool_call(call.id).finished_at)
        self.store.finish_tool_call(call.id, result_summary="ok")
        finished = self.store.get_tool_call(call.id)
        assert finished is not None
        self.assertIsNotNone(finished.finished_at)
        self.assertFalse(finished.is_error)

    def test_sweep_interrupted_tool_calls(self) -> None:
        """An in-flight call is marked interrupted at startup, not assumed done."""
        session_id = self._session()
        call = self.store.start_tool_call(
            session_id=session_id, server="printer", tool="get_status", arguments={}
        )
        swept = self.store.sweep_interrupted_tool_calls()
        self.assertEqual(swept, 1)
        marked = self.store.get_tool_call(call.id)
        assert marked is not None
        self.assertIsNotNone(marked.finished_at)
        self.assertTrue(marked.is_error)
        self.assertIn("interrupted", marked.result_summary or "")

    def test_approval_records_verbatim_command(self) -> None:
        """An approval stores the proposed command verbatim on its own row."""
        session_id = self._session()
        call = self.store.start_tool_call(
            session_id=session_id, server="printer", tool="propose_command", arguments={}
        )
        approval = self.store.record_approval(
            tool_call_id=call.id,
            proposed_command="SET_HEATER_TEMPERATURE HEATER=extruder TARGET=240",
            decision=ApprovalDecision.APPROVED,
            decided_by="user@example.com",
            danger_flags=["heater"],
        )
        fetched = self.store.get_approval(call.id)
        assert fetched is not None
        self.assertEqual(fetched.proposed_command, approval.proposed_command)
        self.assertEqual(fetched.danger_flags, ("heater",))

    def test_double_approval_rejected(self) -> None:
        """The UNIQUE tool_call_id makes a second decision on one proposal impossible."""
        session_id = self._session()
        call = self.store.start_tool_call(
            session_id=session_id, server="printer", tool="propose_command", arguments={}
        )
        self.store.record_approval(
            tool_call_id=call.id,
            proposed_command="G28",
            decision=ApprovalDecision.APPROVED,
            decided_by="user",
        )
        with self.assertRaises(ConstraintViolationError):
            self.store.record_approval(
                tool_call_id=call.id,
                proposed_command="G28",
                decision=ApprovalDecision.REJECTED,
                decided_by="user",
            )

    # -- artifacts, indexes, procedure results ---------------------------------------------

    def test_artifact_and_index_round_trip(self) -> None:
        """Artifact metadata and a built index over it are stored and fetched by key."""
        session_id = self._session()
        artifact = self.store.add_artifact(
            session_id=session_id,
            kind=ArtifactKind.GCODE,
            blob_key="sessions/s/a.gcode",
            size_bytes=1234,
            content_type="text/x.gcode",
        )
        self.assertEqual(self.store.list_artifacts(session_id)[0].id, artifact.id)
        index = self.store.add_file_index(
            artifact_id=artifact.id,
            kind=FileIndexKind.GCODE,
            blob_key="indexes/a/v1.idx",
            format_version=1,
        )
        self.assertEqual(self.store.get_file_index(artifact.id).id, index.id)
        self.store.delete_file_index(index.id)
        self.assertIsNone(self.store.get_file_index(artifact.id))

    def test_procedure_result_scope_check_rejects_machine_with_filament(self) -> None:
        """A printer-scoped procedure carrying a filament is rejected by the CHECK."""
        printer_id = self._printer()
        session_id = self._session(printer_id)
        with self.assertRaises(ConstraintViolationError):
            self.store.add_procedure_result(
                session_id=session_id,
                printer_id=printer_id,
                procedure=Procedure.INPUT_SHAPER,
                values={"x_freq": 55.0},
                filament="PETG",
            )

    def test_procedure_result_scope_check_rejects_filament_without_filament(self) -> None:
        """A filament-scoped procedure with no filament is rejected by the CHECK."""
        printer_id = self._printer()
        session_id = self._session(printer_id)
        with self.assertRaises(ConstraintViolationError):
            self.store.add_procedure_result(
                session_id=session_id,
                printer_id=printer_id,
                procedure=Procedure.TEMPERATURE,
                values={"nozzle": 245},
            )

    def test_procedure_results_valid_both_scopes(self) -> None:
        """A machine result without a filament and a filament result with one both store."""
        printer_id = self._printer()
        session_id = self._session(printer_id)
        self.store.add_procedure_result(
            session_id=session_id,
            printer_id=printer_id,
            procedure=Procedure.PID_TUNE,
            values={"kp": 20.0},
        )
        self.store.add_procedure_result(
            session_id=session_id,
            printer_id=printer_id,
            procedure=Procedure.TEMPERATURE,
            values={"nozzle": 245},
            filament="PETG",
        )
        results = self.store.list_procedure_results(printer_id=printer_id)
        self.assertEqual(len(results), 2)
        petg = self.store.list_procedure_results(
            printer_id=printer_id, procedure=Procedure.TEMPERATURE, filament="PETG"
        )
        self.assertEqual(len(petg), 1)
        self.assertEqual(petg[0].values["nozzle"], 245)

    def test_by_kind_byte_totals(self) -> None:
        """Artifact bytes are summed per kind for accounting."""
        session_id = self._session()
        self.store.add_artifact(
            session_id=session_id,
            kind=ArtifactKind.GCODE,
            blob_key="k1",
            size_bytes=100,
            content_type="x",
        )
        self.store.add_artifact(
            session_id=session_id,
            kind=ArtifactKind.PHOTO,
            blob_key="k2",
            size_bytes=40,
            content_type="x",
        )
        self.assertEqual(
            self.store.sum_artifact_bytes_by_kind(), {"gcode": 100, "photo": 40}
        )

    # -- section cache ---------------------------------------------------------------------

    def test_section_cache_put_get_and_replace(self) -> None:
        """A section-cache entry is stored, fetched, and replaced on a repeat put."""
        self.store.put_section_cache("h1", {"is_printer": True, "name": "Voron"})
        got = self.store.get_section_cache("h1")
        assert got is not None
        self.assertTrue(got.result["is_printer"])
        self.store.put_section_cache("h1", {"is_printer": False})
        replaced = self.store.get_section_cache("h1")
        assert replaced is not None
        self.assertFalse(replaced.result["is_printer"])
        self.assertIsNone(self.store.get_section_cache("missing"))


if __name__ == "__main__":
    unittest.main()
