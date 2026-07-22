"""Import Klipper configuration from local files, following ``[include]`` directives.

Local files are read recursively; the live Moonraker source is imported through a provider seam so
this module carries no dependency on the printer-access module ([kb_ingestion.md §4]). Both are
stored as ``config_snapshot`` rows tagged with their source.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from ..store.models import ConfigSource
from ..store.structured_store import StructuredStore
from . import klipper_config
from .discrepancies import detect_between, detect_within

_INCLUDE = re.compile(r"^\[include\s+([^\]]+)\]\s*$")
_ENTRY_FILE = "printer.cfg"


class ConfigReadError(Exception):
    """A configuration file could not be read or parsed; names the file."""


class LiveConfigProvider(Protocol):
    """Fetches running configuration text over Moonraker (module 4 supplies the concrete one)."""

    def fetch_config(self, address: str) -> str: ...


def read_and_merge(config_path: str | Path) -> str:
    """Read a config tree from ``config_path``, expanding ``[include]`` directives in place."""
    path = Path(config_path)
    entry = path / _ENTRY_FILE if path.is_dir() else path
    if not entry.exists():
        raise ConfigReadError(f"configuration entry {entry} does not exist")
    return _expand(entry, set())


def _expand(path: Path, visited: set[Path]) -> str:
    resolved = path.resolve()
    if resolved in visited:
        return ""  # guard against include cycles
    visited.add(resolved)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigReadError(f"cannot read {path}: {exc}") from exc
    out: list[str] = []
    for line in text.splitlines():
        match = _INCLUDE.match(line.strip())
        if match is None:
            out.append(line)
            continue
        pattern = match.group(1).strip()
        targets = sorted(path.parent.glob(pattern))
        for target in targets:
            out.append(_expand(target, visited))
    return "\n".join(out)


def import_local_config(
    store: StructuredStore, printer_id: str, config_path: str | Path
) -> str:
    """Read, snapshot, and discrepancy-check the local configuration; return the merged text.

    On a read or parse error, no snapshot is written and the last one is kept ([kb_ingestion.md
    §7]); the error names the file.
    """
    merged = read_and_merge(config_path)
    parsed = klipper_config.parse(merged)
    discrepancies = [d.describe() for d in detect_within(parsed)]
    store.add_config_snapshot(
        printer_id=printer_id,
        source=ConfigSource.FILES,
        contents=merged,
        discrepancies=discrepancies,
    )
    return merged


def import_live_config(
    store: StructuredStore,
    printer_id: str,
    address: str,
    provider: LiveConfigProvider,
    local_text: str | None = None,
) -> str:
    """Import the running configuration over Moonraker and snapshot it.

    When the local text is available too, the files-differ-from-live discrepancy is computed
    against it ([kb_ingestion.md §5]).
    """
    live_text = provider.fetch_config(address)
    parsed_live = klipper_config.parse(live_text)
    discrepancies = [d.describe() for d in detect_within(parsed_live)]
    if local_text is not None:
        parsed_local = klipper_config.parse(local_text)
        discrepancies.extend(d.describe() for d in detect_between(parsed_local, parsed_live))
    store.add_config_snapshot(
        printer_id=printer_id,
        source=ConfigSource.MOONRAKER,
        contents=live_text,
        discrepancies=discrepancies,
    )
    return live_text
