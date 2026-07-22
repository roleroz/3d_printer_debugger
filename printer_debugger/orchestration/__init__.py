"""Session orchestration and the approval gate.

Owns the agent: configuring it, running turns, persisting what happens, and enforcing that no
printer write occurs without a human approving it
([orchestration.md](../../docs/design/orchestration.md)).
"""
