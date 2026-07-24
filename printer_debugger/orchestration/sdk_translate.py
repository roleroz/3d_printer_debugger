"""Translate Agent-SDK messages into the orchestrator's ``AgentEvent`` stream ([turn.py]).

Pure: it dispatches on the message and content-block class names, so it is hermetically tested with
lightweight fakes and does not import the SDK. The SDK-driving client feeds it real messages.
"""

from __future__ import annotations

from typing import Any

from ..store.models import TokenUsage
from .turn import (
    AgentEvent,
    AssistantMessageEvent,
    TextEvent,
    ToolResultEvent,
    ToolStartEvent,
    UsageEvent,
)


def _split_tool_name(qualified: str) -> tuple[str, str]:
    """``mcp__printer__get_status`` -> ``("printer", "get_status")``; fall back gracefully."""
    if qualified.startswith("mcp__"):
        parts = qualified.split("__", 2)
        if len(parts) == 3:
            return parts[1], parts[2]
    return "builtin", qualified


def translate_message(message: Any) -> list[AgentEvent]:
    """Translate one SDK message into zero or more AgentEvents."""
    kind = type(message).__name__
    if kind == "AssistantMessage":
        return _assistant(message)
    if kind == "UserMessage":
        return _tool_results(message)
    if kind == "ResultMessage":
        return _usage(message)
    return []


def _assistant(message: Any) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    content = getattr(message, "content", []) or []
    for block in content:
        block_kind = type(block).__name__
        if block_kind == "TextBlock":
            text = getattr(block, "text", "")
            if text:
                events.append(TextEvent(text=text))
        elif block_kind == "ToolUseBlock":
            server, tool = _split_tool_name(getattr(block, "name", ""))
            events.append(
                ToolStartEvent(
                    server=server,
                    tool=tool,
                    arguments=dict(getattr(block, "input", {}) or {}),
                    ref=str(getattr(block, "id", "")),
                )
            )
    # The full block list is persisted as the assistant message; ThinkingBlocks may be dropped by
    # replay later, so keep only what round-trips (text and tool-use) in the stored content.
    events.append(AssistantMessageEvent(content=_serialise_blocks(content)))
    return events


def _tool_results(message: Any) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    for block in getattr(message, "content", []) or []:
        if type(block).__name__ == "ToolResultBlock":
            events.append(
                ToolResultEvent(
                    ref=str(getattr(block, "tool_use_id", "")),
                    result_summary=_summarise(getattr(block, "content", "")),
                    is_error=bool(getattr(block, "is_error", False)),
                )
            )
    return events


def _usage(message: Any) -> list[AgentEvent]:
    usage = getattr(message, "usage", None) or {}
    return [
        UsageEvent(
            usage=TokenUsage(
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
                cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0)),
            )
        )
    ]


def _serialise_blocks(content: list[Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for block in content:
        kind = type(block).__name__
        if kind == "TextBlock":
            blocks.append({"type": "text", "text": getattr(block, "text", "")})
        elif kind == "ToolUseBlock":
            blocks.append(
                {
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}),
                }
            )
    return blocks


def _summarise(content: Any, limit: int = 500) -> str:
    """A bounded description of a tool result — not the payload ([store.md §4.7])."""
    text = content if isinstance(content, str) else str(content)
    return text if len(text) <= limit else text[:limit] + "…"
