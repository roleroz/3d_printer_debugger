"""Route tests via the FastAPI TestClient: sessions, uploads, CSRF, auth, approvals, estop."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

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
        self.assertIn(created["id"], [s["id"] for s in self.client.get("/").json()["sessions"]])
        self.client.post(f"/sessions/{created['id']}/messages", json={"text": "help"})
        view = self.client.get(f"/sessions/{created['id']}").json()
        self.assertEqual(view["messages"][0]["role"], "user")
        self.assertEqual(self.client.post(f"/sessions/{created['id']}/close").json(), {"ok": True})

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
