"""The Agent-SDK adapter: implements the orchestrator's ``AgentClient`` over ``claude-agent-sdk``.

The SDK's ``can_use_tool`` permission callback *is* the approval gate: a read auto-approves, the one
write (``propose_command``) is routed to a human, and anything unlisted is denied — the "deny
unlisted" mechanism ([orchestration.md §2.1, §2.2]). The SDK is imported lazily so the pure
decision logic here is hermetically tested; the streaming glue is exercised by a live test.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable, Callable

from . import sdk_config
from .sdk_translate import translate_message
from .turn import AgentEvent

# approve(command, danger_flags) -> whether the human approved. The composition root wires this to
# the ApprovalGate (publish + await + record); a rejection or timeout returns False.
Approve = Callable[[str, tuple[str, ...]], Awaitable[bool]]
# Per-session builders the composition root supplies.
BuildServers = Callable[[str], "dict[str, Any]"]
BuildPrompt = Callable[[str], str]


def classify_permission(tool_name: str) -> str:
    """For a tool the SDK did not auto-approve: ``"gate"`` for the write, else ``"deny"``.

    Auto-approved read tools never reach this path; only the gated write and any unlisted tool do.
    """
    if tool_name == sdk_config.GATED_WRITE or tool_name.endswith("__propose_command"):
        return "gate"
    return "deny"


async def permission_decision(
    tool_name: str, tool_input: dict[str, Any], approve: Approve
) -> tuple[bool, str]:
    """Decide a non-auto-approved tool call. Pure of the SDK, so it is unit-tested directly."""
    if classify_permission(tool_name) == "deny":
        return False, f"{tool_name} is not a permitted tool"
    command = str(tool_input.get("command", ""))
    danger_flags = tuple(tool_input.get("danger_flags", ()) or ())
    approved = await approve(command, danger_flags)
    return (True, "approved") if approved else (False, "rejected or timed out at the gate")


class ClaudeAgentClient:
    """Drives one turn through ``claude-agent-sdk``, yielding the orchestrator's events."""

    def __init__(
        self,
        *,
        approve: Approve,
        build_servers: BuildServers,
        build_prompt: BuildPrompt,
        resume_lookup: Callable[[str], str | None] = lambda _: None,
        model: str | None = None,
        effort: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._approve = approve
        self._build_servers = build_servers
        self._build_prompt = build_prompt
        self._resume_lookup = resume_lookup
        self._model = model
        self._effort = effort
        self._env = env

    def _make_can_use_tool(self) -> Any:
        """Build the SDK ``can_use_tool`` callback wrapping :func:`permission_decision`."""
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], _context: Any) -> Any:
            allowed, reason = await permission_decision(tool_name, tool_input, self._approve)
            return PermissionResultAllow() if allowed else PermissionResultDeny(message=reason)

        return can_use_tool

    async def run_turn(  # pragma: no cover - live path; needs the SDK + credentials
        self, session_id: str, user_content: list[Any]
    ) -> AsyncIterator[AgentEvent]:
        """Run a turn: build options, stream the SDK's messages, and translate them to events.

        A ``can_use_tool`` callback requires the SDK's **streaming-input** mode, so the prompt is an
        async iterable yielding one user message (not a bare string). The loop ends at the turn's
        ``ResultMessage`` so ``run_turn`` returns after a single turn rather than waiting for more
        input.
        """
        from claude_agent_sdk import query

        options = sdk_config.build_options(
            system_prompt=self._build_prompt(session_id),
            mcp_servers=self._build_servers(session_id),
            can_use_tool=self._make_can_use_tool(),
            model=self._model,
            effort=self._effort,
            resume=self._resume_lookup(session_id),
            env=self._env,
        )

        async def _input_stream() -> AsyncIterator[dict[str, Any]]:
            yield {
                "type": "user",
                "session_id": "",
                "message": {"role": "user", "content": user_content},
                "parent_tool_use_id": None,
            }

        async for message in query(prompt=_input_stream(), options=options):
            for event in translate_message(message):
                yield event
            if type(message).__name__ == "ResultMessage":
                break
