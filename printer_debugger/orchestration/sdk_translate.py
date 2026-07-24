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
    ErrorEvent,
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
        return _result(message)
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


def _result(message: Any) -> list[AgentEvent]:
    """A ``ResultMessage`` yields an ``ErrorEvent`` when the turn failed, then usage if present.

    A failed turn (an API error, a server that could not start, a permission denial, ``max_turns``,
    an abort, …) previously produced only a ``UsageEvent`` and so was invisible; now it also emits a
    concise ``ErrorEvent`` describing the failure so the user and the console can see it.
    """
    events: list[AgentEvent] = []
    error = _error_message(message)
    if error is not None:
        events.append(ErrorEvent(message=error))
    usage = getattr(message, "usage", None)
    if usage is not None:
        events.append(
            UsageEvent(
                usage=TokenUsage(
                    input_tokens=int(usage.get("input_tokens", 0)),
                    output_tokens=int(usage.get("output_tokens", 0)),
                    cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
                    cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0)),
                )
            )
        )
    return events


def _error_message(message: Any) -> str | None:
    """Build a human failure message for a ``ResultMessage``, or None when the turn succeeded.

    A turn is failed when ``is_error`` is set, the ``subtype`` is present but not ``"success"``, or
    ``terminal_reason`` indicates an abort. The message is assembled from whichever diagnostic
    fields are available, omitting the empty ones.
    """
    subtype = str(getattr(message, "subtype", "") or "")
    is_error = bool(getattr(message, "is_error", False))
    terminal = getattr(message, "terminal_reason", None)
    subtype_failed = subtype not in ("", "success")
    if not (is_error or subtype_failed or _indicates_abort(terminal)):
        return None
    status = getattr(message, "api_error_status", None)
    errors = getattr(message, "errors", None)
    result = getattr(message, "result", None)
    parts: list[str] = []
    if subtype:
        parts.append(f"subtype={subtype}")
    if status is not None:
        parts.append(f"status={status}")
    if terminal:
        parts.append(f"terminal={terminal}")
    detail = "; ".join(str(e) for e in errors) if errors else str(result or "")
    head = "The agent turn failed"
    if parts:
        head += " (" + ", ".join(parts) + ")"
    if detail:
        head += f": {detail}"
    return head


def _indicates_abort(terminal_reason: Any) -> bool:
    """Whether a ``terminal_reason`` names an abort/cancellation rather than a clean finish."""
    return bool(terminal_reason) and "abort" in str(terminal_reason).lower()


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
