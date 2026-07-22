"""Storage accounting: how much storage the system is using.

Nothing is ever deleted, so the system must be able to state its footprint
([store.md §10](../../docs/design/store.md)). Reported as the database file size, the artifact
total, and a breakdown of artifact bytes by kind — the breakdown being the useful figure, since it
is what shows G-code files and their indexes dominating the growth.
"""

from __future__ import annotations

import os
from pathlib import Path

from .artifact_store import ArtifactStore
from .models import StorageAccounting
from .structured_store import StructuredStore


def compute_accounting(
    structured: StructuredStore, artifacts: ArtifactStore, database_path: str | Path
) -> StorageAccounting:
    """Gather the three storage figures into a report."""
    return StorageAccounting(
        database_bytes=_database_bytes(database_path),
        artifact_bytes=artifacts.total_size(),
        artifact_bytes_by_kind=structured.sum_artifact_bytes_by_kind(),
    )


def _database_bytes(database_path: str | Path) -> int:
    """Size of the database, including its WAL and shared-memory sidecars if present."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{database_path}{suffix}")
        if candidate.exists():
            total += os.path.getsize(candidate)
    return total
