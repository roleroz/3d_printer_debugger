"""Identifier and timestamp helpers.

Primary keys are a short type prefix plus a UUID; timestamps are ISO-8601 UTC with a ``Z``
suffix, which sorts lexicographically ([store.md §4](../../docs/design/store.md)).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Final

# Type prefixes. One per entity that carries a generated id; the prefix makes an identifier in a
# log line self-describing.
SESSION: Final = "ses"
PRINTER: Final = "prn"
CONFIG_SNAPSHOT: Final = "cfg"
BINDING: Final = "bnd"
MESSAGE: Final = "msg"
TOOL_CALL: Final = "tc"
APPROVAL: Final = "apr"
ARTIFACT: Final = "art"
FILE_INDEX: Final = "idx"
PROCEDURE_RESULT: Final = "prc"


def new_id(prefix: str) -> str:
    """Return a fresh identifier of the form ``<prefix>_<hex-uuid>``."""
    return f"{prefix}_{uuid.uuid4().hex}"


def utcnow_iso() -> str:
    """Return the current UTC time as ISO-8601 with microsecond precision and a ``Z`` suffix.

    Microsecond precision keeps back-to-back writes distinctly ordered — two DB writes are always
    more than a microsecond apart — so "latest" and "most recent" queries are deterministic.
    """
    return to_iso(datetime.now(timezone.utc))


def to_iso(moment: datetime) -> str:
    """Render a ``datetime`` as ISO-8601 UTC with microsecond precision and a ``Z`` suffix."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
