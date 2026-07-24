"""The turn loop ([orchestration.md §5]).

Input is persisted, then handed to the agent client; streamed text, tool calls, and results are
persisted as they complete, and usage is accumulated onto the session. A tool call is persisted
when it starts, with a null completion time, so an interrupted process is recognisable on restart.
The agent client is a seam so the loop is tested without the real SDK (which the adapter wraps).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Protocol

from ..store.models import MessageRole, TokenUsage
from ..store.structured_store import StructuredStore


@dataclass(frozen=True, slots=True)
class TextEvent:
    """A chunk of assistant text."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolStartEvent:
    """A tool call beginning."""

    server: str
    tool: str
    arguments: dict[str, Any]
    ref: str  # correlates with the matching ToolResultEvent.


@dataclass(frozen=True, slots=True)
class ToolResultEvent:
    """A tool call completing."""

    ref: str
    result_summary: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class AssistantMessageEvent:
    """The assistant's full message block list for the turn."""

    content: list[Any]


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """Token usage for the turn."""

    usage: TokenUsage


AgentEvent = TextEvent | ToolStartEvent | ToolResultEvent | AssistantMessageEvent | UsageEvent


class AgentClient(Protocol):
    """Runs one turn, yielding events. The real implementation wraps the Agent SDK."""

    def run_turn(self, session_id: str, user_content: list[Any]) -> AsyncIterator[AgentEvent]: ...


class TurnLoop:
    """Drives one turn: persist, stream, persist incrementally, accumulate usage."""

    def __init__(
        self,
        store: StructuredStore,
        client: AgentClient,
        forward_text: Callable[[str], None] = lambda _: None,
        on_event: Callable[[AgentEvent], None] = lambda _: None,
    ) -> None:
        self._store = store
        self._client = client
        self._forward = forward_text
        self._on_event = on_event

    async def run(self, session_id: str, user_content: list[Any]) -> None:
        """Run a single turn to completion, persisting everything as it happens."""
        self._store.add_message(session_id, MessageRole.USER, user_content)
        refs: dict[str, str] = {}
        async for event in self._client.run_turn(session_id, user_content):
            self._on_event(event)
            self._handle(session_id, event, refs)
        self._store.touch_session(session_id)

    def _handle(self, session_id: str, event: AgentEvent, refs: dict[str, str]) -> None:
        if isinstance(event, TextEvent):
            self._forward(event.text)
        elif isinstance(event, ToolStartEvent):
            call = self._store.start_tool_call(
                session_id=session_id,
                server=event.server,
                tool=event.tool,
                arguments=event.arguments,
            )
            refs[event.ref] = call.id
        elif isinstance(event, ToolResultEvent):
            call_id = refs.get(event.ref)
            if call_id is not None:
                self._store.finish_tool_call(
                    call_id, result_summary=event.result_summary, is_error=event.is_error
                )
        elif isinstance(event, AssistantMessageEvent):
            self._store.add_message(session_id, MessageRole.ASSISTANT, event.content)
        elif isinstance(event, UsageEvent):
            self._store.accumulate_usage(session_id, event.usage)
