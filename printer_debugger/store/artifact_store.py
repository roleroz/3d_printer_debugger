"""``ArtifactStore``: the blob half of the store, behind a substitutable interface.

Two implementations — a local filesystem store and object storage for the cloud — sit behind one
interface so the rest of the system never depends on a path or a bucket
([store.md §3.2](../../docs/design/store.md)). Streaming both ways is required: a G-code upload can
reach the size limit and must never be held in memory whole. There is no delete.
"""

from __future__ import annotations

import os
import shutil
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO

from .errors import ArtifactNotFoundError, ArtifactWriteError

_CHUNK = 1024 * 1024  # 1 MiB streaming chunk.


# -- key layout ([store.md §5]) ---------------------------------------------------------------
# Keys are derived from identifiers, never from user-supplied filenames, which removes path
# traversal and collision as a class of problem.


def artifact_key(session_id: str, artifact_id: str, extension: str = "") -> str:
    """Build the blob key for a session's artifact."""
    return f"sessions/{session_id}/{artifact_id}{extension}"


def index_key(artifact_id: str, format_version: int) -> str:
    """Build the blob key for a file index over an artifact."""
    return f"indexes/{artifact_id}/v{format_version}.idx"


class ArtifactStore(ABC):
    """The blob interface. Callers depend on this, never on a filesystem path or a bucket."""

    @abstractmethod
    def put(self, key: str, stream: BinaryIO) -> str:
        """Consume ``stream`` into ``key``, returning it. Atomic: a failed put stores nothing."""

    @abstractmethod
    def open(self, key: str) -> BinaryIO:
        """Return a readable binary stream for ``key``; raise ArtifactNotFoundError if absent."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Whether ``key`` is present."""

    @abstractmethod
    def size(self, key: str) -> int:
        """Size in bytes of ``key``; raise ArtifactNotFoundError if absent."""

    @abstractmethod
    def total_size(self) -> int:
        """Sum of the sizes of every stored blob, for storage accounting."""


class LocalFilesystemArtifactStore(ArtifactStore):
    """Artifacts stored as files under a root directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys are system-derived, but normalise defensively so a key can never escape the root.
        resolved = (self._root / key).resolve()
        root = self._root.resolve()
        if root != resolved and root not in resolved.parents:
            raise ArtifactWriteError(f"key {key!r} escapes the artifact root")
        return resolved

    def put(self, key: str, stream: BinaryIO) -> str:
        final = self._path(key)
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(f".{final.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "wb") as handle:
                _copy_stream(stream, handle)
            _replace(str(tmp), str(final))
        except Exception as exc:  # noqa: BLE001 — any failure removes the partial and raises
            _remove_quietly(tmp)
            raise ArtifactWriteError(f"failed to store artifact {key!r}: {exc}") from exc
        return key

    def open(self, key: str) -> BinaryIO:
        path = self._path(key)
        if not path.exists():
            raise ArtifactNotFoundError(key)
        return open(path, "rb")

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def size(self, key: str) -> int:
        path = self._path(key)
        if not path.exists():
            raise ArtifactNotFoundError(key)
        return path.stat().st_size

    def total_size(self) -> int:
        total = 0
        for path in self._root.rglob("*"):
            if path.is_file() and not path.name.endswith(".tmp"):
                total += path.stat().st_size
        return total


class ObjectStorageArtifactStore(ArtifactStore):
    """Artifacts stored in a GCS bucket, for the cloud deployment.

    The Google client is imported lazily so the base store carries no cloud dependency until this
    backend is actually used. GCS uploads are atomic — the object is not visible until the upload
    completes — so a failed put leaves no incomplete blob, and no temp-then-copy dance is needed
    ([store.md §13, open question 1](../../docs/design/store.md)). Exercised only against a real
    bucket; its test is tagged manual/requires-network.
    """

    def __init__(self, bucket_name: str, client: object | None = None) -> None:
        self._bucket_name = bucket_name
        self._client = client

    def _bucket(self):  # pragma: no cover - requires live GCS
        if self._client is None:
            from google.cloud import storage  # lazy import: cloud-only dependency

            self._client = storage.Client()
        return self._client.bucket(self._bucket_name)

    def put(self, key: str, stream: BinaryIO) -> str:  # pragma: no cover - requires live GCS
        blob = self._bucket().blob(key)
        try:
            blob.upload_from_file(stream)
        except Exception as exc:  # noqa: BLE001
            raise ArtifactWriteError(f"failed to store artifact {key!r}: {exc}") from exc
        return key

    def open(self, key: str) -> BinaryIO:  # pragma: no cover - requires live GCS
        blob = self._bucket().blob(key)
        if not blob.exists():
            raise ArtifactNotFoundError(key)
        return blob.open("rb")

    def exists(self, key: str) -> bool:  # pragma: no cover - requires live GCS
        return bool(self._bucket().blob(key).exists())

    def size(self, key: str) -> int:  # pragma: no cover - requires live GCS
        blob = self._bucket().blob(key)
        blob.reload()
        if blob.size is None:
            raise ArtifactNotFoundError(key)
        return int(blob.size)

    def total_size(self) -> int:  # pragma: no cover - requires live GCS
        return sum(int(blob.size or 0) for blob in self._client.list_blobs(self._bucket_name))


# Failure-injection seams: exposed as module-level indirections so a test can make a mid-stream
# write or a rename fail without touching the environment ([store.md §12]).
def _copy_stream(source: BinaryIO, dest: BinaryIO) -> None:
    """Copy ``source`` into ``dest`` in bounded chunks."""
    shutil.copyfileobj(source, dest, _CHUNK)


def _replace(src: str, dst: str) -> None:
    """Atomically move a completed temp file into its final place."""
    os.replace(src, dst)


def _remove_quietly(path: Path) -> None:
    """Remove a file, ignoring absence."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
