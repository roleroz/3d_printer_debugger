"""Parser for Klipper's INI-like configuration.

Retains three things discrepancy detection depends on ([kb_ingestion.md §4]): the active values,
the ``SAVE_CONFIG`` block Klipper appends (which supersedes what came before), and commented-out
``key: value`` lines kept separately rather than discarded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_SECTION = re.compile(r"^\[([^\]]+)\]\s*$")
_KEY_VALUE = re.compile(r"^([A-Za-z_][\w.]*)\s*[:=]\s*(\S.*?)\s*$")
_SAVE_CONFIG_MARKER = "SAVE_CONFIG"


@dataclass(frozen=True, slots=True)
class CommentedValue:
    """A commented-out ``key: value`` and the section it appeared under."""

    section: str
    key: str
    value: str


@dataclass(slots=True)
class KlipperConfig:
    """Parsed Klipper configuration, keeping active, saved, and commented values distinct."""

    file_sections: dict[str, dict[str, str]] = field(default_factory=dict)
    saved_sections: dict[str, dict[str, str]] = field(default_factory=dict)
    comments: list[CommentedValue] = field(default_factory=list)

    def effective(self) -> dict[str, dict[str, str]]:
        """Merge file values with the SAVE_CONFIG block; saved values win ([kb_ingestion.md §4])."""
        merged: dict[str, dict[str, str]] = {
            section: dict(values) for section, values in self.file_sections.items()
        }
        for section, values in self.saved_sections.items():
            merged.setdefault(section, {}).update(values)
        return merged


def parse(text: str) -> KlipperConfig:
    """Parse Klipper configuration text into its active, saved, and commented values."""
    config = KlipperConfig()
    current_section: str | None = None
    saved_section: str | None = None
    in_save_config = False

    for raw_line in text.splitlines():
        if _SAVE_CONFIG_MARKER in raw_line and raw_line.lstrip().startswith("#*#"):
            in_save_config = True
            continue

        if in_save_config:
            if not raw_line.lstrip().startswith("#*#"):
                continue
            body = raw_line.lstrip()[3:].strip()
            if not body:
                continue
            section_match = _SECTION.match(body)
            if section_match is not None:
                saved_section = section_match.group(1).strip()
                config.saved_sections.setdefault(saved_section, {})
                continue
            kv = _KEY_VALUE.match(body)
            if kv is not None and saved_section is not None:
                config.saved_sections.setdefault(saved_section, {})[kv.group(1)] = kv.group(2)
            continue

        stripped = raw_line.strip()
        if not stripped:
            continue

        section_match = _SECTION.match(stripped)
        if section_match is not None:
            header = section_match.group(1).strip()
            if header.split()[0].lower() == "include":
                continue  # include directives are expanded by the importer, not a config section
            current_section = header
            config.file_sections.setdefault(current_section, {})
            continue

        if stripped.startswith("#"):
            body = stripped.lstrip("#").strip()
            kv = _KEY_VALUE.match(body)
            if kv is not None and current_section is not None:
                config.comments.append(
                    CommentedValue(current_section, kv.group(1), kv.group(2))
                )
            continue

        kv = _KEY_VALUE.match(stripped)
        if kv is not None and current_section is not None:
            config.file_sections.setdefault(current_section, {})[kv.group(1)] = kv.group(2)

    return config


def find_includes(text: str) -> list[str]:
    """Return the targets of ``[include ...]`` directives, in order, for the importer to follow."""
    includes: list[str] = []
    for raw_line in text.splitlines():
        match = _SECTION.match(raw_line.strip())
        if match is None:
            continue
        parts = match.group(1).split(maxsplit=1)
        if len(parts) == 2 and parts[0].lower() == "include":
            includes.append(parts[1].strip())
    return includes
