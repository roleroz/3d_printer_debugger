"""Detect disagreements between configuration sources ([kb_ingestion.md §5]).

Recorded at ingest, raised only when the value becomes relevant. Detection never edits anything —
the output is a record.
"""

from __future__ import annotations

from .klipper_config import KlipperConfig
from .models import Discrepancy, DiscrepancyKind


def detect_within(config: KlipperConfig) -> list[Discrepancy]:
    """Find the two single-config discrepancy kinds: saved-supersedes-file and commented-differs."""
    found: list[Discrepancy] = []

    # Kind 1: a SAVE_CONFIG value differs from the file value for the same section/key.
    for section, saved_values in config.saved_sections.items():
        file_values = config.file_sections.get(section, {})
        for key, saved in saved_values.items():
            if key in file_values and file_values[key] != saved:
                found.append(
                    Discrepancy(
                        kind=DiscrepancyKind.SAVED_SUPERSEDES_FILE,
                        section=section,
                        key=key,
                        left=file_values[key],
                        right=saved,
                    )
                )

    # Kind 2: a commented-out value differs from the effective value for the same logical section.
    effective = config.effective()
    for comment in config.comments:
        active = effective.get(comment.section, {}).get(comment.key)
        if active is not None and active != comment.value:
            found.append(
                Discrepancy(
                    kind=DiscrepancyKind.COMMENTED_DIFFERS_FROM_ACTIVE,
                    section=comment.section,
                    key=comment.key,
                    left=comment.value,
                    right=active,
                )
            )
    return found


def detect_between(files: KlipperConfig, live: KlipperConfig) -> list[Discrepancy]:
    """Find kind 3: local files differ from the live (running) configuration."""
    found: list[Discrepancy] = []
    file_effective = files.effective()
    live_effective = live.effective()
    for section, file_values in file_effective.items():
        live_values = live_effective.get(section, {})
        for key, value in file_values.items():
            if key in live_values and live_values[key] != value:
                found.append(
                    Discrepancy(
                        kind=DiscrepancyKind.FILES_DIFFER_FROM_LIVE,
                        section=section,
                        key=key,
                        left=value,
                        right=live_values[key],
                    )
                )
    return found
