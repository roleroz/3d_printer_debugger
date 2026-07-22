"""Session lifecycle: creation with auto-naming, binding, and startup recovery.

Naming is a model call on the opening content, stored and renameable at any time
([orchestration.md §3]). The namer is a module-level function variable so tests supply names
without a network. Startup recovery sweeps interrupted tool calls and resolves pending proposals to
denial ([orchestration.md §5, §7]).
"""

from __future__ import annotations

from ..store.models import BindingReason, Session
from ..store.structured_store import StructuredStore
from .binding import BindingSuggestion, ProjectIdentity, detect
from .gate import ApprovalGate


class SessionService:
    """Session lifecycle over the store, with naming, binding, and recovery."""

    def __init__(self, store: StructuredStore) -> None:
        self._store = store

    def create(self, opening_content: str, printer_id: str | None = None) -> Session:
        """Create a session auto-named from its opening content."""
        name = _name_session(opening_content)
        return self._store.create_session(name=name, printer_id=printer_id)

    def suggest_binding(
        self, identity: ProjectIdentity
    ) -> BindingSuggestion:
        """Suggest a printer for a project; the caller binds silently or prompts."""
        return detect(identity, self._store.list_printers())

    def bind(self, session_id: str, printer_id: str, reason: BindingReason) -> None:
        """Bind a session to a printer, recording the binding as history."""
        self._store.bind_printer(session_id, printer_id, reason)

    def recover_on_startup(self, gate: ApprovalGate) -> dict[str, int]:
        """Sweep interrupted tool calls and resolve pending proposals to denial."""
        pending = gate.recover_pending()
        interrupted = self._store.sweep_interrupted_tool_calls()
        return {"proposals_timed_out": pending, "tool_calls_interrupted": interrupted}


def _name_session(opening_content: str) -> str:  # pragma: no cover - needs a model
    """Name a session from its opening content via a small fast model. Injected in tests."""
    import anthropic

    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=32,
        messages=[
            {
                "role": "user",
                "content": "Give a 3-6 word title for this printing problem, no quotes:\n\n"
                + opening_content[:2000],
            }
        ],
    )
    return "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
