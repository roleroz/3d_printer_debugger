"""Server-Sent-Events fan-out with position-based reconnect ([web.md §2.3, §10]).

A session may be open on several devices; all see the same live stream. A reconnecting client asks
for everything after the last event it received, served from a per-session buffer, so a phone that
backgrounds for thirty seconds loses no output.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Event:
    """A published event with its monotonic per-session id."""

    id: int
    kind: str
    data: str


@dataclass
class _Session:
    buffer: list[Event] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    next_id: int = 1


class SseHub:
    """Per-session fan-out with a bounded replay buffer."""

    def __init__(self, buffer_size: int = 1000) -> None:
        self._sessions: dict[str, _Session] = {}
        self._buffer_size = buffer_size

    def publish(self, session_id: str, kind: str, data: str) -> Event:
        """Publish an event to every subscriber of a session and retain it for reconnect."""
        session = self._sessions.setdefault(session_id, _Session())
        event = Event(id=session.next_id, kind=kind, data=data)
        session.next_id += 1
        session.buffer.append(event)
        if len(session.buffer) > self._buffer_size:
            session.buffer = session.buffer[-self._buffer_size :]
        for queue in list(session.subscribers):
            queue.put_nowait(event)
        return event

    def missed_since(self, session_id: str, last_id: int) -> list[Event]:
        """Return buffered events after ``last_id`` for a reconnecting client."""
        session = self._sessions.get(session_id)
        if session is None:
            return []
        return [event for event in session.buffer if event.id > last_id]

    def subscribe(self, session_id: str) -> asyncio.Queue:
        """Register a live subscriber, returning its queue."""
        session = self._sessions.setdefault(session_id, _Session())
        queue: asyncio.Queue = asyncio.Queue()
        session.subscribers.add(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        """Remove a subscriber; other subscribers are unaffected."""
        session = self._sessions.get(session_id)
        if session is not None:
            session.subscribers.discard(queue)

    def subscriber_count(self, session_id: str) -> int:
        """How many live subscribers a session has."""
        session = self._sessions.get(session_id)
        return len(session.subscribers) if session else 0
