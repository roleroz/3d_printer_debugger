"""In-process MCP servers wrapping the tool surfaces.

Thin: it exposes the ``project`` and ``gcode`` tool methods through the Agent SDK's in-process
SDK-MCP mechanism ([architecture.md §3.4]). The SDK is imported lazily so the tool logic — which is
what the tests exercise — carries no SDK dependency. The ``printer`` server lives in the
printer-access module; all three share the response discipline in
[responses][printer_debugger.indexing.responses].
"""

from __future__ import annotations

import inspect
from typing import Any

from .gcode_server import GcodeTools
from .project_server import ProjectTools


def _tool_methods(instance: object) -> dict[str, Any]:
    """Return the public, callable tool methods of a tools instance, by name."""
    return {
        name: getattr(instance, name)
        for name, _ in inspect.getmembers(instance, predicate=inspect.ismethod)
        if not name.startswith("_")
    }


def build_project_server(tools: ProjectTools):  # pragma: no cover - needs the Agent SDK
    """Create the in-process ``project`` MCP server from a ProjectTools instance."""
    return _build_server("project", _tool_methods(tools))


def build_gcode_server(tools: GcodeTools):  # pragma: no cover - needs the Agent SDK
    """Create the in-process ``gcode`` MCP server from a GcodeTools instance."""
    return _build_server("gcode", _tool_methods(tools))


def _build_server(name: str, methods: dict[str, Any]):  # pragma: no cover - needs the Agent SDK
    from claude_agent_sdk import create_sdk_mcp_server, tool  # lazy: SDK-only dependency

    wrapped = []
    for tool_name, method in methods.items():
        wrapped.append(tool(f"{name}.{tool_name}", method.__doc__ or tool_name, {})(method))
    return create_sdk_mcp_server(name=name, tools=wrapped)
