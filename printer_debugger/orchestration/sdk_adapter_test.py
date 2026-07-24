"""Tests for the Agent-SDK adapter's pure pieces: permission policy, translation, gating."""

from __future__ import annotations

import asyncio
import unittest

from printer_debugger.orchestration import sdk_config, sdk_translate
from printer_debugger.orchestration.sdk_client import classify_permission, permission_decision
from printer_debugger.orchestration.turn import (
    AssistantMessageEvent,
    TextEvent,
    ToolResultEvent,
    ToolStartEvent,
    UsageEvent,
)


# -- lightweight fakes mimicking the SDK message/block shapes (dispatched by class name) --------
class TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class ToolUseBlock:
    def __init__(self, id: str, name: str, input: dict) -> None:
        self.id, self.name, self.input = id, name, input


class ToolResultBlock:
    def __init__(self, tool_use_id: str, content: str, is_error: bool = False) -> None:
        self.tool_use_id, self.content, self.is_error = tool_use_id, content, is_error


class AssistantMessage:
    def __init__(self, content: list) -> None:
        self.content = content


class UserMessage:
    def __init__(self, content: list) -> None:
        self.content = content


class ResultMessage:
    def __init__(self, usage: dict) -> None:
        self.usage = usage


class ConfigTest(unittest.TestCase):
    """The four permission mechanisms are correct: allowlist, disallow list, gated write."""

    def test_reads_and_web_allowed_write_excluded(self) -> None:
        """The allowlist has every read tool and web search/fetch, and never the write."""
        allowed = sdk_config.allowed_tools()
        self.assertIn("mcp__project__get_settings", allowed)
        self.assertIn("mcp__printer__get_status", allowed)
        self.assertIn("WebSearch", allowed)
        self.assertIn("WebFetch", allowed)
        self.assertNotIn("mcp__printer__propose_command", allowed)

    def test_host_builtins_disallowed(self) -> None:
        """The host-touching built-ins are explicitly disallowed."""
        disallowed = sdk_config.disallowed_tools()
        for tool in ("Bash", "Read", "Write", "Edit", "Glob", "Task"):
            self.assertIn(tool, disallowed)


class PermissionTest(unittest.TestCase):
    """A non-auto-approved tool is gated (the write) or denied (everything else)."""

    def test_classify_write_gated_unknown_denied(self) -> None:
        """propose_command is gated; an unlisted tool is denied."""
        self.assertEqual(classify_permission("mcp__printer__propose_command"), "gate")
        self.assertEqual(classify_permission("Bash"), "deny")
        self.assertEqual(classify_permission("mcp__something__else"), "deny")

    def test_gated_write_awaits_human(self) -> None:
        """A gated write is allowed only if the human approves; a denial blocks it."""

        async def scenario() -> None:
            async def approve_yes(cmd: str, flags: tuple) -> bool:
                return True

            async def approve_no(cmd: str, flags: tuple) -> bool:
                return False

            allowed, _ = await permission_decision(
                "mcp__printer__propose_command", {"command": "G28"}, approve_yes
            )
            self.assertTrue(allowed)
            denied, reason = await permission_decision(
                "mcp__printer__propose_command", {"command": "G28"}, approve_no
            )
            self.assertFalse(denied)
            self.assertIn("rejected", reason)

        asyncio.run(scenario())

    def test_unlisted_tool_denied_without_asking(self) -> None:
        """An unlisted tool is denied without ever consulting the human."""

        async def scenario() -> None:
            called = False

            async def approve(cmd: str, flags: tuple) -> bool:
                nonlocal called
                called = True
                return True

            allowed, _ = await permission_decision("Bash", {}, approve)
            self.assertFalse(allowed)
            self.assertFalse(called, "the human is never asked about a denied tool")

        asyncio.run(scenario())


class TranslateTest(unittest.TestCase):
    """SDK messages translate into the orchestrator's event stream."""

    def test_assistant_text_and_tool_use(self) -> None:
        """An assistant message yields text, a tool-start, and the stored message."""
        message = AssistantMessage(
            [TextBlock("looking at layer 3"),
             ToolUseBlock("tu_1", "mcp__gcode__get_commands", {"line": 300})]
        )
        events = sdk_translate.translate_message(message)
        self.assertIsInstance(events[0], TextEvent)
        start = next(e for e in events if isinstance(e, ToolStartEvent))
        self.assertEqual((start.server, start.tool, start.ref), ("gcode", "get_commands", "tu_1"))
        self.assertTrue(any(isinstance(e, AssistantMessageEvent) for e in events))

    def test_tool_result_and_usage(self) -> None:
        """A user tool-result message and a result message translate to their events."""
        results = sdk_translate.translate_message(
            UserMessage([ToolResultBlock("tu_1", "header text")])
        )
        self.assertIsInstance(results[0], ToolResultEvent)
        self.assertEqual(results[0].ref, "tu_1")
        usage = sdk_translate.translate_message(
            ResultMessage({"input_tokens": 120, "output_tokens": 40})
        )
        self.assertIsInstance(usage[0], UsageEvent)
        self.assertEqual(usage[0].usage.input_tokens, 120)


if __name__ == "__main__":
    unittest.main()
