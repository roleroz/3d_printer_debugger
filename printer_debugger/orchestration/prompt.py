"""System-prompt assembly, ordered stable content first for caching ([orchestration.md §4.1]).

Items 1 and 2 (role/method/principles and the procedure catalog) are identical across every
session, so they cache across sessions, not merely within one. Session state changes every turn and
sits last. The prompt also carries the policy rules that are not mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass

ROLE_AND_METHOD = """\
You are a 3D-printing calibration and diagnosis assistant. You work one problem per session. You
reason from evidence: cite web-derived claims and rank them below first-hand evidence; distinguish
established facts from hypotheses; judge photo quality and ask for a better photo rather than
guessing; never present a configuration snapshot value as live runtime state. Every printer write
goes through a human approval gate — propose, explain, and wait."""


@dataclass(frozen=True, slots=True)
class PromptInputs:
    """The pieces assembled into a system prompt."""

    procedure_catalog: str
    shared_context: str
    printer_section: str
    printer_snapshot: str
    session_state: str


def assemble(inputs: PromptInputs) -> str:
    """Assemble the system prompt with stable content first.

    Ordering (stable → volatile): role/method, procedure catalog, shared KB context, the bound
    printer's section and snapshot, then session-specific state.
    """
    return "\n\n".join(
        segment
        for segment in (
            _stable_prefix(inputs.procedure_catalog),
            _section("Shared context", inputs.shared_context),
            _section("Bound printer", inputs.printer_section),
            _section("Configuration snapshot", inputs.printer_snapshot),
            _section("Session state", inputs.session_state),
        )
        if segment
    )


def stable_prefix(procedure_catalog: str) -> str:
    """The cache-shared prefix: role/method plus the catalog, identical across sessions."""
    return _stable_prefix(procedure_catalog)


def _stable_prefix(procedure_catalog: str) -> str:
    parts = [ROLE_AND_METHOD]
    if procedure_catalog:
        parts.append(_section("Procedure catalog", procedure_catalog))
    return "\n\n".join(parts)


def _section(title: str, body: str) -> str:
    body = (body or "").strip()
    if not body:
        return ""
    return f"## {title}\n{body}"
