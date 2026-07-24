"""Route tests via the FastAPI TestClient: sessions, uploads, CSRF, auth, approvals, estop."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from printer_debugger.kb.models import IngestOutcome
from printer_debugger.store.db import Database
from printer_debugger.store.models import PrinterStatus
from printer_debugger.store.structured_store import StructuredStore
from printer_debugger.web.app import AppContext, create_app
from printer_debugger.web.security import AuthConfig, AuthMode


class AppTestBase(unittest.TestCase):
    """Shared store/app fixture."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.db = Database(Path(self._dir.name) / "t.db")
        self.db.migrate()
        self.store = StructuredStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def _client(self, context: AppContext) -> TestClient:
        return TestClient(create_app(context))


class LocalModeTest(AppTestBase):
    """In local mode the routes work without auth."""

    def setUp(self) -> None:
        super().setUp()
        self.stopped: list[str] = []
        self.context = AppContext(
            store=self.store,
            auth=AuthConfig(mode=AuthMode.LOCAL),
            resolve_approval=lambda tc, ok, who: tc == "tc_pending",
            emergency_stop=lambda printer_id: self.stopped.append(printer_id),
            max_upload_bytes=100,
        )
        self.client = self._client(self.context)

    def test_health_and_session_crud(self) -> None:
        """Health reports the mode; a session can be created, listed, viewed, and closed."""
        self.assertEqual(self.client.get("/healthz").json()["mode"], "local")
        created = self.client.post("/sessions", json={"name": "Warping"}).json()
        listed = self.client.get("/api/sessions").json()["sessions"]
        self.assertIn(created["id"], [s["id"] for s in listed])
        self.client.post(f"/sessions/{created['id']}/messages", json={"text": "help"})
        view = self.client.get(f"/api/sessions/{created['id']}").json()
        self.assertEqual(view["messages"][0]["role"], "user")
        self.assertEqual(self.client.post(f"/sessions/{created['id']}/close").json(), {"ok": True})

    def test_rename_session(self) -> None:
        """Renaming a session with a non-empty name updates the stored name."""
        created = self.client.post("/sessions", json={"name": "Old"}).json()
        response = self.client.post(f"/sessions/{created['id']}/rename", json={"name": "New name"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "New name")
        view = self.client.get(f"/api/sessions/{created['id']}").json()
        self.assertEqual(view["name"], "New name")

    def test_rename_rejects_blank_name(self) -> None:
        """A whitespace-only rename is rejected with 400 and leaves the name unchanged."""
        created = self.client.post("/sessions", json={"name": "Keep"}).json()
        response = self.client.post(f"/sessions/{created['id']}/rename", json={"name": "   "})
        self.assertEqual(response.status_code, 400)
        view = self.client.get(f"/api/sessions/{created['id']}").json()
        self.assertEqual(view["name"], "Keep")

    def test_upload_size_precheck(self) -> None:
        """An oversized upload is rejected by the declared Content-Length before reading."""
        response = self.client.post(
            "/sessions/s/files", content=b"x" * 500,
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 413)

    def test_approval_route(self) -> None:
        """An approval for a pending proposal resolves; an unknown one 409s."""
        ok = self.client.post("/approvals/tc_pending", json={"approve": True})
        self.assertEqual(ok.status_code, 200)
        missing = self.client.post("/approvals/tc_other", json={"approve": True})
        self.assertEqual(missing.status_code, 409)

    def test_estop_route_fires_directly(self) -> None:
        """The emergency stop route fires the stop, bypassing the agent and gate."""
        response = self.client.post("/printers/prn_1/estop")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stopped, ["prn_1"])

    def test_printers_listed(self) -> None:
        """Printers are listed with their status."""
        self.store.create_printer(
            name="Voron", kb_section="s", kb_content_hash="h", status=PrinterStatus.COMPLETE
        )
        printers = self.client.get("/printers").json()["printers"]
        self.assertEqual(printers[0]["name"], "Voron")


class PrinterImportTest(AppTestBase):
    """The printer-import route surfaces the ingest outcome, or 400/503 when it cannot run."""

    def _context(self, ingest_kb: object = None) -> AppContext:
        return AppContext(
            store=self.store,
            auth=AuthConfig(mode=AuthMode.LOCAL),
            ingest_kb=ingest_kb,  # type: ignore[arg-type]
        )

    def test_import_returns_outcome(self) -> None:
        """A successful import returns the upserted/degraded printers and the messages list."""
        outcome = IngestOutcome(
            printers_upserted=("Voron",),
            printers_degraded=("Voron",),
            messages=("Voron is degraded: missing config_path.",),
        )
        captured: list[str] = []

        def fake(text: str) -> IngestOutcome:
            captured.append(text)
            return outcome

        client = self._client(self._context(ingest_kb=fake))
        response = client.post(
            "/printers/import",
            content="# Voron\naddr: 1.2.3.4".encode("utf-8"),
            headers={"Content-Type": "text/markdown", "X-Filename": "printers.md"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["printers_upserted"], ["Voron"])
        self.assertEqual(data["printers_degraded"], ["Voron"])
        self.assertEqual(data["messages"], ["Voron is degraded: missing config_path."])
        self.assertEqual(captured, ["# Voron\naddr: 1.2.3.4"])

    def test_empty_body_is_rejected(self) -> None:
        """An empty uploaded document is rejected with 400 before the ingester runs."""
        called: list[str] = []

        def fake(text: str) -> IngestOutcome:
            called.append(text)
            return IngestOutcome()

        client = self._client(self._context(ingest_kb=fake))
        response = client.post(
            "/printers/import", content=b"", headers={"Content-Type": "text/markdown"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(called, [])

    def test_no_ingester_returns_503(self) -> None:
        """With no ingester wired the route reports 503 rather than silently doing nothing."""
        client = self._client(self._context(ingest_kb=None))
        response = client.post(
            "/printers/import", content=b"# Voron", headers={"Content-Type": "text/markdown"}
        )
        self.assertEqual(response.status_code, 503)


class UploadFailureReportingTest(AppTestBase):
    """File uploads log and report failures instead of dying silently."""

    _LOGGER = "printer_debugger.web.app"

    def test_failed_upload_logs_and_returns_500(self) -> None:
        """A raising index step is logged at ERROR and returns a 500 with a readable reason."""

        def boom(artifact: object, body: bytes) -> None:
            raise RuntimeError("index build blew up")

        context = AppContext(
            store=self.store, auth=AuthConfig(mode=AuthMode.LOCAL),
            on_upload=boom, max_upload_bytes=1000,
        )
        client = self._client(context)
        session = self.store.create_session(name="s")
        with self.assertLogs(self._LOGGER, level="ERROR") as logs:
            response = client.post(
                f"/sessions/{session.id}/files", content=b"\x89PNGdata",
                headers={"Content-Type": "image/png", "X-Filename": "p.jpg"},
            )
        self.assertEqual(response.status_code, 500)
        self.assertIn("error", response.json())
        self.assertTrue(any("upload failed" in line for line in logs.output))

    def test_too_large_upload_logs_warning(self) -> None:
        """An oversized declared upload returns 413 and logs a warning with context."""
        context = AppContext(
            store=self.store, auth=AuthConfig(mode=AuthMode.LOCAL), max_upload_bytes=10
        )
        client = self._client(context)
        with self.assertLogs(self._LOGGER, level="WARNING") as logs:
            response = client.post(
                "/sessions/s/files", content=b"x" * 50,
                headers={"Content-Type": "image/png", "X-Filename": "big.jpg"},
            )
        self.assertEqual(response.status_code, 413)
        self.assertTrue(any("too large" in line for line in logs.output))

    def test_normal_photo_upload_succeeds(self) -> None:
        """A normal photo upload with no failure still returns 200 with the artifact id."""
        context = AppContext(
            store=self.store, auth=AuthConfig(mode=AuthMode.LOCAL), max_upload_bytes=1000
        )
        client = self._client(context)
        session = self.store.create_session(name="s")
        response = client.post(
            f"/sessions/{session.id}/files", content=b"\x89PNGdata",
            headers={"Content-Type": "image/png", "X-Filename": "p.jpg"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("artifact_id", response.json())


class ExposedModeTest(AppTestBase):
    """Exposed mode enforces auth and the CSRF defense on mutating routes."""

    def setUp(self) -> None:
        super().setUp()
        self.context = AppContext(
            store=self.store,
            auth=AuthConfig(
                mode=AuthMode.EXPOSED,
                allowed_subjects=frozenset({"me@example.com"}),
                allowed_origin="https://app.example",
            ),
        )
        self.client = self._client(self.context)

    def test_unauthenticated_rejected(self) -> None:
        """A request with no allowlisted subject is rejected before any handler runs."""
        self.assertEqual(self.client.get("/healthz").status_code, 401)

    def test_authenticated_read_allowed(self) -> None:
        """An allowlisted subject may read."""
        response = self.client.get("/healthz", headers={"X-Auth-Subject": "me@example.com"})
        self.assertEqual(response.status_code, 200)

    def test_cross_site_post_refused(self) -> None:
        """A forged cross-site POST to the approval route is refused before the handler."""
        response = self.client.post(
            "/approvals/tc_1",
            json={"approve": True},
            headers={"X-Auth-Subject": "me@example.com", "Origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_same_origin_post_allowed(self) -> None:
        """A same-origin POST from the real app passes the CSRF check."""
        response = self.client.post(
            "/sessions",
            json={"name": "s"},
            headers={"X-Auth-Subject": "me@example.com", "Origin": "https://app.example"},
        )
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
