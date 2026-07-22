"""Tests for the Moonraker client, emergency stop, webcam, and tools, via a fake transport."""

from __future__ import annotations

import json
import unittest

from printer_debugger.printer import webcam
from printer_debugger.printer.emergency import EmergencyStopFailed, emergency_stop
from printer_debugger.printer.moonraker import MoonrakerClient, PrinterUnreachable
from printer_debugger.printer.tiers import Tier
from printer_debugger.printer.tools import PrinterTools


class FakeTransport:
    """A scripted transport: maps (method, url-substring) to (status, body)."""

    def __init__(self, routes: dict[tuple[str, str], tuple[int, bytes]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []
        self.unreachable = False

    def __call__(self, method: str, url: str, body: bytes | None) -> tuple[int, bytes]:
        self.calls.append((method, url))
        if self.unreachable:
            raise PrinterUnreachable("network down")
        for (m, fragment), response in self.routes.items():
            if m == method and fragment in url:
                return response
        return 404, b"{}"


def _result(payload: dict) -> bytes:
    return json.dumps({"result": payload}).encode()


class MoonrakerClientTest(unittest.TestCase):
    """The client reads over HTTP and treats unreachability as a normal state."""

    def test_reachable_and_unreachable(self) -> None:
        """is_reachable reflects whether /printer/info answers."""
        transport = FakeTransport({("GET", "/printer/info"): (200, _result({"state": "ready"}))})
        client = MoonrakerClient("http://p", transport)
        self.assertTrue(client.is_reachable())
        transport.unreachable = True
        self.assertFalse(client.is_reachable())

    def test_auth_required_is_unreachable(self) -> None:
        """A 401 surfaces as unreachable with the auth reason."""
        transport = FakeTransport({("GET", "/printer/info"): (401, b"{}")})
        client = MoonrakerClient("http://p", transport)
        self.assertFalse(client.is_reachable())

    def test_log_tail_is_bounded(self) -> None:
        """get_logs returns only the tail of the log, never the whole file."""
        big = b"x" * 100_000
        transport = FakeTransport({("GET", "/server/files/klippy.log"): (200, big)})
        client = MoonrakerClient("http://p", transport)
        self.assertLessEqual(len(client.get_logs(tail_bytes=1000)), 1000)


class EmergencyStopTest(unittest.TestCase):
    """Emergency stop uses the dedicated endpoint and fails loudly."""

    def test_uses_dedicated_endpoint(self) -> None:
        """The stop POSTs to /printer/emergency_stop, not a gcode script."""
        transport = FakeTransport({("POST", "/printer/emergency_stop"): (200, b"{}")})
        emergency_stop("http://p", transport)
        self.assertEqual(transport.calls[-1][0], "POST")
        self.assertIn("/printer/emergency_stop", transport.calls[-1][1])

    def test_unreachable_raises_loudly(self) -> None:
        """A failure to send raises EmergencyStopFailed rather than being swallowed."""
        transport = FakeTransport({})
        transport.unreachable = True
        with self.assertRaises(EmergencyStopFailed):
            emergency_stop("http://p", transport)


class WebcamTest(unittest.TestCase):
    """Webcam capture reports an absent camera rather than erroring per attempt."""

    def test_no_url_is_unavailable(self) -> None:
        """A printer with no configured webcam reports the capability absent."""
        with self.assertRaises(webcam.WebcamUnavailable):
            webcam.capture_still(None)

    def test_success_returns_bytes(self) -> None:
        """A successful capture returns the frame bytes."""
        transport = FakeTransport({("GET", "/snapshot"): (200, b"\xff\xd8jpeg")})
        self.assertEqual(webcam.capture_still("http://p/snapshot", transport), b"\xff\xd8jpeg")


class PrinterToolsTest(unittest.TestCase):
    """The tools answer runtime reads and refuse to substitute saved values when unreachable."""

    def _tools(self, transport: FakeTransport, gate=None) -> PrinterTools:
        return PrinterTools(MoonrakerClient("http://p", transport), config_text="", gate=gate)

    def test_runtime_read_when_reachable(self) -> None:
        """A runtime read returns the live value tagged as runtime tier."""
        transport = FakeTransport(
            {
                ("GET", "/printer/objects/query"): (
                    200, _result({"status": {"print_stats": {"state": "printing"}}})
                )
            }
        )
        result = self._tools(transport).get_status()
        self.assertTrue(result["available"])
        self.assertEqual(result["tier"], Tier.RUNTIME.value)

    def test_runtime_read_when_unreachable_has_no_fallback(self) -> None:
        """When unreachable, a runtime read returns unavailable-with-reason, not a saved value."""
        transport = FakeTransport({})
        transport.unreachable = True
        result = self._tools(transport).get_status()
        self.assertFalse(result["available"])
        self.assertIn("reason", result)

    def test_propose_command_refuses_unknown(self) -> None:
        """propose_command refuses an unknown command before the gate sees it."""
        result = self._tools(FakeTransport({})).propose_command("FROBNICATE")
        self.assertTrue(result["refused"])

    def test_propose_command_reaches_gate(self) -> None:
        """A recognised command is passed to the gate with its classification."""
        seen: list[str] = []

        def gate(command, classification):
            seen.append(command)
            return {"executed": True, "result": "ok"}

        result = self._tools(FakeTransport({}), gate=gate).propose_command("G28")
        self.assertEqual(seen, ["G28"])
        self.assertTrue(result["executed"])


if __name__ == "__main__":
    unittest.main()
