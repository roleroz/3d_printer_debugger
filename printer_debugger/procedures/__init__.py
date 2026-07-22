"""The calibration catalog.

Procedures are data, not code ([procedures.md §2.1](../../docs/design/procedures.md)): structured
documents loaded at startup and placed in the system prompt. This module supplies the knowledge and
one constraint (scope); it does not supply control flow — the agent runs procedures with the
ordinary tools, every command through the same approval gate.
"""
