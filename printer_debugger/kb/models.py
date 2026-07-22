"""Value types for knowledge-base ingestion."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Section:
    """A section of the document: its heading, and the verbatim text including the heading."""

    heading: str
    text: str
    index: int  # 0-based position among sections, for identifying an unnamed one to the user.


@dataclass(frozen=True, slots=True)
class SectionedDocument:
    """A document split into a preamble (shared context) and its sections."""

    preamble: str
    sections: tuple[Section, ...]


@dataclass(frozen=True, slots=True)
class SectionExtraction:
    """What the model returns for one section: is it a printer, and its identifying values."""

    is_printer: bool
    name: str | None = None
    address: str | None = None
    config_path: str | None = None


class DiscrepancyKind(enum.Enum):
    """The three kinds of configuration disagreement detected at ingest ([kb_ingestion.md §5])."""

    SAVED_SUPERSEDES_FILE = "saved_supersedes_file"
    COMMENTED_DIFFERS_FROM_ACTIVE = "commented_differs_from_active"
    FILES_DIFFER_FROM_LIVE = "files_differ_from_live"


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """A recorded disagreement between configuration sources."""

    kind: DiscrepancyKind
    section: str
    key: str
    left: str
    right: str

    def describe(self) -> str:
        """A one-line human description, for the recorded list and for surfacing when relevant."""
        return f"[{self.section}] {self.key}: {self.left!r} vs {self.right!r} ({self.kind.value})"


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """The result of an ingest pass, for reporting to the user and logs."""

    printers_upserted: tuple[str, ...] = ()
    printers_degraded: tuple[str, ...] = ()
    printers_absent: tuple[str, ...] = ()
    unnamed_sections: tuple[str, ...] = ()  # descriptions of sections that could not be stored.
    shared_context_headings: tuple[str, ...] = ()
    messages: tuple[str, ...] = ()  # user-facing notes (degraded reasons, name requests).


@dataclass(frozen=True, slots=True)
class OrchestratorView:
    """What the orchestrator receives for a bound printer ([kb_ingestion.md §6])."""

    printer_id: str
    section_text: str
    shared_context: str
    snapshot_contents: str | None = None
    snapshot_source: str | None = None
    snapshot_captured_at: str | None = None
    discrepancies: tuple[str, ...] = field(default_factory=tuple)
