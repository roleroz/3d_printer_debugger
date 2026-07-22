"""Split the document into sections on the heading level that separates printers.

Mechanical, per [kb_ingestion.md §3.2](../../docs/design/kb_ingestion.md): the section level is
the most common heading level below the title, determined per document rather than hard-coded, so
a user who writes ``#`` per printer is handled as well as one who writes ``##`` under a title.
Content before the first section heading is preamble and joins the shared context.
"""

from __future__ import annotations

import re
from collections import Counter

from .models import Section, SectionedDocument

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def _heading_level(line: str) -> tuple[int, str] | None:
    """Return ``(level, text)`` if the line is an ATX heading, else None."""
    match = _HEADING.match(line)
    if match is None:
        return None
    return len(match.group(1)), match.group(2)


def section_level(lines: list[str]) -> int | None:
    """Determine the heading level that separates sections, or None if there are no headings."""
    headings = [h for line in lines if (h := _heading_level(line)) is not None]
    if not headings:
        return None
    levels = [level for level, _ in headings]
    smallest = min(levels)
    # A single top-of-document heading at the smallest level is the title; sections are the most
    # common level below it. Otherwise (e.g. many top-level headings), sections are the most
    # common level overall.
    if levels.count(smallest) == 1 and headings[0][0] == smallest:
        below = [level for level in levels if level > smallest]
        if below:
            return Counter(below).most_common(1)[0][0]
    return Counter(levels).most_common(1)[0][0]


def split_sections(document: str) -> SectionedDocument:
    """Split a document into its preamble and its sections at the section heading level."""
    lines = document.splitlines()
    level = section_level(lines)
    if level is None:
        return SectionedDocument(preamble=document, sections=())

    preamble_lines: list[str] = []
    sections: list[Section] = []
    current_heading: str | None = None
    current_lines: list[str] = []
    seen_section = False

    def flush() -> None:
        if current_heading is not None:
            sections.append(
                Section(
                    heading=current_heading,
                    text="\n".join(current_lines).rstrip() + "\n",
                    index=len(sections),
                )
            )

    for line in lines:
        parsed = _heading_level(line)
        if parsed is not None and parsed[0] == level:
            flush()
            seen_section = True
            current_heading = parsed[1]
            current_lines = [line]
        elif seen_section:
            current_lines.append(line)
        else:
            preamble_lines.append(line)
    flush()

    return SectionedDocument(
        preamble="\n".join(preamble_lines).strip(),
        sections=tuple(sections),
    )
