"""Tests for the off-the-event-loop async facades."""

from __future__ import annotations

import asyncio
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from printer_debugger.store.artifact_store import LocalFilesystemArtifactStore
from printer_debugger.store.async_store import (
    AsyncArtifactStore,
    AsyncStructuredStore,
    run_off_loop,
)
from printer_debugger.store.db import Database
from printer_debugger.store.structured_store import StructuredStore


class AsyncFacadeTest(unittest.TestCase):
    """The async facades delegate each call to a worker thread and return its result."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        base = Path(self._dir.name)
        self.db = Database(base / "test.db")
        self.db.migrate()
        self.store = AsyncStructuredStore(StructuredStore(self.db))
        self.artifacts = AsyncArtifactStore(LocalFilesystemArtifactStore(base / "a"))

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_structured_store_call_is_awaitable(self) -> None:
        """Creating and fetching a session works through the async facade."""

        async def scenario() -> None:
            session = await self.store.create_session(name="async session")
            fetched = await self.store.get_session(session.id)
            self.assertEqual(fetched.name, "async session")

        asyncio.run(scenario())

    def test_artifact_store_call_is_awaitable(self) -> None:
        """Putting and checking an artifact works through the async facade."""

        async def scenario() -> None:
            await self.artifacts.put("sessions/s/a", io.BytesIO(b"bytes"))
            self.assertTrue(await self.artifacts.exists("sessions/s/a"))
            self.assertEqual(await self.artifacts.size("sessions/s/a"), 5)

        asyncio.run(scenario())

    def test_run_off_loop_runs_callable(self) -> None:
        """run_off_loop executes a blocking callable and returns its value."""

        async def scenario() -> None:
            result = await run_off_loop(lambda a, b: a + b, 2, 3)
            self.assertEqual(result, 5)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
