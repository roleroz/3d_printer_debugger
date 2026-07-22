"""Load and validate the procedure catalog, and cross-check scope against the schema.

Each document is validated at load: it parses, declares a valid scope, and names only procedure
identifiers the schema accepts; a procedure needing a printed test declares a ``test_source`` and a
macro-based one declares none ([procedures.md §3, §9]). Scope is declared here and enforced by the
database — declaring and enforcing in different places is deliberate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..store.models import PRINTER_SCOPED_PROCEDURES, Procedure

_DOCS_DIR = Path(__file__).resolve().parent / "documents"
_MACRO_ONLY = {"input_shaper", "pid_tune"}  # no printed test object.


class CatalogError(Exception):
    """A catalog document is malformed or inconsistent — a broken system prompt, hence fatal."""


@dataclass(frozen=True, slots=True)
class ProcedureDoc:
    """One procedure's document."""

    id: str
    name: str
    scope: str
    purpose: str
    preconditions: tuple[dict[str, Any], ...]
    hardware_requirements: tuple[str, ...]
    steps: tuple[dict[str, Any], ...]
    evidence: str
    interpretation: str
    results: tuple[str, ...]
    records: str
    test_source: dict[str, Any] | None = None


def load_catalog(directory: Path | None = None) -> dict[str, ProcedureDoc]:
    """Load and validate every procedure document, returning them keyed by id."""
    directory = directory or _DOCS_DIR
    catalog: dict[str, ProcedureDoc] = {}
    for path in sorted(directory.glob("*.yaml")):
        doc = _load_one(path)
        catalog[doc.id] = doc
    _validate_completeness(catalog)
    return catalog


def _load_one(path: Path) -> ProcedureDoc:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CatalogError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError(f"{path.name} is not a mapping")
    try:
        procedure_id = data["id"]
    except KeyError as exc:
        raise CatalogError(f"{path.name} has no id") from exc

    _validate_ids_and_scope(path.name, data)
    _validate_test_source(path.name, procedure_id, data)
    return ProcedureDoc(
        id=procedure_id,
        name=data["name"],
        scope=data["scope"],
        purpose=data.get("purpose", ""),
        preconditions=tuple(data.get("preconditions", []) or []),
        hardware_requirements=tuple(data.get("hardware_requirements", []) or []),
        steps=tuple(data.get("steps", []) or []),
        evidence=data.get("evidence", ""),
        interpretation=data.get("interpretation", ""),
        results=tuple(data.get("results", []) or []),
        records=data.get("records", ""),
        test_source=data.get("test_source"),
    )


def _validate_ids_and_scope(filename: str, data: dict) -> None:
    procedure_id = data["id"]
    try:
        Procedure(procedure_id)
    except ValueError as exc:
        raise CatalogError(f"{filename}: unknown procedure id {procedure_id!r}") from exc
    scope = data.get("scope")
    if scope not in ("printer", "printer_and_filament"):
        raise CatalogError(f"{filename}: invalid scope {scope!r}")
    # The declared scope must match what the schema's CHECK will accept for this procedure.
    is_printer_scoped = Procedure(procedure_id) in PRINTER_SCOPED_PROCEDURES
    if is_printer_scoped and scope != "printer":
        raise CatalogError(f"{filename}: {procedure_id} must be scope 'printer'")
    if not is_printer_scoped and scope != "printer_and_filament":
        raise CatalogError(f"{filename}: {procedure_id} must be scope 'printer_and_filament'")


def _validate_test_source(filename: str, procedure_id: str, data: dict) -> None:
    test_source = data.get("test_source")
    if procedure_id in _MACRO_ONLY:
        if test_source is not None:
            raise CatalogError(
            f"{filename}: {procedure_id} is macro-based and needs no test_source"
        )
        return
    if not test_source:
        raise CatalogError(f"{filename}: {procedure_id} needs a test_source")
    if test_source.get("kind") == "shipped_model":
        raise CatalogError(f"{filename}: models are referenced, never shipped in the repository")


def render_for_prompt(catalog: dict[str, ProcedureDoc]) -> str:
    """Render the whole catalog to text for the system prompt prefix ([procedures.md §2.1]).

    All six documents in full, so they cache across sessions; the cost is paid once.
    """
    blocks: list[str] = []
    for doc in catalog.values():
        lines = [f"### {doc.name} (id: {doc.id}, scope: {doc.scope})", doc.purpose.strip()]
        if doc.hardware_requirements:
            lines.append("Requires: " + "; ".join(doc.hardware_requirements))
        if doc.interpretation:
            lines.append("Interpretation: " + doc.interpretation.strip())
        blocks.append("\n".join(part for part in lines if part))
    return "\n\n".join(blocks)


def _validate_completeness(catalog: dict[str, ProcedureDoc]) -> None:
    expected = {p.value for p in Procedure}
    missing = expected - set(catalog)
    if missing:
        raise CatalogError(f"catalog is missing procedures: {sorted(missing)}")
