"""Typed, frozen dataclasses for every stored entity.

The store returns these, never rows or dicts ([store.md §3.1](../../docs/design/store.md)). JSON
columns are modelled as structured Python types (tuples, mappings) rather than raw strings, per
task T4.2; the row mappers in [structured_store][printer_debugger.store.structured_store] handle
serialisation at the boundary.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping


class PrinterStatus(enum.Enum):
    """Whether the knowledge-base document supplied everything for a printer."""

    COMPLETE = "complete"
    DEGRADED = "degraded"


class ConfigSource(enum.Enum):
    """Where a configuration snapshot came from — a live read and a file are different claims."""

    FILES = "files"
    MOONRAKER = "moonraker"


class SessionState(enum.Enum):
    """A session is open or closed; closed is not terminal and can be reopened."""

    OPEN = "open"
    CLOSED = "closed"


class BindingReason(enum.Enum):
    """Why a session was bound to a printer."""

    DETECTED = "detected"
    CHOSEN = "chosen"
    REASSIGNED = "reassigned"


class MessageRole(enum.Enum):
    """The author of a conversation message."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ArtifactKind(enum.Enum):
    """What an artifact holds."""

    PROJECT = "project"
    GCODE = "gcode"
    PHOTO = "photo"
    WEBCAM_STILL = "webcam_still"
    AUDIO = "audio"
    PROCEDURE_OUTPUT = "procedure_output"
    PRINTER_STATE = "printer_state"


class FileIndexKind(enum.Enum):
    """Which kind of file an index describes."""

    PROJECT = "project"
    GCODE = "gcode"


class Procedure(enum.Enum):
    """A calibration procedure. The first two are printer-scoped; the rest carry a filament."""

    INPUT_SHAPER = "input_shaper"
    PID_TUNE = "pid_tune"
    FIRST_LAYER = "first_layer"
    PRESSURE_ADVANCE_FLOW = "pressure_advance_flow"
    TEMPERATURE = "temperature"
    STRINGING_RETRACTION = "stringing_retraction"


class ApprovalDecision(enum.Enum):
    """How a proposed printer write was resolved. A timeout is a decision, not a missing row."""

    APPROVED = "approved"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"


PRINTER_SCOPED_PROCEDURES: frozenset[Procedure] = frozenset(
    {Procedure.INPUT_SHAPER, Procedure.PID_TUNE}
)


@dataclass(frozen=True, slots=True)
class Printer:
    """A printer as ingested from the knowledge-base document ([store.md §4.2])."""

    id: str
    name: str
    kb_section: str
    kb_content_hash: str
    status: PrinterStatus
    ingested_at: str
    address: str | None = None
    config_path: str | None = None
    missing: tuple[str, ...] = ()
    absent_since: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """A captured printer configuration, with its provenance ([store.md §4.3])."""

    id: str
    printer_id: str
    source: ConfigSource
    captured_at: str
    contents: str
    discrepancies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Session:
    """One problem being debugged or one calibration being run ([store.md §4.4])."""

    id: str
    name: str
    state: SessionState
    created_at: str
    last_active_at: str
    printer_id: str | None = None
    sdk_session_id: str | None = None
    closed_at: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """A turn's token counts, accumulated onto a session."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass(frozen=True, slots=True)
class PrinterBinding:
    """A record that a session was bound to a printer, kept as history ([store.md §4.5])."""

    id: str
    session_id: str
    printer_id: str
    bound_at: str
    reason: BindingReason


@dataclass(frozen=True, slots=True)
class Message:
    """A conversation message; ``content`` is the full block list, not flattened text."""

    id: str
    session_id: str
    seq: int
    role: MessageRole
    content: list[Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A record of a tool invocation; ``finished_at`` is null while in flight ([store.md §4.7])."""

    id: str
    session_id: str
    server: str
    tool: str
    arguments: Mapping[str, Any]
    started_at: str
    message_id: str | None = None
    result_summary: str | None = None
    is_error: bool = False
    finished_at: str | None = None


@dataclass(frozen=True, slots=True)
class Approval:
    """The audit record of a decision on a proposed printer write ([store.md §4.8])."""

    id: str
    tool_call_id: str
    proposed_command: str
    decision: ApprovalDecision
    decided_by: str
    decided_at: str
    danger_flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Artifact:
    """A stored blob's metadata; the bytes live in the artifact store ([store.md §4.9])."""

    id: str
    session_id: str
    kind: ArtifactKind
    blob_key: str
    size_bytes: int
    content_type: str
    captured_at: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class FileIndex:
    """A built index over a project or G-code artifact ([store.md §4.10])."""

    id: str
    artifact_id: str
    kind: FileIndexKind
    blob_key: str
    format_version: int
    built_at: str


@dataclass(frozen=True, slots=True)
class ProcedureResult:
    """A calibration result, scoped by the database CHECK ([store.md §4.11])."""

    id: str
    session_id: str
    printer_id: str
    procedure: Procedure
    values: Mapping[str, Any]
    recorded_at: str
    filament: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SectionCache:
    """A cached knowledge-base extraction, keyed by section content hash ([store.md §4.12])."""

    content_hash: str
    result: Mapping[str, Any]
    cached_at: str


@dataclass(frozen=True, slots=True)
class StorageAccounting:
    """A report of how much storage the system is using ([store.md §10])."""

    database_bytes: int
    artifact_bytes: int
    artifact_bytes_by_kind: Mapping[str, int] = field(default_factory=dict)
