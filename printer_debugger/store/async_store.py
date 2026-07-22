"""The off-the-event-loop execution wrapper.

The SQLite driver is blocking; running its calls on the event loop would stall SSE streams and
agent turns ([store.md §7](../../docs/design/store.md)). These thin facades run every store call in
a worker thread via :func:`asyncio.to_thread`, so async callers never block the loop. The single
write connection stays serialised behind its lock, so concurrent writes from different worker
threads remain one-at-a-time.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from .artifact_store import ArtifactStore
from .structured_store import StructuredStore


class _ThreadedFacade:
    """Wraps an object so each method call runs in a worker thread; attributes pass through."""

    def __init__(self, wrapped: object) -> None:
        self.__dict__["_wrapped"] = wrapped

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._wrapped, name)
        if not callable(attr):
            return attr

        async def run_in_thread(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(attr, *args, **kwargs)

        return run_in_thread


class AsyncStructuredStore(_ThreadedFacade):
    """Async facade over :class:`StructuredStore`; every method becomes a coroutine."""

    def __init__(self, structured: StructuredStore) -> None:
        super().__init__(structured)


class AsyncArtifactStore(_ThreadedFacade):
    """Async facade over :class:`ArtifactStore`; every method becomes a coroutine.

    ``open`` returns the underlying stream object; read it with ``asyncio.to_thread`` on the
    caller's side so large reads also stay off the loop.
    """

    def __init__(self, artifacts: ArtifactStore) -> None:
        super().__init__(artifacts)


async def run_off_loop(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking store callable in a worker thread. The general escape hatch."""
    return await asyncio.to_thread(fn, *args, **kwargs)
