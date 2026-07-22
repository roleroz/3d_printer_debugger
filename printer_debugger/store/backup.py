"""Backup and restore.

Backup uses SQLite's online backup for a consistent database copy while the system runs — ``cp``
is not equivalent and can capture a torn WAL state. The artifact tree is synced **after** the
database, so a restored pair can only contain an inert artifact-with-no-row, never a dangling
row-with-no-artifact ([store.md §9](../../docs/design/store.md)). Both halves must be restored
together; neither is meaningful alone.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from .db import Database
from .errors import StoreError

_DATABASE_NAME = "database.db"
_ARTIFACTS_NAME = "artifacts"


def backup(database: Database, artifact_root: str | Path, destination: str | Path) -> None:
    """Back up the database and artifacts into ``destination``, database first.

    Ordering is the guarantee: an artifact written after the database copy but during the artifact
    copy is inert; the reverse order could dangle a row ([store.md §9]).
    """
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        _backup_database(database.path, dest / _DATABASE_NAME)
        _sync_tree(Path(artifact_root), dest / _ARTIFACTS_NAME)
    except Exception as exc:  # noqa: BLE001
        raise StoreError(f"backup failed: {exc}") from exc


def restore(source: str | Path, database_path: str | Path, artifact_root: str | Path) -> None:
    """Restore both halves from a backup directory. The system must be stopped first.

    Startup migration then brings a restored older database up to the current schema
    ([store.md §9]). Both halves are replaced together.
    """
    src = Path(source)
    backup_db = src / _DATABASE_NAME
    backup_artifacts = src / _ARTIFACTS_NAME
    if not backup_db.exists():
        raise StoreError(f"backup at {src} has no {_DATABASE_NAME}")
    # Remove WAL/shm sidecars of the target so the restored file is authoritative.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{database_path}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    _copy_file(backup_db, Path(database_path))
    target_artifacts = Path(artifact_root)
    if target_artifacts.exists():
        shutil.rmtree(target_artifacts)
    if backup_artifacts.exists():
        _copytree(backup_artifacts, target_artifacts)
    else:
        target_artifacts.mkdir(parents=True, exist_ok=True)


# Failure-injection seams and the operations that must stay overridable for tests
# ([store.md §12]).
def _backup_database(source_path: str, dest_path: Path) -> None:
    """Copy the live database consistently via SQLite's online backup API."""
    source = sqlite3.connect(source_path)
    try:
        with sqlite3.connect(dest_path) as target:
            source.backup(target)
    finally:
        source.close()


def _sync_tree(source: Path, dest: Path) -> None:
    """Sync the artifact tree, tolerating a pre-existing destination."""
    if source.exists():
        _copytree(source, dest)
    else:
        dest.mkdir(parents=True, exist_ok=True)


def _copytree(source: Path, dest: Path) -> None:
    shutil.copytree(source, dest, dirs_exist_ok=True)


def _copy_file(source: Path, dest: Path) -> None:
    shutil.copyfile(source, dest)
