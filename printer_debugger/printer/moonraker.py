"""Moonraker client over HTTP.

One-shot queries and command submission go over HTTP; the persistent-WebSocket subscription that
keeps live state current is a refinement (see implementation_notes). Reachability is a normal
state, reported through return values, never an exception into a session ([printer_access.md §3]).
The transport is injectable so tests run against a local socket server and never the real machine.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

# transport(method, url, body) -> (status_code, response_bytes). Injected in tests.
Transport = Callable[[str, str, bytes | None], "tuple[int, bytes]"]


class MoonrakerError(Exception):
    """A Moonraker request failed in a way that is not simple unreachability."""


class PrinterUnreachable(Exception):
    """The printer could not be reached — a normal state, surfaced through return values."""


def _urllib_transport(method: str, url: str, body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PrinterUnreachable(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class MoonrakerClient:
    """Read access to one printer's Moonraker API. Write paths live in emergency/tools."""

    base_url: str
    transport: Transport = _urllib_transport

    def _get(self, path: str) -> dict:
        status, body = self.transport("GET", self.base_url.rstrip("/") + path, None)
        if status == 200:
            return json.loads(body).get("result", {})
        if status in (401, 403):
            raise PrinterUnreachable(f"authentication required ({status})")
        raise MoonrakerError(f"GET {path} returned {status}")

    def is_reachable(self) -> bool:
        """Whether the printer answers, without raising if it does not."""
        try:
            self._get("/printer/info")
            return True
        except PrinterUnreachable:
            return False

    def info(self) -> dict:
        """Klipper/Moonraker identity and state from /printer/info."""
        return self._get("/printer/info")

    def query_objects(self, objects: dict[str, object] | None = None) -> dict:
        """Query printer objects (toolhead, heaters, print_stats) via /printer/objects/query."""
        if objects is None:
            objects = {"toolhead": None, "print_stats": None, "extruder": None, "heater_bed": None}
        query = "&".join(
            key if value is None else f"{key}={value}" for key, value in objects.items()
        )
        return self._get("/printer/objects/query?" + query)

    def get_config(self) -> dict:
        """The saved/running configuration from /printer/objects/query?configfile."""
        return self._get("/printer/objects/query?configfile")

    def get_logs(self, tail_bytes: int = 16_384) -> str:
        """A bounded tail of klippy.log; the rolling log is never returned whole."""
        status, body = self.transport(
            "GET", self.base_url.rstrip("/") + "/server/files/klippy.log", None
        )
        if status != 200:
            raise MoonrakerError(f"log fetch returned {status}")
        return body[-tail_bytes:].decode("utf-8", errors="replace")
