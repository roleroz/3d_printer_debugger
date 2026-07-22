"""Exceptions raised by the store.

Every failure the store surfaces is one of these, so callers never have to catch a raw
``sqlite3`` error or an ``OSError`` from the blob backend.
"""

from __future__ import annotations


class StoreError(Exception):
    """Base class for every error the store raises."""


class MigrationError(StoreError):
    """A schema migration could not be applied, or the migration set is inconsistent."""


class DatabaseNewerThanCodeError(StoreError):
    """The database schema version is newer than the code knows how to handle.

    Fatal: it means a rollback happened without the data being considered
    ([store.md §8](../../docs/design/store.md)).
    """

    def __init__(self, database_version: int, code_version: int) -> None:
        self.database_version = database_version
        self.code_version = code_version
        super().__init__(
            f"database schema version {database_version} is newer than the code's "
            f"maximum known version {code_version}; refusing to run"
        )


class ArtifactNotFoundError(StoreError):
    """A blob key was requested that the artifact store does not hold."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"no artifact stored under key {key!r}")


class ArtifactWriteError(StoreError):
    """Writing an artifact failed; any partial blob has been removed."""


class ConstraintViolationError(StoreError):
    """A write violated a schema constraint.

    A bug rather than a condition ([store.md §11](../../docs/design/store.md)): the offending
    statement is included so it is visible where it ran.
    """

    def __init__(self, message: str, statement: str | None = None) -> None:
        self.statement = statement
        super().__init__(message if statement is None else f"{message} (statement: {statement})")
