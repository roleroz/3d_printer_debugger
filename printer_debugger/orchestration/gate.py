"""The approval gate ([orchestration.md §6]).

A proposed printer write is classified, persisted, published to every viewer, and then awaited
outside any transaction until a human decides or the window elapses. The failure direction is
always denial: a crash while a proposal is pending resolves to timed-out on restart, never to an
approval. This is the component whose failure means an unapproved command reaches a machine.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..store.models import ApprovalDecision
from ..store.structured_store import StructuredStore

DEFAULT_TIMEOUT_SECONDS = 300.0

# publish(proposal) notifies every viewer of a pending proposal.
Publish = Callable[["Proposal"], None]
# execute(command) actually submits the approved command to the printer, returning a result.
Execute = Callable[[str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class Proposal:
    """A pending proposal shown to viewers."""

    tool_call_id: str
    session_id: str
    command: str
    danger_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Outcome:
    """The gate's outcome for a proposal."""

    decision: ApprovalDecision
    result: dict[str, Any] | None = None
    message: str | None = None


class ApprovalGate:
    """Serialises the human-in-the-loop decision for every printer write."""

    def __init__(
        self,
        store: StructuredStore,
        publish: Publish,
        execute: Execute,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._store = store
        self._publish = publish
        self._execute = execute
        self._timeout = timeout_seconds
        self._pending: dict[str, asyncio.Future[tuple[bool, str]]] = {}

    async def decide(
        self, *, session_id: str, tool_call_id: str, command: str, danger_flags: tuple[str, ...]
    ) -> Outcome:
        """Publish a proposal and await the human decision, or time out into a denial.

        The wait holds no lock and no open transaction ([orchestration.md §7]); other sessions
        proceed. Recording the decision is a separate commit from any recording of the proposal.
        """
        loop = asyncio.get_event_loop()
        future: asyncio.Future[tuple[bool, str]] = loop.create_future()
        self._pending[tool_call_id] = future
        self._publish(Proposal(tool_call_id, session_id, command, danger_flags))
        try:
            approved, decided_by = await asyncio.wait_for(future, timeout=self._timeout)
        except asyncio.TimeoutError:
            self._record(tool_call_id, command, ApprovalDecision.TIMED_OUT, "system", danger_flags)
            return Outcome(ApprovalDecision.TIMED_OUT, message="approval timed out")
        finally:
            self._pending.pop(tool_call_id, None)

        if not approved:
            self._record(
                tool_call_id, command, ApprovalDecision.REJECTED, decided_by, danger_flags
            )
            return Outcome(ApprovalDecision.REJECTED, message="rejected by user")

        self._record(tool_call_id, command, ApprovalDecision.APPROVED, decided_by, danger_flags)
        result = await self._execute(command)
        return Outcome(ApprovalDecision.APPROVED, result=result)

    def resolve(self, tool_call_id: str, approved: bool, decided_by: str) -> bool:
        """Deliver a viewer's decision to a waiting proposal. Any viewer may decide."""
        future = self._pending.get(tool_call_id)
        if future is None or future.done():
            return False
        future.set_result((approved, decided_by))
        return True

    def recover_pending(self) -> int:
        """On restart, resolve every proposal left pending to timed-out; return how many.

        A crash must never resolve to an approval ([orchestration.md §7]).
        """
        recovered = 0
        for call in self._interrupted_proposals():
            self._record(
                call.id, "", ApprovalDecision.TIMED_OUT, "system", (), proposed=call.arguments
            )
            recovered += 1
        return recovered

    def _interrupted_proposals(self):
        # A propose_command tool call with no approval row is one left pending by a dead process.
        pending = []
        for session in self._store.list_sessions():
            for call in self._store.list_tool_calls(session.id):
                if call.tool == "propose_command" and self._store.get_approval(call.id) is None:
                    pending.append(call)
        return pending

    def _record(
        self,
        tool_call_id: str,
        command: str,
        decision: ApprovalDecision,
        decided_by: str,
        danger_flags: tuple[str, ...],
        proposed: Any = None,
    ) -> None:
        text = command or (proposed or {}).get("command", "") if proposed else command
        self._store.record_approval(
            tool_call_id=tool_call_id,
            proposed_command=text,
            decision=decision,
            decided_by=decided_by,
            danger_flags=danger_flags,
        )
