"""UI-shell tests: server-rendered HTML, static assets, artifact serving, and refusal properties.

Exercised through the FastAPI ``TestClient`` over the real routes, so the auth and CSRF middleware
apply exactly as in production ([web.md §11]).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from printer_debugger.store.artifact_store import LocalFilesystemArtifactStore
from printer_debugger.store.db import Database
from printer_debugger.store.models import ArtifactKind, MessageRole, PrinterStatus
from printer_debugger.store.structured_store import StructuredStore
from printer_debugger.web.app import AppContext, create_app
from printer_debugger.web.security import AuthConfig, AuthMode


class UiTestBase(unittest.TestCase):
    """A local-mode app with a filesystem artifact store and a bound printer."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        root = Path(self._dir.name)
        self.db = Database(root / "t.db")
        self.db.migrate()
        self.store = StructuredStore(self.db)
        self.artifacts = LocalFilesystemArtifactStore(root / "artifacts")
        self.printer = self.store.create_printer(
            name="Voron", kb_section="s", kb_content_hash="h", status=PrinterStatus.COMPLETE
        )
        self.session = self.store.create_session(name="Warping", printer_id=self.printer.id)
        self.context = AppContext(
            store=self.store,
            auth=AuthConfig(mode=AuthMode.LOCAL),
            artifacts=self.artifacts,
            resolve_approval=lambda tc, ok, who: tc == "tc_pending",
            emergency_stop=lambda pid: None,
            max_upload_bytes=100,
        )
        self.client = TestClient(create_app(self.context))

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()


class SessionListPageTest(UiTestBase):
    """The session list renders as HTML with its key affordances."""

    def test_list_is_html_with_key_elements(self) -> None:
        """GET / returns HTML naming the session, the mode badge, and a new-session control."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        body = response.text
        self.assertIn('id="new-session"', body)
        self.assertIn("Warping", body)
        self.assertIn("mode-badge", body)
        self.assertIn(f'href="/sessions/{self.session.id}"', body)
        self.assertIn('id="printer-import-input"', body)  # Import-printers affordance.

    def test_json_api_still_available(self) -> None:
        """The JSON session list stays available under /api/sessions for scripted callers."""
        listed = self.client.get("/api/sessions").json()["sessions"]
        self.assertIn(self.session.id, [s["id"] for s in listed])


class SessionViewPageTest(UiTestBase):
    """The session view renders the working surface with all its regions."""

    def test_view_has_composer_conversation_and_strip(self) -> None:
        """GET /sessions/{id} renders the composer, conversation, printer strip, and estop."""
        self.store.add_message(
            self.session.id, MessageRole.USER, [{"type": "text", "text": "the corners lift"}]
        )
        response = self.client.get(f"/sessions/{self.session.id}")
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn('id="composer"', body)
        self.assertIn('id="conversation"', body)
        self.assertIn("printer-strip", body)
        self.assertIn('capture="environment"', body)  # Camera opens directly (web.md §4.2).
        self.assertIn("the corners lift", body)
        self.assertIn('id="estop"', body)  # Estop present while a printer is connected.
        self.assertIn('id="rename-btn"', body)  # Rename affordance next to the title.

    def test_estop_absent_without_printer(self) -> None:
        """A session with no printer bound renders no emergency-stop control."""
        loose = self.store.create_session(name="No printer")
        body = self.client.get(f"/sessions/{loose.id}").text
        self.assertNotIn('id="estop"', body)

    def test_missing_session_is_404_html(self) -> None:
        """A view of an unknown session returns a 404 HTML page, not a stack trace."""
        response = self.client.get("/sessions/ses_missing")
        self.assertEqual(response.status_code, 404)


class ApprovalRefusalPropertiesTest(UiTestBase):
    """The approval interface bakes in its refusal properties ([web.md §5])."""

    def test_approve_button_not_autofocus_and_reject_first(self) -> None:
        """Approve carries no autofocus and Reject precedes it in DOM/tab order."""
        body = self.client.get(f"/sessions/{self.session.id}").text
        self.assertIn('id="approval-template"', body)
        approve = body.index('data-role="approve"')
        reject = body.index('data-role="reject"')
        self.assertLess(reject, approve)  # Reject is reachable first; Approve is never the default.
        approve_button = body[body.rindex("<button", 0, approve):approve]
        self.assertNotIn("autofocus", approve_button)

    def test_client_js_guards_enter_from_approving(self) -> None:
        """The served app.js swallows Enter/Space so a stray keypress cannot approve."""
        js = self.client.get("/static/app.js").text
        self.assertIn("keydown", js)
        self.assertIn("preventDefault", js)
        self.assertIn('"Enter"', js)


class StaticAssetTest(UiTestBase):
    """Static assets are served with the correct content types."""

    def test_js_and_css_served(self) -> None:
        """app.js and styles.css are served with JavaScript and CSS content types."""
        js = self.client.get("/static/app.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("javascript", js.headers["content-type"])
        css = self.client.get("/static/styles.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn("css", css.headers["content-type"])

    def test_unknown_static_is_404(self) -> None:
        """An unlisted static path returns 404 rather than reading an arbitrary file."""
        self.assertEqual(self.client.get("/static/secret.py").status_code, 404)


class ArtifactServingTest(UiTestBase):
    """Artifacts are served as their stored bytes with the stored content type."""

    def test_uploaded_photo_is_served(self) -> None:
        """An uploaded image round-trips: GET /artifacts/{id} returns its bytes and content type."""
        upload = self.client.post(
            f"/sessions/{self.session.id}/files",
            content=b"\x89PNGdata",
            headers={"Content-Type": "image/png"},
        )
        artifact_id = upload.json()["artifact_id"]
        served = self.client.get(f"/artifacts/{artifact_id}")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.content, b"\x89PNGdata")
        self.assertIn("image/png", served.headers["content-type"])

    def test_missing_artifact_is_404(self) -> None:
        """A request for an unknown artifact id returns 404."""
        self.assertEqual(self.client.get("/artifacts/art_missing").status_code, 404)

    def test_artifact_row_without_blob_is_404(self) -> None:
        """An artifact row whose blob is absent from the store returns 404, not a server error."""
        orphan = self.store.add_artifact(
            session_id=self.session.id, kind=ArtifactKind.PHOTO,
            blob_key="sessions/x/never-written", size_bytes=3, content_type="image/png",
        )
        self.assertEqual(self.client.get(f"/artifacts/{orphan.id}").status_code, 404)


class UploadAndAudioTest(UiTestBase):
    """Uploads size-pre-check, and audio is captured with transcription marked pending."""

    def test_upload_size_precheck_before_body(self) -> None:
        """An oversized declared Content-Length is rejected with 413 before the body is read."""
        response = self.client.post(
            f"/sessions/{self.session.id}/files",
            content=b"x" * 500,
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 413)

    def test_audio_upload_marks_transcription_pending(self) -> None:
        """Audio uploads store the clip and report transcription as pending (Whisper deferred)."""
        response = self.client.post(
            f"/sessions/{self.session.id}/audio",
            content=b"webm-bytes",
            headers={"Content-Type": "audio/webm"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["transcription"], "pending")


class CrossSiteRefusalTest(unittest.TestCase):
    """The CSRF defense still refuses forged cross-site mutations in exposed mode."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.db = Database(Path(self._dir.name) / "t.db")
        self.db.migrate()
        self.store = StructuredStore(self.db)
        self.context = AppContext(
            store=self.store,
            auth=AuthConfig(
                mode=AuthMode.EXPOSED,
                allowed_subjects=frozenset({"me@example.com"}),
                allowed_origin="https://app.example",
            ),
        )
        self.client = TestClient(create_app(self.context))

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_cross_site_approval_refused(self) -> None:
        """A cross-site POST to /approvals/{id} is refused before the handler runs."""
        response = self.client.post(
            "/approvals/tc_1",
            json={"approve": True},
            headers={"X-Auth-Subject": "me@example.com", "Origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
