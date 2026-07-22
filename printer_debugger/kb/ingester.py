"""The ingestion flow: sectioning, extraction, upsert, and the orchestrator's view.

Upsert never replaces ([kb_ingestion.md §3.1]): a printer keeps its identity across edits, matched
by name; a removed section flags ``absent_since`` rather than deleting the row; a reappearing name
clears it. One bad section never fails the ingest of the others.
"""

from __future__ import annotations

from pathlib import Path

from ..store.models import PrinterStatus
from ..store.structured_store import StructuredStore
from . import config_import, extraction, sectioning
from .models import IngestOutcome, OrchestratorView, SectionExtraction


class KbIngester:
    """Ingests the knowledge-base document into printer and config records."""

    def __init__(self, store: StructuredStore, config_base: str = "/") -> None:
        self._store = store
        self._config_base = config_base
        self._shared_context = ""

    def ingest(self, document: str) -> IngestOutcome:
        """Ingest a document: extract each section, upsert printers, flag removals."""
        sectioned = sectioning.split_sections(document)

        upserted: list[str] = []
        degraded: list[str] = []
        unnamed: list[str] = []
        shared_headings: list[str] = []
        shared_texts: list[str] = [sectioned.preamble] if sectioned.preamble else []
        messages: list[str] = []
        seen_names: set[str] = set()

        for section in sectioned.sections:
            result = extraction.extract_with_cache(self._store, section.text)
            if not result.is_printer:
                shared_headings.append(section.heading)
                shared_texts.append(section.text)
                continue

            name = (result.name or "").strip()
            if not name:
                where = f"section {section.index + 1} ({section.heading!r})"
                unnamed.append(where)
                messages.append(f"{where} reads as a printer but has no name; please add one.")
                continue

            seen_names.add(name)
            self._upsert_printer(name, section.text, result, upserted, degraded, messages)

        self._flag_removed(seen_names, messages)
        self._shared_context = "\n\n".join(shared_texts).strip()

        absent = tuple(p.name for p in self._store.list_printers() if p.absent_since is not None)
        return IngestOutcome(
            printers_upserted=tuple(upserted),
            printers_degraded=tuple(degraded),
            printers_absent=absent,
            unnamed_sections=tuple(unnamed),
            shared_context_headings=tuple(shared_headings),
            messages=tuple(messages),
        )

    def _upsert_printer(
        self,
        name: str,
        section_text: str,
        result: SectionExtraction,
        upserted: list[str],
        degraded: list[str],
        messages: list[str],
    ) -> None:
        address = extraction.normalize_address(result.address)
        config_path = extraction.normalize_config_path(result.config_path, self._config_base)

        missing: list[str] = []
        if address is None:
            missing.append("address")
        if config_path is None:
            missing.append("config_path")
        status = PrinterStatus.DEGRADED if missing else PrinterStatus.COMPLETE
        content_hash = extraction.section_hash(section_text)

        existing = self._store.get_printer_by_name(name)
        if existing is None:
            printer = self._store.create_printer(
                name=name,
                kb_section=section_text,
                kb_content_hash=content_hash,
                status=status,
                address=address,
                config_path=config_path,
                missing=missing,
            )
            printer_id = printer.id
        else:
            self._store.update_printer(
                existing.id,
                kb_section=section_text,
                kb_content_hash=content_hash,
                status=status,
                address=address,
                config_path=config_path,
                missing=missing,
            )
            printer_id = existing.id

        upserted.append(name)
        if missing:
            degraded.append(name)
            messages.append(
                f"{name} is degraded: missing {', '.join(missing)}. "
                + _degraded_consequence(missing)
            )

        if config_path is not None:
            self._import_local(printer_id, name, config_path, messages)

    def _import_local(
        self, printer_id: str, name: str, config_path: str, messages: list[str]
    ) -> None:
        if not Path(config_path).exists():
            messages.append(
                f"{name}: config path {config_path} not found; "
                "live config still works if reachable."
            )
            return
        try:
            config_import.import_local_config(self._store, printer_id, config_path)
        except config_import.ConfigReadError as exc:
            messages.append(f"{name}: configuration not snapshotted ({exc}); last snapshot kept.")

    def _flag_removed(self, seen_names: set[str], messages: list[str]) -> None:
        for printer in self._store.list_printers():
            if printer.name not in seen_names and printer.absent_since is None:
                self._store.mark_printer_absent(printer.id)
                messages.append(
                    f"{printer.name} was removed from the document; "
                    "kept but marked no longer present."
                )

    def shared_context(self) -> str:
        """The retained shared-context text from the last ingest."""
        return self._shared_context

    def assemble_view(self, printer_id: str) -> OrchestratorView:
        """Assemble what the orchestrator receives for a bound printer ([kb_ingestion.md §6])."""
        printer = self._store.get_printer(printer_id)
        if printer is None:
            raise KeyError(f"no printer {printer_id!r}")
        snapshot = self._store.latest_config_snapshot(printer_id)
        return OrchestratorView(
            printer_id=printer_id,
            section_text=printer.kb_section,
            shared_context=self._shared_context,
            snapshot_contents=snapshot.contents if snapshot else None,
            snapshot_source=snapshot.source.value if snapshot else None,
            snapshot_captured_at=snapshot.captured_at if snapshot else None,
            discrepancies=snapshot.discrepancies if snapshot else (),
        )


def _degraded_consequence(missing: list[str]) -> str:
    """Describe what a missing value disables ([kb_ingestion.md §3.4])."""
    parts: list[str] = []
    if "address" in missing:
        parts.append("no live features at all (state, logs, live config, webcam)")
    if "config_path" in missing:
        parts.append("no offline snapshot; live config still works when reachable")
    return "; ".join(parts) + "."
