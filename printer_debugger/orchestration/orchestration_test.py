"""Tests for binding, prompt assembly, the turn loop, and session naming."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from printer_debugger.orchestration import prompt, session_service, turn
from printer_debugger.orchestration.binding import ProjectIdentity, detect, mismatch_finding
from printer_debugger.orchestration.prompt import PromptInputs
from printer_debugger.store.db import Database
from printer_debugger.store.models import PrinterStatus
from printer_debugger.store.structured_store import StructuredStore


def _printer(store: StructuredStore, name: str, absent: bool = False):
    printer = store.create_printer(
        name=name, kb_section="s", kb_content_hash="h-" + name, status=PrinterStatus.COMPLETE
    )
    if absent:
        store.mark_printer_absent(printer.id)
    return store.get_printer(printer.id)


class BindingTest(unittest.TestCase):
    """Binding is confident on one match and prompts otherwise; a mismatch is a finding."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.db = Database(Path(self._dir.name) / "t.db")
        self.db.migrate()
        self.store = StructuredStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_confident_single_match(self) -> None:
        """A project whose preset names a known printer binds confidently."""
        trident = _printer(self.store, "Voron Trident")
        _printer(self.store, "Switchwire")
        identity = ProjectIdentity(printer_settings_id="Voron Trident 300 0.4 nozzle")
        result = detect(identity, self.store.list_printers())
        self.assertTrue(result.confident)
        self.assertEqual(result.printer_id, trident.id)

    def test_no_match_prompts_with_candidates(self) -> None:
        """A project matching nothing prompts, offering the known printers."""
        _printer(self.store, "Voron Trident")
        identity = ProjectIdentity(printer_settings_id="Prusa MK4")
        result = detect(identity, self.store.list_printers())
        self.assertFalse(result.confident)
        self.assertIn("Voron Trident", result.candidates)

    def test_absent_printers_excluded(self) -> None:
        """An absent printer is not offered as a binding candidate."""
        _printer(self.store, "Gone", absent=True)
        result = detect(ProjectIdentity(printer_settings_id="x"), self.store.list_printers())
        self.assertNotIn("Gone", result.candidates)

    def test_mismatch_is_a_finding(self) -> None:
        """A project sliced for a different printer than the bound one yields a finding."""
        bound = _printer(self.store, "Voron Trident")
        identity = ProjectIdentity(printer_settings_id="Voron Switchwire 0.4")
        finding = mismatch_finding(identity, bound)
        self.assertIsNotNone(finding)
        self.assertIn("does not match", finding)


class PromptTest(unittest.TestCase):
    """The stable prefix is byte-identical across sessions with different printers."""

    def test_stable_prefix_shared_across_printers(self) -> None:
        """Two prompts with different printers share an identical cache prefix."""
        catalog = "input_shaper: ...\npid_tune: ..."
        a = prompt.assemble(PromptInputs(catalog, "slicer: Orca", "Trident section", "cfg A", "st"))
        b = prompt.assemble(
            PromptInputs(catalog, "slicer: Orca", "Switchwire section", "cfg B", "s")
        )
        prefix = prompt.stable_prefix(catalog)
        self.assertTrue(a.startswith(prefix))
        self.assertTrue(b.startswith(prefix))

    def test_session_state_comes_last(self) -> None:
        """Session state appears after the printer section in the assembled prompt."""
        assembled = prompt.assemble(
            PromptInputs("cat", "shared", "printer", "snap", "SESSION_MARKER")
        )
        self.assertLess(assembled.index("printer"), assembled.index("SESSION_MARKER"))


class _FakeAgentClient:
    """Yields a scripted event stream for the turn loop."""

    def __init__(self, events: list) -> None:
        self._events = events

    async def run_turn(self, session_id: str, user_content: list):
        for event in self._events:
            yield event


class TurnLoopTest(unittest.TestCase):
    """The turn loop persists messages and tool calls and accumulates usage."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.db = Database(Path(self._dir.name) / "t.db")
        self.db.migrate()
        self.store = StructuredStore(self.db)
        self.session = self.store.create_session(name="s")

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_turn_persists_everything(self) -> None:
        """A turn persists the user and assistant messages, the tool call, and usage."""
        from printer_debugger.store.models import TokenUsage

        events = [
            turn.TextEvent("thinking..."),
            turn.ToolStartEvent("gcode", "get_header", {}, ref="1"),
            turn.ToolResultEvent("1", "header returned"),
            turn.AssistantMessageEvent([{"type": "text", "text": "here is the answer"}]),
            turn.UsageEvent(TokenUsage(input_tokens=100, output_tokens=50)),
        ]
        loop = turn.TurnLoop(self.store, _FakeAgentClient(events))
        asyncio.run(loop.run(self.session.id, [{"type": "text", "text": "why warping?"}]))

        messages = self.store.list_messages(self.session.id)
        self.assertEqual([m.role.value for m in messages], ["user", "assistant"])
        calls = self.store.list_tool_calls(self.session.id)
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(calls[0].finished_at)
        refreshed = self.store.get_session(self.session.id)
        self.assertEqual(refreshed.input_tokens, 100)
        self.assertEqual(refreshed.output_tokens, 50)

    def test_error_event_is_forwarded_and_persisted(self) -> None:
        """An ErrorEvent is passed to on_event and persisted as an assistant message on reload."""
        seen: list = []
        events = [turn.ErrorEvent("The agent turn failed (subtype=error_max_turns)")]
        loop = turn.TurnLoop(
            self.store, _FakeAgentClient(events), on_event=lambda e: seen.append(e)
        )
        asyncio.run(loop.run(self.session.id, [{"type": "text", "text": "why?"}]))

        self.assertTrue(any(isinstance(e, turn.ErrorEvent) for e in seen))
        messages = self.store.list_messages(self.session.id)
        self.assertEqual([m.role.value for m in messages], ["user", "assistant"])
        text = messages[-1].content[0]["text"]
        self.assertIn("error_max_turns", text)
        self.assertTrue(text.startswith("⚠️"))


class SessionNamingTest(unittest.TestCase):
    """Session creation names from opening content via the injectable namer."""

    def test_create_uses_injected_namer(self) -> None:
        """A created session takes the name the namer returns."""
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            db.migrate()
            store = StructuredStore(db)
            original = session_service._name_session
            session_service._name_session = lambda content: "Warping on the Trident"
            try:
                service = session_service.SessionService(store)
                session = service.create("my first layer is warping")
                self.assertEqual(session.name, "Warping on the Trident")
            finally:
                session_service._name_session = original
                db.close()


if __name__ == "__main__":
    unittest.main()
