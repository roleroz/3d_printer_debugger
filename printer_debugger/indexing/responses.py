"""Shared MCP response discipline: ceilings, bounded markers, narrowing guidance.

Every tool bounds its own response; a request that would exceed its ceiling fails with a message
naming the limit and how to narrow it, never a silent truncation
([file_indexing.md §2.3, §5](../../docs/design/file_indexing.md)).
"""

from __future__ import annotations

import json
from typing import Any

# The largest JSON payload any single tool returns, in bytes. Chosen to keep a tool result well
# within a comfortable model-context budget; configurable at the composition root.
MAX_RESPONSE_BYTES = 32_000


class ToolError(Exception):
    """A tool request that cannot be answered within the response ceiling, with how to narrow it."""

    def __init__(self, message: str, narrow: str) -> None:
        self.narrow = narrow
        super().__init__(f"{message} — {narrow}")


def bounded(payload: dict[str, Any], *, complete: bool = True) -> dict[str, Any]:
    """Attach the ``bounded`` marker stating how much of the answer was returned, and enforce size.

    Raises :class:`ToolError` if the payload exceeds the response ceiling, rather than truncating.
    """
    result = dict(payload)
    result["bounded"] = {"complete": complete, "limit_bytes": MAX_RESPONSE_BYTES}
    size = len(json.dumps(result, default=str).encode("utf-8"))
    if size > MAX_RESPONSE_BYTES:
        raise ToolError(
            f"response is {size} bytes, over the {MAX_RESPONSE_BYTES}-byte ceiling",
            "request a narrower range, a specific key, or a single object",
        )
    return result
