"""Tests for the approval gate: approve, reject, timeout, crash-recovery, and no bypass."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from printer_debugger.orchestration.gate import ApprovalGate
from printer_debugger.store.db import Database
from printer_debugger.store.models import ApprovalDecision
from printer_debugger.store.structured_store import StructuredStore


class GateTest(unittest.TestCase):
    """Every gate path, because its failure means an unapproved command reaches a machine."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.db = Database(Path(self._dir.name) / "t.db")
        self.db.migrate()
        self.store = StructuredStore(self.db)
        self.session = self.store.create_session(name="s")
        self.executed: list[str] = []
        self.published: list[str] = []

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def _call(self) -> str:
        return self.store.start_tool_call(
            session_id=self.session.id, server="printer", tool="propose_command",
            arguments={"command": "G28"},
        ).id

    def _gate(self, timeout: float = 5.0) -> ApprovalGate:
        async def execute(command: str) -> dict:
            self.executed.append(command)
            return {"result": "ok"}

        return ApprovalGate(
            self.store, publish=lambda p: self.published.append(p.command),
            execute=execute, timeout_seconds=timeout,
        )

    def test_approval_executes(self) -> None:
        """An approved proposal is executed and recorded approved."""

        async def scenario() -> None:
            gate = self._gate()
            call_id = self._call()
            task = asyncio.create_task(
                gate.decide(session_id=self.session.id, tool_call_id=call_id,
                            command="G28", danger_flags=())
            )
            await asyncio.sleep(0)
            self.assertTrue(gate.resolve(call_id, True, "user@x"))
            outcome = await task
            self.assertEqual(outcome.decision, ApprovalDecision.APPROVED)
            self.assertEqual(self.executed, ["G28"])
            self.assertEqual(self.store.get_approval(call_id).decision, ApprovalDecision.APPROVED)

        asyncio.run(scenario())

    def test_rejection_does_not_execute(self) -> None:
        """A rejected proposal is not executed and is recorded rejected."""

        async def scenario() -> None:
            gate = self._gate()
            call_id = self._call()
            task = asyncio.create_task(
                gate.decide(session_id=self.session.id, tool_call_id=call_id,
                            command="G28", danger_flags=())
            )
            await asyncio.sleep(0)
            gate.resolve(call_id, False, "user@x")
            outcome = await task
            self.assertEqual(outcome.decision, ApprovalDecision.REJECTED)
            self.assertEqual(self.executed, [])

        asyncio.run(scenario())

    def test_timeout_is_denial(self) -> None:
        """A proposal no one decides times out into a denial, never an approval."""

        async def scenario() -> None:
            gate = self._gate(timeout=0.05)
            call_id = self._call()
            outcome = await gate.decide(
                session_id=self.session.id, tool_call_id=call_id, command="G28", danger_flags=()
            )
            self.assertEqual(outcome.decision, ApprovalDecision.TIMED_OUT)
            self.assertEqual(self.executed, [])
            self.assertEqual(self.store.get_approval(call_id).decision, ApprovalDecision.TIMED_OUT)

        asyncio.run(scenario())

    def test_crash_recovery_resolves_pending_to_denial(self) -> None:
        """A proposal left pending by a dead process resolves to timed-out on restart."""
        call_id = self._call()  # a propose_command with no approval row
        gate = self._gate()
        recovered = gate.recover_pending()
        self.assertEqual(recovered, 1)
        self.assertEqual(self.store.get_approval(call_id).decision, ApprovalDecision.TIMED_OUT)

    def test_resolve_unknown_proposal_is_noop(self) -> None:
        """Resolving a proposal the gate is not waiting on returns False and changes nothing."""
        gate = self._gate()
        self.assertFalse(gate.resolve("tc_missing", True, "user"))


if __name__ == "__main__":
    unittest.main()
