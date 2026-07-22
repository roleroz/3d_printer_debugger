"""``StructuredStore``: typed methods over the SQLite entities.

Hand-written SQL through the standard-library driver, with explicit row mappers to frozen
dataclasses. No ORM, no query builder, and deliberately no general query method — a new access
pattern gets a new named method so every one is visible and indexable
([store.md §3.1, §2.3](../../docs/design/store.md)).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from . import ids
from .db import Database
from .errors import ConstraintViolationError
from .models import (
    Approval,
    ApprovalDecision,
    Artifact,
    ArtifactKind,
    BindingReason,
    ConfigSnapshot,
    ConfigSource,
    FileIndex,
    FileIndexKind,
    Message,
    MessageRole,
    Printer,
    PrinterBinding,
    PrinterStatus,
    Procedure,
    ProcedureResult,
    SectionCache,
    Session,
    SessionState,
    TokenUsage,
    ToolCall,
)

# -- JSON helpers ------------------------------------------------------------------------------


def _dump(value: Any) -> str:
    """Serialise a structured value to compact JSON for a TEXT column."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _load_list(text: str | None) -> tuple[str, ...]:
    """Parse a JSON array TEXT column into a tuple, treating NULL as empty."""
    if text is None:
        return ()
    return tuple(json.loads(text))


def _load_obj(text: str | None) -> dict[str, Any]:
    """Parse a JSON object TEXT column into a dict, treating NULL as empty."""
    if text is None:
        return {}
    return dict(json.loads(text))


# -- row mappers -------------------------------------------------------------------------------


def _printer(row: sqlite3.Row) -> Printer:
    return Printer(
        id=row["id"],
        name=row["name"],
        kb_section=row["kb_section"],
        kb_content_hash=row["kb_content_hash"],
        status=PrinterStatus(row["status"]),
        ingested_at=row["ingested_at"],
        address=row["address"],
        config_path=row["config_path"],
        missing=_load_list(row["missing"]),
        absent_since=row["absent_since"],
    )


def _config_snapshot(row: sqlite3.Row) -> ConfigSnapshot:
    return ConfigSnapshot(
        id=row["id"],
        printer_id=row["printer_id"],
        source=ConfigSource(row["source"]),
        captured_at=row["captured_at"],
        contents=row["contents"],
        discrepancies=_load_list(row["discrepancies"]),
    )


def _session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        name=row["name"],
        state=SessionState(row["state"]),
        created_at=row["created_at"],
        last_active_at=row["last_active_at"],
        printer_id=row["printer_id"],
        sdk_session_id=row["sdk_session_id"],
        closed_at=row["closed_at"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_creation_tokens=row["cache_creation_tokens"],
    )


def _binding(row: sqlite3.Row) -> PrinterBinding:
    return PrinterBinding(
        id=row["id"],
        session_id=row["session_id"],
        printer_id=row["printer_id"],
        bound_at=row["bound_at"],
        reason=BindingReason(row["reason"]),
    )


def _message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        session_id=row["session_id"],
        seq=row["seq"],
        role=MessageRole(row["role"]),
        content=json.loads(row["content"]),
        created_at=row["created_at"],
    )


def _tool_call(row: sqlite3.Row) -> ToolCall:
    return ToolCall(
        id=row["id"],
        session_id=row["session_id"],
        server=row["server"],
        tool=row["tool"],
        arguments=_load_obj(row["arguments"]),
        started_at=row["started_at"],
        message_id=row["message_id"],
        result_summary=row["result_summary"],
        is_error=bool(row["is_error"]),
        finished_at=row["finished_at"],
    )


def _approval(row: sqlite3.Row) -> Approval:
    return Approval(
        id=row["id"],
        tool_call_id=row["tool_call_id"],
        proposed_command=row["proposed_command"],
        decision=ApprovalDecision(row["decision"]),
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        danger_flags=_load_list(row["danger_flags"]),
    )


def _artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=row["id"],
        session_id=row["session_id"],
        kind=ArtifactKind(row["kind"]),
        blob_key=row["blob_key"],
        size_bytes=row["size_bytes"],
        content_type=row["content_type"],
        captured_at=row["captured_at"],
        note=row["note"],
    )


def _file_index(row: sqlite3.Row) -> FileIndex:
    return FileIndex(
        id=row["id"],
        artifact_id=row["artifact_id"],
        kind=FileIndexKind(row["kind"]),
        blob_key=row["blob_key"],
        format_version=row["format_version"],
        built_at=row["built_at"],
    )


def _procedure_result(row: sqlite3.Row) -> ProcedureResult:
    return ProcedureResult(
        id=row["id"],
        session_id=row["session_id"],
        printer_id=row["printer_id"],
        procedure=Procedure(row["procedure"]),
        values=_load_obj(row["values_json"]),
        recorded_at=row["recorded_at"],
        filament=row["filament"],
        evidence=_load_list(row["evidence"]),
    )


def _section_cache(row: sqlite3.Row) -> SectionCache:
    return SectionCache(
        content_hash=row["content_hash"],
        result=_load_obj(row["result"]),
        cached_at=row["cached_at"],
    )


class StructuredStore:
    """Typed access to the structured half of the store."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # -- printers (T3.1) -------------------------------------------------------------------

    def create_printer(
        self,
        *,
        name: str,
        kb_section: str,
        kb_content_hash: str,
        status: PrinterStatus,
        address: str | None = None,
        config_path: str | None = None,
        missing: Sequence[str] = (),
    ) -> Printer:
        """Insert a printer ingested from the knowledge-base document and return it."""
        printer = Printer(
            id=ids.new_id(ids.PRINTER),
            name=name,
            kb_section=kb_section,
            kb_content_hash=kb_content_hash,
            status=status,
            ingested_at=ids.utcnow_iso(),
            address=address,
            config_path=config_path,
            missing=tuple(missing),
        )
        with self._writing() as conn:
            conn.execute(
                "INSERT INTO printer (id, name, address, config_path, kb_section, "
                "kb_content_hash, status, missing, absent_since, ingested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    printer.id,
                    printer.name,
                    printer.address,
                    printer.config_path,
                    printer.kb_section,
                    printer.kb_content_hash,
                    printer.status.value,
                    _dump(list(printer.missing)) if printer.missing else None,
                    printer.absent_since,
                    printer.ingested_at,
                ),
            )
        return printer

    def update_printer(
        self,
        printer_id: str,
        *,
        kb_section: str,
        kb_content_hash: str,
        status: PrinterStatus,
        address: str | None,
        config_path: str | None,
        missing: Sequence[str],
    ) -> None:
        """Update a printer's ingested fields on re-ingest, clearing any absence marker."""
        with self._writing() as conn:
            conn.execute(
                "UPDATE printer SET kb_section=?, kb_content_hash=?, status=?, address=?, "
                "config_path=?, missing=?, absent_since=NULL, ingested_at=? WHERE id=?",
                (
                    kb_section,
                    kb_content_hash,
                    status.value,
                    address,
                    config_path,
                    _dump(list(missing)) if missing else None,
                    ids.utcnow_iso(),
                    printer_id,
                ),
            )

    def mark_printer_absent(self, printer_id: str, absent_since: str | None = None) -> None:
        """Record that a printer's section was removed from the document, keeping the row."""
        with self._writing() as conn:
            conn.execute(
                "UPDATE printer SET absent_since=? WHERE id=? AND absent_since IS NULL",
                (absent_since or ids.utcnow_iso(), printer_id),
            )

    def get_printer(self, printer_id: str) -> Printer | None:
        """Fetch a printer by id, or None."""
        return self._fetch_one("SELECT * FROM printer WHERE id=?", (printer_id,), _printer)

    def get_printer_by_name(self, name: str) -> Printer | None:
        """Fetch a printer by its unique name, or None."""
        return self._fetch_one("SELECT * FROM printer WHERE name=?", (name,), _printer)

    def list_printers(self) -> list[Printer]:
        """List all printers, present and absent, by name."""
        return self._fetch_all("SELECT * FROM printer ORDER BY name", (), _printer)

    # -- config snapshots (T3.1) -----------------------------------------------------------

    def add_config_snapshot(
        self,
        *,
        printer_id: str,
        source: ConfigSource,
        contents: str,
        discrepancies: Sequence[str] = (),
    ) -> ConfigSnapshot:
        """Append a configuration snapshot; snapshots accumulate rather than overwrite."""
        snapshot = ConfigSnapshot(
            id=ids.new_id(ids.CONFIG_SNAPSHOT),
            printer_id=printer_id,
            source=source,
            captured_at=ids.utcnow_iso(),
            contents=contents,
            discrepancies=tuple(discrepancies),
        )
        with self._writing() as conn:
            conn.execute(
                "INSERT INTO config_snapshot (id, printer_id, source, captured_at, contents, "
                "discrepancies) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    snapshot.id,
                    snapshot.printer_id,
                    snapshot.source.value,
                    snapshot.captured_at,
                    snapshot.contents,
                    _dump(list(snapshot.discrepancies)) if snapshot.discrepancies else None,
                ),
            )
        return snapshot

    def latest_config_snapshot(self, printer_id: str) -> ConfigSnapshot | None:
        """Return the most recent configuration snapshot for a printer, or None."""
        return self._fetch_one(
            "SELECT * FROM config_snapshot WHERE printer_id=? "
            "ORDER BY captured_at DESC, rowid DESC LIMIT 1",
            (printer_id,),
            _config_snapshot,
        )

    def list_config_snapshots(self, printer_id: str) -> list[ConfigSnapshot]:
        """List a printer's configuration snapshots, newest first."""
        return self._fetch_all(
            "SELECT * FROM config_snapshot WHERE printer_id=? "
            "ORDER BY captured_at DESC, rowid DESC",
            (printer_id,),
            _config_snapshot,
        )

    # -- sessions and bindings (T3.2) ------------------------------------------------------

    def create_session(
        self, *, name: str, printer_id: str | None = None, sdk_session_id: str | None = None
    ) -> Session:
        """Create a session; ``printer_id`` is null only until binding resolves."""
        now = ids.utcnow_iso()
        session = Session(
            id=ids.new_id(ids.SESSION),
            name=name,
            state=SessionState.OPEN,
            created_at=now,
            last_active_at=now,
            printer_id=printer_id,
            sdk_session_id=sdk_session_id,
        )
        with self._writing() as conn:
            conn.execute(
                "INSERT INTO session (id, name, printer_id, state, sdk_session_id, created_at, "
                "last_active_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session.id,
                    session.name,
                    session.printer_id,
                    session.state.value,
                    session.sdk_session_id,
                    session.created_at,
                    session.last_active_at,
                ),
            )
        return session

    def get_session(self, session_id: str) -> Session | None:
        """Fetch a session by id, or None."""
        return self._fetch_one("SELECT * FROM session WHERE id=?", (session_id,), _session)

    def list_sessions(self) -> list[Session]:
        """List sessions, most recently active first ([spec.md §5.2])."""
        return self._fetch_all(
            "SELECT * FROM session ORDER BY last_active_at DESC, rowid DESC", (), _session
        )

    def rename_session(self, session_id: str, name: str) -> None:
        """Rename a session."""
        with self._writing() as conn:
            conn.execute("UPDATE session SET name=? WHERE id=?", (name, session_id))

    def close_session(self, session_id: str) -> None:
        """Close a session; closed is not terminal and can be reopened."""
        now = ids.utcnow_iso()
        with self._writing() as conn:
            conn.execute(
                "UPDATE session SET state='closed', closed_at=?, last_active_at=? WHERE id=?",
                (now, now, session_id),
            )

    def reopen_session(self, session_id: str) -> None:
        """Reopen a closed session."""
        with self._writing() as conn:
            conn.execute(
                "UPDATE session SET state='open', closed_at=NULL, last_active_at=? WHERE id=?",
                (ids.utcnow_iso(), session_id),
            )

    def touch_session(self, session_id: str) -> None:
        """Bump a session's last-active time to now."""
        with self._writing() as conn:
            conn.execute(
                "UPDATE session SET last_active_at=? WHERE id=?",
                (ids.utcnow_iso(), session_id),
            )

    def set_sdk_session_id(self, session_id: str, sdk_session_id: str) -> None:
        """Record the Agent SDK's own session identifier for resume."""
        with self._writing() as conn:
            conn.execute(
                "UPDATE session SET sdk_session_id=? WHERE id=?", (sdk_session_id, session_id)
            )

    def accumulate_usage(self, session_id: str, usage: TokenUsage) -> None:
        """Add a turn's token counts onto a session. Cost is never stored."""
        with self._writing() as conn:
            conn.execute(
                "UPDATE session SET input_tokens=input_tokens+?, output_tokens=output_tokens+?, "
                "cache_read_tokens=cache_read_tokens+?, "
                "cache_creation_tokens=cache_creation_tokens+? WHERE id=?",
                (
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_read_tokens,
                    usage.cache_creation_tokens,
                    session_id,
                ),
            )

    def bind_printer(
        self, session_id: str, printer_id: str, reason: BindingReason
    ) -> PrinterBinding:
        """Bind a session to a printer and record the binding as history in one transaction.

        A reassignment writes a new binding row so findings established beforehand are not
        silently reattributed ([store.md §4.5]).
        """
        binding = PrinterBinding(
            id=ids.new_id(ids.BINDING),
            session_id=session_id,
            printer_id=printer_id,
            bound_at=ids.utcnow_iso(),
            reason=reason,
        )
        with self._writing() as conn:
            conn.execute(
                "UPDATE session SET printer_id=? WHERE id=?", (printer_id, session_id)
            )
            conn.execute(
                "INSERT INTO printer_binding (id, session_id, printer_id, bound_at, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    binding.id,
                    binding.session_id,
                    binding.printer_id,
                    binding.bound_at,
                    binding.reason.value,
                ),
            )
        return binding

    def list_bindings(self, session_id: str) -> list[PrinterBinding]:
        """List a session's printer bindings in chronological order."""
        return self._fetch_all(
            "SELECT * FROM printer_binding WHERE session_id=? ORDER BY bound_at, rowid",
            (session_id,),
            _binding,
        )

    # -- messages (T3.2) -------------------------------------------------------------------

    def add_message(
        self, session_id: str, role: MessageRole, content: Sequence[Any]
    ) -> Message:
        """Append a message, assigning the next sequence number within the session."""
        message_id = ids.new_id(ids.MESSAGE)
        created_at = ids.utcnow_iso()
        with self._writing() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS next FROM message WHERE session_id=?",
                (session_id,),
            ).fetchone()
            seq = int(row["next"])
            conn.execute(
                "INSERT INTO message (id, session_id, seq, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (message_id, session_id, seq, role.value, _dump(list(content)), created_at),
            )
        return Message(
            id=message_id,
            session_id=session_id,
            seq=seq,
            role=role,
            content=list(content),
            created_at=created_at,
        )

    def list_messages(self, session_id: str) -> list[Message]:
        """List a session's messages in sequence order — the conversation, in full."""
        return self._fetch_all(
            "SELECT * FROM message WHERE session_id=? ORDER BY seq", (session_id,), _message
        )

    # -- tool calls and approvals (T3.3) ---------------------------------------------------

    def start_tool_call(
        self,
        *,
        session_id: str,
        server: str,
        tool: str,
        arguments: Mapping[str, Any],
        message_id: str | None = None,
    ) -> ToolCall:
        """Record a tool call as it starts, with a null completion time."""
        call = ToolCall(
            id=ids.new_id(ids.TOOL_CALL),
            session_id=session_id,
            server=server,
            tool=tool,
            arguments=dict(arguments),
            started_at=ids.utcnow_iso(),
            message_id=message_id,
        )
        with self._writing() as conn:
            conn.execute(
                "INSERT INTO tool_call (id, session_id, message_id, server, tool, arguments, "
                "started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    call.id,
                    call.session_id,
                    call.message_id,
                    call.server,
                    call.tool,
                    _dump(dict(call.arguments)),
                    call.started_at,
                ),
            )
        return call

    def finish_tool_call(
        self, tool_call_id: str, *, result_summary: str | None, is_error: bool = False
    ) -> None:
        """Mark a tool call finished with a bounded result summary."""
        with self._writing() as conn:
            conn.execute(
                "UPDATE tool_call SET result_summary=?, is_error=?, finished_at=? WHERE id=?",
                (result_summary, 1 if is_error else 0, ids.utcnow_iso(), tool_call_id),
            )

    def get_tool_call(self, tool_call_id: str) -> ToolCall | None:
        """Fetch a tool call by id, or None."""
        return self._fetch_one(
            "SELECT * FROM tool_call WHERE id=?", (tool_call_id,), _tool_call
        )

    def list_tool_calls(self, session_id: str) -> list[ToolCall]:
        """List a session's tool calls in start order."""
        return self._fetch_all(
            "SELECT * FROM tool_call WHERE session_id=? ORDER BY started_at, rowid",
            (session_id,),
            _tool_call,
        )

    def sweep_interrupted_tool_calls(self) -> int:
        """Mark every in-flight tool call interrupted at startup; return how many.

        A call left with no completion time is one a dead process never finished, and must not
        be assumed to have succeeded ([store.md §11]).
        """
        with self._writing() as conn:
            cursor = conn.execute(
                "UPDATE tool_call SET finished_at=?, is_error=1, "
                "result_summary='interrupted (process restart)' WHERE finished_at IS NULL",
                (ids.utcnow_iso(),),
            )
            return cursor.rowcount

    def record_approval(
        self,
        *,
        tool_call_id: str,
        proposed_command: str,
        decision: ApprovalDecision,
        decided_by: str,
        danger_flags: Sequence[str] = (),
    ) -> Approval:
        """Record the decision on a proposed write; the UNIQUE key blocks double-approval."""
        approval = Approval(
            id=ids.new_id(ids.APPROVAL),
            tool_call_id=tool_call_id,
            proposed_command=proposed_command,
            decision=decision,
            decided_by=decided_by,
            decided_at=ids.utcnow_iso(),
            danger_flags=tuple(danger_flags),
        )
        with self._writing() as conn:
            conn.execute(
                "INSERT INTO approval (id, tool_call_id, proposed_command, danger_flags, "
                "decision, decided_by, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    approval.id,
                    approval.tool_call_id,
                    approval.proposed_command,
                    _dump(list(approval.danger_flags)) if approval.danger_flags else None,
                    approval.decision.value,
                    approval.decided_by,
                    approval.decided_at,
                ),
            )
        return approval

    def get_approval(self, tool_call_id: str) -> Approval | None:
        """Fetch the approval for a tool call, or None."""
        return self._fetch_one(
            "SELECT * FROM approval WHERE tool_call_id=?", (tool_call_id,), _approval
        )

    # -- artifacts, indexes, procedure results (T3.4) --------------------------------------

    def add_artifact(
        self,
        *,
        session_id: str,
        kind: ArtifactKind,
        blob_key: str,
        size_bytes: int,
        content_type: str,
        note: str | None = None,
    ) -> Artifact:
        """Record artifact metadata; the bytes are stored separately in the artifact store."""
        artifact = Artifact(
            id=ids.new_id(ids.ARTIFACT),
            session_id=session_id,
            kind=kind,
            blob_key=blob_key,
            size_bytes=size_bytes,
            content_type=content_type,
            captured_at=ids.utcnow_iso(),
            note=note,
        )
        with self._writing() as conn:
            conn.execute(
                "INSERT INTO artifact (id, session_id, kind, blob_key, size_bytes, "
                "content_type, captured_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact.id,
                    artifact.session_id,
                    artifact.kind.value,
                    artifact.blob_key,
                    artifact.size_bytes,
                    artifact.content_type,
                    artifact.captured_at,
                    artifact.note,
                ),
            )
        return artifact

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        """Fetch artifact metadata by id, or None."""
        return self._fetch_one("SELECT * FROM artifact WHERE id=?", (artifact_id,), _artifact)

    def list_artifacts(self, session_id: str) -> list[Artifact]:
        """List a session's artifacts in capture order."""
        return self._fetch_all(
            "SELECT * FROM artifact WHERE session_id=? ORDER BY captured_at, rowid",
            (session_id,),
            _artifact,
        )

    def sum_artifact_bytes_by_kind(self) -> dict[str, int]:
        """Return total artifact bytes grouped by kind, for storage accounting ([store.md §10])."""
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT kind, COALESCE(SUM(size_bytes), 0) AS bytes FROM artifact GROUP BY kind"
            ).fetchall()
        return {row["kind"]: int(row["bytes"]) for row in rows}

    def add_file_index(
        self, *, artifact_id: str, kind: FileIndexKind, blob_key: str, format_version: int
    ) -> FileIndex:
        """Record a built index over an artifact."""
        index = FileIndex(
            id=ids.new_id(ids.FILE_INDEX),
            artifact_id=artifact_id,
            kind=kind,
            blob_key=blob_key,
            format_version=format_version,
            built_at=ids.utcnow_iso(),
        )
        with self._writing() as conn:
            conn.execute(
                "INSERT INTO file_index (id, artifact_id, kind, blob_key, format_version, "
                "built_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    index.id,
                    index.artifact_id,
                    index.kind.value,
                    index.blob_key,
                    index.format_version,
                    index.built_at,
                ),
            )
        return index

    def get_file_index(self, artifact_id: str) -> FileIndex | None:
        """Fetch the index for an artifact, or None."""
        return self._fetch_one(
            "SELECT * FROM file_index WHERE artifact_id=?", (artifact_id,), _file_index
        )

    def delete_file_index(self, index_id: str) -> None:
        """Remove an index row so a stale-format index can be rebuilt ([store.md §4.10])."""
        with self._writing() as conn:
            conn.execute("DELETE FROM file_index WHERE id=?", (index_id,))

    def add_procedure_result(
        self,
        *,
        session_id: str,
        printer_id: str,
        procedure: Procedure,
        values: Mapping[str, Any],
        filament: str | None = None,
        evidence: Sequence[str] = (),
    ) -> ProcedureResult:
        """Record a calibration result; the database CHECK enforces its scope ([store.md §4.11])."""
        result = ProcedureResult(
            id=ids.new_id(ids.PROCEDURE_RESULT),
            session_id=session_id,
            printer_id=printer_id,
            procedure=procedure,
            values=dict(values),
            recorded_at=ids.utcnow_iso(),
            filament=filament,
            evidence=tuple(evidence),
        )
        with self._writing() as conn:
            conn.execute(
                "INSERT INTO procedure_result (id, session_id, printer_id, procedure, filament, "
                "values_json, evidence, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.id,
                    result.session_id,
                    result.printer_id,
                    result.procedure.value,
                    result.filament,
                    _dump(dict(result.values)),
                    _dump(list(result.evidence)) if result.evidence else None,
                    result.recorded_at,
                ),
            )
        return result

    def list_procedure_results(
        self,
        *,
        printer_id: str | None = None,
        procedure: Procedure | None = None,
        filament: str | None = None,
    ) -> list[ProcedureResult]:
        """List procedure results, optionally filtered by printer, procedure, and filament."""
        clauses: list[str] = []
        params: list[Any] = []
        if printer_id is not None:
            clauses.append("printer_id=?")
            params.append(printer_id)
        if procedure is not None:
            clauses.append("procedure=?")
            params.append(procedure.value)
        if filament is not None:
            clauses.append("filament=?")
            params.append(filament)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._fetch_all(
            f"SELECT * FROM procedure_result{where} ORDER BY recorded_at DESC, rowid DESC",
            tuple(params),
            _procedure_result,
        )

    # -- section cache (T3.4) --------------------------------------------------------------

    def get_section_cache(self, content_hash: str) -> SectionCache | None:
        """Fetch a cached extraction by section content hash, or None."""
        return self._fetch_one(
            "SELECT * FROM section_cache WHERE content_hash=?", (content_hash,), _section_cache
        )

    def put_section_cache(self, content_hash: str, result: Mapping[str, Any]) -> SectionCache:
        """Cache an extraction result under a section's content hash (insert or replace)."""
        entry = SectionCache(
            content_hash=content_hash, result=dict(result), cached_at=ids.utcnow_iso()
        )
        with self._writing() as conn:
            conn.execute(
                "INSERT INTO section_cache (content_hash, result, cached_at) VALUES (?, ?, ?) "
                "ON CONFLICT(content_hash) DO UPDATE SET result=excluded.result, "
                "cached_at=excluded.cached_at",
                (entry.content_hash, _dump(dict(entry.result)), entry.cached_at),
            )
        return entry

    # -- internals -------------------------------------------------------------------------

    def _writing(self):
        """Write context manager that converts constraint violations into store errors."""
        return _WriteGuard(self._db)

    def _fetch_one(self, sql: str, params: tuple[Any, ...], mapper):
        with self._db.read() as conn:
            row = conn.execute(sql, params).fetchone()
        return mapper(row) if row is not None else None

    def _fetch_all(self, sql: str, params: tuple[Any, ...], mapper) -> list:
        with self._db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [mapper(row) for row in rows]


class _WriteGuard:
    """Wraps ``Database.write`` and turns an IntegrityError into a ConstraintViolationError."""

    def __init__(self, database: Database) -> None:
        self._cm = database.write()

    def __enter__(self) -> sqlite3.Connection:
        return self._cm.__enter__()

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None and issubclass(exc_type, sqlite3.IntegrityError):
            # Let the context manager roll back, then re-raise as a store error.
            self._cm.__exit__(exc_type, exc, tb)
            raise ConstraintViolationError(str(exc)) from exc
        return self._cm.__exit__(exc_type, exc, tb)
