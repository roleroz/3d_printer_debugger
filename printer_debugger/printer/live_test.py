"""Live, read-only integration test against a real printer.

Tagged manual + requires-network: excluded from the default suite. Hits only read endpoints on the
configured printer and never writes, never stops it. Skips gracefully if the printer is offline.
Run with: ``bazel test //printer_debugger/printer:live_test --test_tag_filters=+requires-network``
(or ``--test_output=all`` after removing the default exclusion).
"""

from __future__ import annotations

import os
import unittest

from printer_debugger.printer.moonraker import MoonrakerClient
from printer_debugger.printer.tools import PrinterTools

_BASE_URL = os.environ.get("PD_LIVE_PRINTER", "http://voron2.eterovic.xyz:7125")


class LivePrinterReadTest(unittest.TestCase):
    """Read-only checks against the real printer; skipped if it is unreachable."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = MoonrakerClient(_BASE_URL)
        if not cls.client.is_reachable():
            raise unittest.SkipTest(f"printer at {_BASE_URL} is not reachable")

    def test_info_reads(self) -> None:
        """/printer/info returns identity/state fields."""
        info = self.client.info()
        self.assertIsInstance(info, dict)

    def test_status_tool_reads(self) -> None:
        """The get_status tool returns an available runtime value."""
        tools = PrinterTools(self.client)
        result = tools.get_status()
        self.assertTrue(result["available"])

    def test_temperatures_read(self) -> None:
        """The get_temperatures tool returns live values without writing anything."""
        tools = PrinterTools(self.client)
        result = tools.get_temperatures()
        self.assertTrue(result["available"])


if __name__ == "__main__":
    unittest.main()
