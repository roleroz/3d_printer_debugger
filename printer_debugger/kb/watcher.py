"""Document watcher: hashing, interval polling, and the startup ingest ([kb_ingestion.md §3.1]).

Polls rather than watching filesystem events: the document may live on a mounted path where events
are unreliable, it changes rarely, and hashing it is trivial. The poll loop uses an injected sleep
so tests drive it without waiting.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Callable

from .ingester import KbIngester
from .models import IngestOutcome

DEFAULT_POLL_INTERVAL_SECONDS = 60.0


class DocumentMissingError(Exception):
    """The knowledge-base document is absent at startup — fatal, since nothing works without it."""


class DocumentWatcher:
    """Watches the document and re-ingests it when its content hash changes."""

    def __init__(
        self,
        path: str | Path,
        ingester: KbIngester,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._path = Path(path)
        self._ingester = ingester
        self._interval = poll_interval_seconds
        self._sleep = sleep
        self._last_hash: str | None = None
        self._running = False

    def start(self) -> IngestOutcome:
        """Perform the startup ingest. A missing document here is fatal ([kb_ingestion.md §7])."""
        if not self._path.exists():
            raise DocumentMissingError(f"knowledge-base document {self._path} is missing")
        text = self._path.read_text(encoding="utf-8")
        self._last_hash = _hash(text)
        return self._ingester.ingest(text)

    def poll(self) -> IngestOutcome | None:
        """Re-ingest if the document changed; return the outcome, or None if unchanged.

        A document that becomes unreadable mid-run keeps the last good ingest and reports, rather
        than crashing ([kb_ingestion.md §7]).
        """
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            return IngestOutcome(
                messages=(f"document unreadable ({exc}); keeping the last good ingest.",)
            )
        digest = _hash(text)
        if digest == self._last_hash:
            return None
        self._last_hash = digest
        return self._ingester.ingest(text)

    def run_forever(self, stop: Callable[[], bool] = lambda: False) -> None:  # pragma: no cover
        """Poll on the interval until ``stop`` returns true. The long-running entry point."""
        self._running = True
        while not stop():
            self._sleep(self._interval)
            self.poll()
        self._running = False


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
