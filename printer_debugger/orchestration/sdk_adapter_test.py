"""Tests for the Agent-SDK adapter's pure pieces: permission policy, translation, gating."""

from __future__ import annotations

import asyncio
import base64
import unittest

from printer_debugger.orchestration import sdk_config, sdk_translate
from printer_debugger.orchestration.sdk_client import (
    classify_permission,
    inline_image_blocks,
    permission_decision,
)
from printer_debugger.orchestration.turn import (
    AssistantMessageEvent,
    ErrorEvent,
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
    def __init__(
        self,
        usage: dict | None = None,
        *,
        subtype: str = "success",
        is_error: bool = False,
        stop_reason: str | None = None,
        result: str | None = None,
        errors: list[str] | None = None,
        api_error_status: int | None = None,
        terminal_reason: str | None = None,
    ) -> None:
        self.usage = usage
        self.subtype = subtype
        self.is_error = is_error
        self.stop_reason = stop_reason
        self.result = result
        self.errors = errors
        self.api_error_status = api_error_status
        self.terminal_reason = terminal_reason


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

    def test_success_result_yields_only_usage(self) -> None:
        """A successful ResultMessage emits a UsageEvent and no ErrorEvent."""
        events = sdk_translate.translate_message(
            ResultMessage({"input_tokens": 5, "output_tokens": 2}, subtype="success")
        )
        self.assertTrue(all(not isinstance(e, ErrorEvent) for e in events))
        self.assertTrue(any(isinstance(e, UsageEvent) for e in events))

    def test_is_error_result_yields_error_and_usage(self) -> None:
        """An is_error ResultMessage emits an ErrorEvent reflecting the fields, plus usage."""
        events = sdk_translate.translate_message(
            ResultMessage(
                {"input_tokens": 5, "output_tokens": 0},
                subtype="error_during_execution",
                is_error=True,
                api_error_status=529,
                errors=["overloaded"],
            )
        )
        error = next(e for e in events if isinstance(e, ErrorEvent))
        self.assertIn("subtype=error_during_execution", error.message)
        self.assertIn("status=529", error.message)
        self.assertIn("overloaded", error.message)
        self.assertTrue(any(isinstance(e, UsageEvent) for e in events))

    def test_non_success_subtype_yields_error(self) -> None:
        """A non-"success" subtype (e.g. max-turns) emits an ErrorEvent even without is_error."""
        events = sdk_translate.translate_message(
            ResultMessage(
                {"input_tokens": 1, "output_tokens": 0},
                subtype="error_max_turns",
                is_error=False,
                result="reached the turn limit",
            )
        )
        error = next(e for e in events if isinstance(e, ErrorEvent))
        self.assertIn("subtype=error_max_turns", error.message)
        self.assertIn("reached the turn limit", error.message)


class InlineImageBlocksTest(unittest.TestCase):
    """Image reference blocks are inlined to base64 for the model; other blocks pass through."""

    def test_image_reference_becomes_base64_block(self) -> None:
        """An image reference block is read via the reader and re-emitted as a base64 block."""
        reads: list[str] = []

        def reader(artifact_id: str) -> tuple[bytes, str]:
            reads.append(artifact_id)
            return b"\x89PNG-bytes", "image/png"

        content = [{"type": "image", "artifact_id": "art_1", "media_type": "image/png"}]
        result = inline_image_blocks(content, reader)
        expected_data = base64.b64encode(b"\x89PNG-bytes").decode("ascii")
        self.assertEqual(
            result,
            [{"type": "image",
              "source": {"type": "base64", "media_type": "image/png", "data": expected_data}}],
        )
        self.assertEqual(reads, ["art_1"])

    def test_text_and_inlined_blocks_pass_through_unchanged(self) -> None:
        """A text block and an already-inlined image block are returned without a reader call."""

        def reader(artifact_id: str) -> tuple[bytes, str]:
            raise AssertionError("the reader must not be called for non-reference blocks")

        already_inlined = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"},
        }
        content = [{"type": "text", "text": "why is this warping?"}, already_inlined]
        self.assertEqual(inline_image_blocks(content, reader), content)

    def test_media_type_falls_back_to_reader_content_type(self) -> None:
        """A reference block with no media_type uses the content type the reader returns."""

        def reader(artifact_id: str) -> tuple[bytes, str]:
            return b"jpegdata", "image/jpeg"

        content = [{"type": "image", "artifact_id": "art_2"}]
        result = inline_image_blocks(content, reader)
        self.assertEqual(result[0]["source"]["media_type"], "image/jpeg")


if __name__ == "__main__":
    unittest.main()
