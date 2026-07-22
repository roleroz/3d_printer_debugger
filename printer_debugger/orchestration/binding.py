"""Printer binding: detect from project identity, prompt when unsure, mismatch as a finding.

Silent on a confident match, prompted otherwise ([orchestration.md §3, §3.1]). A disagreement
between the project's printer and the session's bound printer is raised as a diagnostic finding
rather than reconciled quietly — slicing for one machine and printing on another explains a whole
class of defects.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..store.models import Printer


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    """The identification inputs read from a project ([file_indexing get_printer_identity])."""

    printer_settings_id: str | None = None
    nozzle_diameter: float | None = None
    printable_area: str | None = None


@dataclass(frozen=True, slots=True)
class BindingSuggestion:
    """The outcome of matching a project against the known printers."""

    printer_id: str | None
    confident: bool
    reason: str
    candidates: tuple[str, ...] = field(default_factory=tuple)


def detect(identity: ProjectIdentity, printers: list[Printer]) -> BindingSuggestion:
    """Match a project's printer identity against the known printers.

    Confident on exactly one match, prompted (not confident) on zero or several.
    """
    present = [p for p in printers if p.absent_since is None]
    if not present:
        return BindingSuggestion(None, False, "no printers are defined; choose one to add")

    matches = [p for p in present if _matches(identity, p)]
    if len(matches) == 1:
        return BindingSuggestion(matches[0].id, True, "matched the project's printer preset")
    if len(matches) > 1:
        return BindingSuggestion(
            None, False, "several printers match; choose one",
            candidates=tuple(p.name for p in matches),
        )
    return BindingSuggestion(
        None, False, "no printer matched the project; choose one",
        candidates=tuple(p.name for p in present),
    )


def _matches(identity: ProjectIdentity, printer: Printer) -> bool:
    """A printer matches when the preset name contains all the printer's distinctive name tokens.

    Requiring every significant (alphabetic, >2-char) token — not just any — keeps a shared vendor
    word like "voron" from matching a different model ("Trident" vs "Switchwire").
    """
    preset = (identity.printer_settings_id or "").lower()
    if not preset:
        return False
    tokens = [
        t for t in printer.name.lower().split() if len(t) > 2 and any(c.isalpha() for c in t)
    ]
    return bool(tokens) and all(token in preset for token in tokens)


def mismatch_finding(identity: ProjectIdentity, bound: Printer) -> str | None:
    """Return a finding if the project's printer disagrees with the bound printer, else None."""
    if _matches(identity, bound):
        return None
    return (
        f"The project was sliced for '{identity.printer_settings_id}', which does not match the "
        f"bound printer '{bound.name}'. Slicing for one machine and printing on another has a "
        f"characteristic signature — wrong nozzle diameter, wrong flow, geometry outside the build "
        f"volume — and it explains a whole class of defects."
    )
