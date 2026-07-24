"""Wrap a tool surface as an in-process SDK-MCP server ([architecture.md §3.4]).

The Agent SDK hosts MCP servers in-process; each of our tool classes (``ProjectTools``,
``GcodeTools``, ``PrinterTools``) becomes one server whose tools are its public methods. The SDK's
``tool`` decorator wants an async ``def(args: dict) -> dict`` with an MCP-shaped result, so each
sync method is adapted: a JSON input schema is derived from its signature, it is called with the
decoded arguments, and its bounded dict is returned as JSON text content. A :class:`ToolError`
becomes an MCP error result the model can read and adapt to.

The SDK is imported lazily inside :func:`build_sdk_server`, so the pure adapters here — schema
derivation and result formatting — carry no SDK dependency and are hermetically tested. Building an
actual server (the one line that touches the SDK) is exercised only on the live path.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable

from .gcode_server import GcodeTools
from .project_server import ProjectTools
from .responses import ToolError

# JSON-schema type for a Python annotation. Anything unrecognised is described as a string, which
# is the SDK's most permissive input and never wrong at the wire level. String keys cover the case
# where ``from __future__ import annotations`` leaves annotations as unresolved strings.
_JSON_TYPES: dict[Any, str] = {
    int: "integer",
    float: "number",
    str: "string",
    bool: "boolean",
    "int": "integer",
    "float": "number",
    "str": "string",
    "bool": "boolean",
}


def tool_methods(instance: object) -> dict[str, Callable[..., Any]]:
    """Return the public, callable tool methods of a tools instance, keyed by name."""
    return {
        name: getattr(instance, name)
        for name, _ in inspect.getmembers(instance, predicate=inspect.ismethod)
        if not name.startswith("_")
    }


def input_schema(method: Callable[..., Any]) -> dict[str, Any]:
    """Derive a minimal JSON input schema for a tool method from its signature.

    A parameter with no default is required; its JSON type comes from its annotation, defaulting
    to ``string``. ``self`` and any ``*args``/``**kwargs`` are skipped.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    try:
        signature = inspect.signature(method, eval_str=True)
    except (NameError, TypeError):
        # ``from __future__ import annotations`` can leave an annotation that will not evaluate;
        # fall back to the raw (string) annotations, which _json_type also understands.
        signature = inspect.signature(method)
    for name, parameter in signature.parameters.items():
        if name == "self" or parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        properties[name] = {"type": _json_type(parameter.annotation)}
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _json_type(annotation: Any) -> str:
    """Map a Python annotation (including ``T | None`` unions) to a JSON-schema type name."""
    if annotation in _JSON_TYPES:
        return _JSON_TYPES[annotation]
    # A union such as ``int | None`` (a types.UnionType) — use the first non-None member's type.
    args = getattr(annotation, "__args__", ())
    for arg in args:
        if arg is not type(None) and arg in _JSON_TYPES:
            return _JSON_TYPES[arg]
    return "string"


def format_result(payload: Any) -> dict[str, Any]:
    """Format a tool's return value as an MCP text-content result."""
    return {"content": [{"type": "text", "text": _to_text(payload)}]}


def format_error(exc: Exception) -> dict[str, Any]:
    """Format a raised tool error as an MCP error result the model can read and adapt to.

    A :class:`ToolError`'s string already carries its narrowing guidance, so it is used verbatim.
    """
    return {"content": [{"type": "text", "text": str(exc)}], "is_error": True}


def _to_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)


def build_sdk_server(name: str, instance: object):  # pragma: no cover - needs the Agent SDK
    """Create the in-process MCP server named ``name`` from a tools instance.

    The SDK qualifies each tool as ``mcp__{name}__{method}``, matching the allowlist in
    ``sdk_config`` (which passes the bare method name to ``tool``). The SDK import is lazy.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    wrapped = []
    for method_name, method in tool_methods(instance).items():
        description = (method.__doc__ or method_name).strip().splitlines()[0]
        wrapped.append(
            tool(method_name, description, input_schema(method))(_adapt(method))
        )
    return create_sdk_mcp_server(name=name, tools=wrapped)


def _adapt(method: Callable[..., Any]):  # pragma: no cover - exercised via build_sdk_server (live)
    """Adapt one sync tool method into the SDK's ``async def(args) -> dict`` handler."""

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return format_result(method(**(args or {})))
        except ToolError as exc:
            return format_error(exc)

    return handler


def build_project_server(tools: ProjectTools):  # pragma: no cover - needs the Agent SDK
    """Create the in-process ``project`` MCP server from a ProjectTools instance."""
    return build_sdk_server("project", tools)


def build_gcode_server(tools: GcodeTools):  # pragma: no cover - needs the Agent SDK
    """Create the in-process ``gcode`` MCP server from a GcodeTools instance."""
    return build_sdk_server("gcode", tools)
