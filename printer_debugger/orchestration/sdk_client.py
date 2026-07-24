"""The Agent-SDK adapter: implements the orchestrator's ``AgentClient`` over ``claude-agent-sdk``.

The SDK's ``can_use_tool`` permission callback *is* the approval gate: a read auto-approves, the one
write (``propose_command``) is routed to a human, and anything unlisted is denied — the "deny
unlisted" mechanism ([orchestration.md §2.1, §2.2]). The SDK is imported lazily so the pure
decision logic here is hermetically tested; the streaming glue is exercised by a live test.
"""

from __future__ import annotations

import base64
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
# read_artifact(artifact_id) -> (raw bytes, content type). Supplied by the composition root so the
# lean image reference blocks in a persisted message can be inlined for the model at this boundary.
ArtifactReader = Callable[[str], "tuple[bytes, str]"]


def inline_image_blocks(content: list[Any], read_artifact: ArtifactReader) -> list[Any]:
    """Expand each image reference block to a base64 image block; pass every other block through.

    A persisted user message keeps images lean as a reference block
    ``{"type": "image", "artifact_id": id, "media_type": ct}`` — a name, not bytes. The model needs
    the real image, so at the SDK boundary each reference is read and re-emitted as the Anthropic
    image block ``{"type": "image", "source": {"type": "base64", "media_type": ct, "data": b64}}``.
    The block's declared ``media_type`` wins; the reader's content type is the fallback. Text blocks
    and any already-inlined image blocks (those carrying a ``source`` rather than an
    ``artifact_id``) are returned unchanged.
    """
    inlined: list[Any] = []
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "image"
            and block.get("artifact_id")
        ):
            data, content_type = read_artifact(str(block["artifact_id"]))
            media_type = str(block.get("media_type") or content_type)
            encoded = base64.b64encode(data).decode("ascii")
            inlined.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": encoded},
                }
            )
        else:
            inlined.append(block)
    return inlined


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
        read_artifact: ArtifactReader | None = None,
        model: str | None = None,
        effort: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._approve = approve
        self._build_servers = build_servers
        self._build_prompt = build_prompt
        self._resume_lookup = resume_lookup
        self._read_artifact = read_artifact
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
        input. Image reference blocks are inlined to base64 here so the model receives the bytes
        while the persisted message stays lean.
        """
        from claude_agent_sdk import query

        content = (
            inline_image_blocks(user_content, self._read_artifact)
            if self._read_artifact is not None
            else user_content
        )

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
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
            }

        async for message in query(prompt=_input_stream(), options=options):
            for event in translate_message(message):
                yield event
            if type(message).__name__ == "ResultMessage":
                break
