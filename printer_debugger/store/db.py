"""Connection handling, the single-writer discipline, and the migration runner.

One SQLite file. A single write connection serialised behind a lock, and short-lived read
connections that WAL lets run concurrently ([store.md §6, §7](../../docs/design/store.md)). The
driver is blocking; [async_store][printer_debugger.store.async_store] is the off-the-event-loop
wrapper callers use from the event loop.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import DatabaseNewerThanCodeError, MigrationError

# The highest migration version the shipped code contains. A database numbered above this was
# written by newer code and must not be run against ([store.md §8]).
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d+)_.*\.sql$")

DEFAULT_BUSY_TIMEOUT_MS = 5000


def _discover_migrations() -> list[tuple[int, Path]]:
    """Return ``(version, path)`` for every migration file, ascending by version."""
    found: list[tuple[int, Path]] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if match is None:
            raise MigrationError(f"migration file {path.name!r} does not match NNN_description.sql")
        found.append((int(match.group(1)), path))
    versions = [v for v, _ in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration version among {versions}")
    if versions != sorted(versions):
        raise MigrationError("migration versions are not contiguous/ordered")
    return found


def code_schema_version() -> int:
    """The highest migration version the code ships, or 0 if there are none."""
    migrations = _discover_migrations()
    return migrations[-1][0] if migrations else 0


class Database:
    """Owns the SQLite file: its connections, pragmas, write lock, and migrations."""

    def __init__(self, path: str | Path, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> None:
        self._path = str(path)
        self._busy_timeout_ms = busy_timeout_ms
        self._write_lock = threading.Lock()
        self._write_conn = self._connect(is_first=True)

    @property
    def path(self) -> str:
        """The database file path."""
        return self._path

    # -- connection setup ------------------------------------------------------------------

    def _connect(self, is_first: bool = False) -> sqlite3.Connection:
        """Open a connection and apply the pragmas from store.md §6, in order."""
        conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        if is_first:
            # WAL is set once, before other connections open, and persists in the file.
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    # -- read/write access -----------------------------------------------------------------

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Yield the single write connection inside a transaction, serialised behind the lock.

        Commits on clean exit, rolls back on any exception. The transaction spans only the
        database work — never a model call, printer request, or approval wait ([store.md §7]).
        """
        with self._write_lock:
            self._write_conn.execute("BEGIN")
            try:
                yield self._write_conn
            except BaseException:
                self._write_conn.execute("ROLLBACK")
                raise
            else:
                self._write_conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Yield a short-lived read connection; WAL lets it run without blocking the writer."""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        """Close the write connection. Read connections are short-lived and close themselves."""
        with self._write_lock:
            self._write_conn.close()

    # -- migrations (T8.1) -----------------------------------------------------------------

    def current_version(self) -> int:
        """Return the highest applied migration version, or 0 on a fresh database."""
        with self.read() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if row is None:
                return 0
            version = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
            return int(version) if version is not None else 0

    def migrate(self) -> int:
        """Apply pending migrations in order; return the resulting version.

        Each migration and its ``schema_version`` row commit in one transaction, so a partial
        application is not a state that exists. A database newer than the code is fatal.
        """
        migrations = _discover_migrations()
        code_version = migrations[-1][0] if migrations else 0
        current = self.current_version()
        if current > code_version:
            raise DatabaseNewerThanCodeError(current, code_version)
        for version, path in migrations:
            if version <= current:
                continue
            statements = _split_statements(_read_migration(path))
            # One transaction per migration. executescript is avoided deliberately: it forces an
            # implicit COMMIT before running, which would defeat the all-or-nothing guarantee.
            with self._write_lock:
                self._write_conn.execute("BEGIN")
                try:
                    for statement in statements:
                        self._write_conn.execute(statement)
                    self._write_conn.execute(
                        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                        (version, _now()),
                    )
                except Exception as exc:  # noqa: BLE001 — any failure rolls back and is fatal
                    self._write_conn.execute("ROLLBACK")
                    raise MigrationError(f"migration {path.name} failed: {exc}") from exc
                else:
                    self._write_conn.execute("COMMIT")
        return code_version


def _split_statements(sql: str) -> list[str]:
    """Split a migration script into individual statements.

    Handles ``--`` line comments and single-quoted string literals so a semicolon inside a
    literal is not a false boundary. Migration files must not embed a ``;`` other than as a
    statement terminator (no triggers with compound bodies in this schema).
    """
    statements: list[str] = []
    buf: list[str] = []
    in_string = False
    i = 0
    length = len(sql)
    while i < length:
        ch = sql[i]
        if in_string:
            buf.append(ch)
            if ch == "'":
                # A doubled '' is an escaped quote inside the literal, not its end.
                if i + 1 < length and sql[i + 1] == "'":
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "-" and i + 1 < length and sql[i + 1] == "-":
            # Line comment: skip to end of line.
            while i < length and sql[i] != "\n":
                i += 1
            continue
        if ch == "'":
            in_string = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


# Exposed as a module-level indirection so tests can inject a read failure without touching the
# environment ([store.md §12]).
def _read_migration(path: Path) -> str:
    """Read a migration file's SQL text."""
    return path.read_text(encoding="utf-8")


def _now() -> str:
    from .ids import utcnow_iso

    return utcnow_iso()
