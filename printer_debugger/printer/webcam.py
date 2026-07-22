"""Webcam still capture from crowsnest's snapshot endpoint ([printer_access.md §7]).

Every capture is stored as an artifact by the caller. A printer with no camera reports the
capability as absent rather than erroring on each attempt.
"""

from __future__ import annotations

from .moonraker import PrinterUnreachable, Transport, _urllib_transport


class WebcamUnavailable(Exception):
    """No usable webcam — the capability is reported absent rather than raised per attempt."""


def capture_still(snapshot_url: str | None, transport: Transport = _urllib_transport) -> bytes:
    """Return a webcam frame's bytes, or raise WebcamUnavailable if there is no camera."""
    if not snapshot_url:
        raise WebcamUnavailable("this printer has no configured webcam")
    try:
        status, body = transport("GET", snapshot_url, None)
    except PrinterUnreachable as exc:
        raise WebcamUnavailable(f"webcam unreachable: {exc}") from exc
    if status != 200 or not body:
        raise WebcamUnavailable(f"webcam snapshot returned {status}")
    return body
