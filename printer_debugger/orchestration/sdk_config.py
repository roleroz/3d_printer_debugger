"""Agent-SDK configuration: the four permission mechanisms as data ([orchestration.md §2.2]).

Redundant on purpose — allowlist, disallow list, deny-unlisted mode, and the permission callback —
so no single mistake grants shell access on a host that sits on the printer network. This module is
pure data + a lazy options builder, so the security policy is hermetically testable without the SDK.
"""

from __future__ import annotations

from typing import Any

# Host-touching Claude Code built-ins, explicitly disallowed. Reads that touch neither host nor LAN
# (web search/fetch) are allowed instead ([orchestration.md §2.2]).
HOST_BUILTINS: tuple[str, ...] = (
    "Bash", "BashOutput", "KillShell", "KillBash", "Read", "Write", "Edit", "MultiEdit",
    "NotebookEdit", "Glob", "Grep", "Task", "TodoWrite", "SlashCommand", "ExitPlanMode",
)

WEB_TOOLS: tuple[str, ...] = ("WebSearch", "WebFetch")

# The read tools of each in-process MCP server. propose_command is deliberately absent — it is the
# one write, and it reaches the printer only through the approval gate via can_use_tool.
PROJECT_READ_TOOLS: tuple[str, ...] = (
    "get_settings", "get_modified_settings", "get_objects", "get_plate_layout",
    "get_object_render", "get_object_dimensions", "get_thumbnail", "get_printer_identity",
)
GCODE_READ_TOOLS: tuple[str, ...] = (
    "get_header", "get_layer_table", "locate", "summarise_layers", "get_commands", "get_region",
    "get_state_at", "get_events", "get_anomalies", "get_thumbnail", "index_status",
)
PRINTER_READ_TOOLS: tuple[str, ...] = (
    "get_status", "get_temperatures", "get_position", "get_config", "get_runtime_state",
    "get_logs", "capture_still",
)

# The one gated write. Not in the allowlist, so the SDK routes it to can_use_tool → the gate.
GATED_WRITE: str = "mcp__printer__propose_command"


def _qualify(server: str, tools: tuple[str, ...]) -> list[str]:
    return [f"mcp__{server}__{tool}" for tool in tools]


def allowed_tools() -> list[str]:
    """Every auto-approved tool: the read MCP tools plus web search and fetch (no write)."""
    return (
        _qualify("project", PROJECT_READ_TOOLS)
        + _qualify("gcode", GCODE_READ_TOOLS)
        + _qualify("printer", PRINTER_READ_TOOLS)
        + list(WEB_TOOLS)
    )


def disallowed_tools() -> list[str]:
    """The host-touching built-ins, explicitly named."""
    return list(HOST_BUILTINS)


def build_options(
    *,
    system_prompt: str,
    mcp_servers: dict[str, Any],
    can_use_tool: Any,
    model: str | None = None,
    effort: str | None = None,
    resume: str | None = None,
    env: dict[str, str] | None = None,
) -> Any:
    """Construct ``ClaudeAgentOptions`` (lazy import). Thinking is adaptive; unlisted tools deny.

    ``permission_mode='default'`` means a tool not in ``allowed_tools`` is routed to
    ``can_use_tool`` — where the gate handles the write and everything else is denied. ``env``
    carries the subscription OAuth token (``CLAUDE_CODE_OAUTH_TOKEN``) to the SDK explicitly, in
    addition to it being present in the process environment.
    """
    from claude_agent_sdk import ClaudeAgentOptions, ThinkingConfigAdaptive

    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        allowed_tools=allowed_tools(),
        disallowed_tools=disallowed_tools(),
        permission_mode="default",
        can_use_tool=can_use_tool,
        mcp_servers=mcp_servers,
        model=model,
        effort=effort,
        thinking=ThinkingConfigAdaptive(type="adaptive"),
        resume=resume,
        include_partial_messages=True,
        env=env or {},
    )
