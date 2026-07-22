"""The three tiers of printer state, enforced by type ([printer_access.md §2.2, §4]).

Configured, saved, and runtime values are never conflated: each is a distinct structure carrying
its source and read time, and there is no accessor that returns "the value" without saying which
kind it is. The consequence that matters: **runtime state has no stored fallback** — when the
printer is unreachable, a runtime query returns unavailable-with-reason, never a saved value.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class Tier(enum.Enum):
    """Which tier a value belongs to."""

    CONFIGURED = "configured"  # from config files, snapshotted; available offline.
    SAVED = "saved"  # from SAVE_CONFIG, snapshotted; available offline.
    RUNTIME = "runtime"  # live from the machine; no stored fallback.


@dataclass(frozen=True, slots=True)
class TieredValue(Generic[T]):
    """A value that always states which tier it came from and when it was read."""

    tier: Tier
    value: T
    read_at: str
    source: str  # "files", "moonraker", etc.


@dataclass(frozen=True, slots=True)
class Unavailable:
    """A runtime value that could not be read, with the reason — never a substituted saved value."""

    tier: Tier
    reason: str


RuntimeResult = TieredValue[Any] | Unavailable


def runtime(value: Any, read_at: str, source: str = "moonraker") -> TieredValue[Any]:
    """Build a runtime-tier value."""
    return TieredValue(tier=Tier.RUNTIME, value=value, read_at=read_at, source=source)


def unavailable_runtime(reason: str) -> Unavailable:
    """Build an unavailable runtime result — the only thing returned when unreachable."""
    return Unavailable(tier=Tier.RUNTIME, reason=reason)
