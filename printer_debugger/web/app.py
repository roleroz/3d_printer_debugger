"""The FastAPI application ([web.md §3]).

Server-rendered behaviour with JSON payloads here; HTML templating and the client JavaScript
(camera, mic, SSE consumer) are a presentation layer over these routes. Every mutating request
passes the auth and CSRF checks first; the emergency stop is separate and minimal.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, BinaryIO, Callable, Iterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from ..kb.models import IngestOutcome
from ..store.artifact_store import ArtifactStore, artifact_key
from ..store.errors import ArtifactNotFoundError
from ..store.models import ArtifactKind, BindingReason, MessageRole
from ..store.structured_store import StructuredStore
from . import templates
from .security import AuthConfig, authorize, csrf_ok
from .sse import SseHub
from .transcription import TranscribeAudio

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_TYPES = {"app.js": "application/javascript", "styles.css": "text/css"}

# on_message(session_id, content) enqueues a turn; emergency_stop(printer_id) fires M112.
OnMessage = Callable[[str, list[Any]], Awaitable[None]]
EmergencyStop = Callable[[str], None]
# on_upload(artifact, body) builds and stores any index a just-uploaded file needs (a G-code
# index synchronously; a .3mf is stored whole, mesh read on demand). The composition supplies it.
OnUpload = Callable[[Any, bytes], None]
# ingest_kb(document) parses an uploaded knowledge-base markdown into printer records.
IngestKb = Callable[[str], IngestOutcome]


@dataclass
class AppContext:
    """Everything the routes need, injected so the app is testable in isolation."""

    store: StructuredStore
    auth: AuthConfig
    hub: SseHub = field(default_factory=SseHub)
    resolve_approval: Callable[[str, bool, str], bool] = lambda *_: False
    on_message: OnMessage | None = None
    on_upload: OnUpload | None = None
    ingest_kb: IngestKb | None = None
    emergency_stop: EmergencyStop | None = None
    artifacts: ArtifactStore | None = None
    transcribe: TranscribeAudio | None = None
    max_upload_bytes: int = 500 * 1024 * 1024


def create_app(context: AppContext) -> FastAPI:
    """Build the application with its routes and the auth/CSRF middleware."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # On shutdown, close the hub so every open SSE generator is unblocked and returns.
        # Without this, the long-lived event streams keep uvicorn's graceful shutdown
        # waiting forever on Ctrl+C.
        try:
            yield
        finally:
            context.hub.close()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(AuthCsrfMiddleware, context=context)
    _register_routes(app, context)
    return app


class AuthCsrfMiddleware:
    """Pure-ASGI auth + CSRF gate.

    Deliberately raw ASGI, not Starlette ``BaseHTTPMiddleware`` (``@app.middleware("http")``):
    that base class routes every response through an anyio memory-object stream and a task group,
    which buffers/stalls large upload responses, breaks long-lived SSE streams, and dumps
    ``CancelledError`` on shutdown. Inspecting only the scope headers here leaves the request and
    response byte streams flowing straight through uvicorn untouched.
    """

    def __init__(self, app: ASGIApp, context: AppContext) -> None:
        self._app = app
        self._context = context

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if not authorize(self._context.auth, headers.get("x-auth-subject")):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        if not csrf_ok(self._context.auth, scope["method"], headers.get("origin")):
            await JSONResponse(
                {"error": "cross-site request refused"}, status_code=403
            )(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _register_routes(app: FastAPI, context: AppContext) -> None:
    store = context.store

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"store": "ok", "mode": context.auth.mode.value}

    @app.get("/", response_class=HTMLResponse)
    async def session_list_page() -> Response:
        printers = {p.id: p for p in store.list_printers()}
        return HTMLResponse(
            templates.render_session_list(store.list_sessions(), printers, context.auth)
        )

    @app.get("/api/sessions")
    async def session_list() -> dict:
        return {
            "sessions": [
                {"id": s.id, "name": s.name, "state": s.state.value,
                 "last_active": s.last_active_at, "printer_id": s.printer_id}
                for s in store.list_sessions()
            ]
        }

    @app.get("/static/{name}")
    async def static_file(name: str) -> Response:
        content_type = _STATIC_TYPES.get(name)
        path = _STATIC_DIR / name
        if content_type is None or not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return Response(content=path.read_bytes(), media_type=content_type)

    @app.post("/sessions")
    async def create_session(body: dict) -> dict:
        session = store.create_session(name=body.get("name", "New session"))
        return {"id": session.id, "name": session.name}

    @app.get("/sessions/{session_id}", response_class=HTMLResponse)
    async def session_view_page(session_id: str) -> Response:
        session = store.get_session(session_id)
        if session is None:
            return HTMLResponse("<h1>Session not found</h1>", status_code=404)
        printer = (
            store.get_printer(session.printer_id) if session.printer_id is not None else None
        )
        return HTMLResponse(
            templates.render_session_view(
                session,
                store.list_messages(session_id),
                store.list_artifacts(session_id),
                printer,
                store.list_printers(),
                context.auth,
            )
        )

    @app.get("/api/sessions/{session_id}")
    async def session_view(session_id: str) -> Response:
        session = store.get_session(session_id)
        if session is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(
            {
                "id": session.id,
                "name": session.name,
                "messages": [
                    {"role": m.role.value, "content": m.content}
                    for m in store.list_messages(session_id)
                ],
                "artifacts": [
                    {"id": a.id, "kind": a.kind.value, "note": a.note}
                    for a in store.list_artifacts(session_id)
                ],
            }
        )

    @app.post("/sessions/{session_id}/messages")
    async def post_message(session_id: str, body: dict) -> dict:
        content = body.get("content") or [{"type": "text", "text": body.get("text", "")}]
        # The turn loop persists the user message when a handler is wired; only persist here as a
        # fallback (no agent), so the message is never written twice.
        if context.on_message is not None:
            await context.on_message(session_id, content)
        else:
            store.add_message(session_id, MessageRole.USER, content)
        return {"ok": True}

    @app.post("/sessions/{session_id}/files")
    async def upload_file(session_id: str, request: Request) -> Response:
        # Diagnostic instrumentation ([decisions.md 2026-07-23]): a remote photo upload reaches
        # 100% on the client, then the server never responds. These INFO lines pin down exactly
        # where it stalls — the "body read actual=…" line is the one that distinguishes a stalled
        # request body (never printed) from a stall in storage or the response (printed, but no
        # later line follows). Logging + timing only; no status codes or behaviour change.
        start = time.monotonic()
        declared = request.headers.get("Content-Length")
        content_type = request.headers.get("Content-Type", "application/octet-stream")
        filename = request.headers.get("X-Filename", "")
        client = request.client
        client_addr = f"{client.host}:{client.port}" if client is not None else "unknown"
        logger.info(
            "upload: enter session=%s client=%s scheme=%s declared_len=%s content_type=%s "
            "filename=%s",
            session_id, client_addr, request.url.scheme, declared, content_type, filename,
        )
        if declared is not None and int(declared) > context.max_upload_bytes:
            logger.warning(
                "upload rejected as too large: session=%s filename=%s content_type=%s "
                "declared=%s limit=%s",
                session_id, filename, content_type, declared, context.max_upload_bytes,
            )
            return JSONResponse(
                {"error": "file too large",
                 "limit_bytes": context.max_upload_bytes, "declared": int(declared)},
                status_code=413,
            )
        logger.info("upload: reading body… (declared=%s)", declared)
        read_start = time.monotonic()
        body = await request.body()
        read_elapsed = time.monotonic() - read_start
        logger.info("upload: body read actual=%d bytes in %.3fs", len(body), read_elapsed)
        if declared is not None and int(declared) != len(body):
            logger.warning(
                "upload: length mismatch declared=%s actual=%d", declared, len(body),
            )
        kind = _kind_for_upload(filename, content_type)
        try:
            store_start = time.monotonic()
            artifact = _store_upload(context, session_id, body, kind, content_type)
            logger.info(
                "upload: stored artifact=%s kind=%s in %.3fs",
                artifact.id, kind.value, time.monotonic() - store_start,
            )
            # Build any index the file needs synchronously, while the request is still open
            # ([decisions.md 2026-07-23]): a G-code index is stored now; a .3mf is stored whole.
            if context.on_upload is not None:
                on_upload_start = time.monotonic()
                context.on_upload(artifact, body)
                logger.info(
                    "upload: on_upload done in %.3fs", time.monotonic() - on_upload_start,
                )
        except Exception:
            logger.exception(
                "upload failed: session=%s filename=%s content_type=%s size=%d",
                session_id, filename, content_type, len(body),
            )
            return JSONResponse(
                {"error": "the upload could not be stored on the server"},
                status_code=500,
            )
        logger.info(
            "upload: responding 200 artifact=%s total=%.3fs",
            artifact.id, time.monotonic() - start,
        )
        return JSONResponse({"artifact_id": artifact.id, "size": len(body), "kind": kind.value})

    @app.post("/sessions/{session_id}/audio")
    async def upload_audio(session_id: str, request: Request) -> Response:
        # Lighter mirror of the /files instrumentation ([decisions.md 2026-07-23]): audio bodies
        # are small and succeed today, so only enter / body-read / responding are logged.
        start = time.monotonic()
        declared = request.headers.get("Content-Length")
        content_type = request.headers.get("Content-Type", "audio/webm")
        client = request.client
        client_addr = f"{client.host}:{client.port}" if client is not None else "unknown"
        logger.info(
            "upload: audio enter session=%s client=%s scheme=%s declared_len=%s content_type=%s",
            session_id, client_addr, request.url.scheme, declared, content_type,
        )
        if declared is not None and int(declared) > context.max_upload_bytes:
            logger.warning(
                "audio upload rejected as too large: session=%s content_type=%s "
                "declared=%s limit=%s",
                session_id, content_type, declared, context.max_upload_bytes,
            )
            return JSONResponse(
                {"error": "file too large",
                 "limit_bytes": context.max_upload_bytes, "declared": int(declared)},
                status_code=413,
            )
        logger.info("upload: audio reading body… (declared=%s)", declared)
        read_start = time.monotonic()
        body = await request.body()
        logger.info(
            "upload: audio body read actual=%d bytes in %.3fs",
            len(body), time.monotonic() - read_start,
        )
        # Transcribe with the bundled Whisper model when one is wired ([decisions.md 2026-07-23]);
        # the blocking CPU work runs off the event loop. A failure (or no transcriber) never fails
        # the upload — the clip is still stored, marked pending, so nothing is lost.
        transcript: str | None = None
        error: str | None = None
        if context.transcribe is not None:
            try:
                transcript = await asyncio.to_thread(context.transcribe, body, content_type)
            except Exception as exc:
                logger.exception(
                    "audio transcription failed: session=%s content_type=%s size=%d",
                    session_id, content_type, len(body),
                )
                error = str(exc)
        note = transcript if transcript else "transcription pending"
        try:
            artifact = _store_upload(
                context, session_id, body, ArtifactKind.AUDIO, content_type, note=note,
            )
        except Exception:
            logger.exception(
                "audio upload failed: session=%s content_type=%s size=%d",
                session_id, content_type, len(body),
            )
            return JSONResponse(
                {"error": "the audio clip could not be stored on the server"},
                status_code=500,
            )
        logger.info(
            "upload: audio responding artifact=%s total=%.3fs",
            artifact.id, time.monotonic() - start,
        )
        # A successful transcript reaches the session as a user message so the agent answers it,
        # taking the same path the text composer uses ([web.md §7]).
        if transcript:
            message_content = [{"type": "text", "text": transcript}]
            if context.on_message is not None:
                await context.on_message(session_id, message_content)
            else:
                store.add_message(session_id, MessageRole.USER, message_content)
            return JSONResponse(
                {"artifact_id": artifact.id, "size": len(body), "transcription": transcript}
            )
        if error is not None:
            return JSONResponse(
                {"artifact_id": artifact.id, "size": len(body),
                 "transcription": "failed", "error": error}
            )
        return JSONResponse(
            {"artifact_id": artifact.id, "size": len(body), "transcription": "pending"}
        )

    @app.get("/artifacts/{artifact_id}")
    async def serve_artifact(artifact_id: str) -> Response:
        artifact = store.get_artifact(artifact_id)
        if artifact is None or context.artifacts is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            stream = context.artifacts.open(artifact.blob_key)
        except ArtifactNotFoundError:
            return JSONResponse({"error": "not found"}, status_code=404)
        return StreamingResponse(_iter_blob(stream), media_type=artifact.content_type)

    @app.get("/sessions/{session_id}/stream")
    async def stream(session_id: str, request: Request) -> StreamingResponse:
        last_id = int(request.headers.get("Last-Event-ID", request.query_params.get("last_id", 0)))
        return StreamingResponse(
            _event_stream(context.hub, session_id, last_id, request),
            media_type="text/event-stream",
        )

    @app.post("/sessions/{session_id}/rename")
    async def rename_session(session_id: str, body: dict) -> Response:
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "name must not be empty"}, status_code=400)
        store.rename_session(session_id, name)
        return JSONResponse({"ok": True, "name": name})

    @app.post("/sessions/{session_id}/printer")
    async def bind_session_printer(session_id: str, request: Request) -> Response:
        # Accept both a plain HTML form submit (urlencoded) and a JSON fetch. The form path
        # returns a 303 back to the session page so the reload shows the new binding; the JSON
        # path returns the binding as JSON. Errors always answer with a status code + reason.
        content_type = request.headers.get("content-type", "")
        is_form = content_type.startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        )
        wants_html = is_form or "text/html" in request.headers.get("accept", "")
        body = await request.body()
        if is_form:
            parsed = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            values = parsed.get("printer_id")
            printer_id = (values[0] if values else "").strip()
        else:
            try:
                data = json.loads(body) if body else {}
            except (ValueError, json.JSONDecodeError):
                data = {}
            printer_id = str(data.get("printer_id") or "").strip()
        if not printer_id:
            return JSONResponse({"error": "printer_id must not be empty"}, status_code=400)
        session = store.get_session(session_id)
        if session is None:
            return JSONResponse({"error": "session not found"}, status_code=404)
        if store.get_printer(printer_id) is None:
            return JSONResponse({"error": "printer not found"}, status_code=404)
        reason = (
            BindingReason.REASSIGNED
            if session.printer_id and session.printer_id != printer_id
            else BindingReason.CHOSEN
        )
        store.bind_printer(session_id, printer_id, reason)
        if wants_html:
            return RedirectResponse(f"/sessions/{session_id}", status_code=303)
        return JSONResponse(
            {"ok": True, "printer_id": printer_id, "reason": reason.value}
        )

    @app.post("/sessions/{session_id}/close")
    async def close_session(session_id: str) -> dict:
        store.close_session(session_id)
        return {"ok": True}

    @app.post("/approvals/{tool_call_id}")
    async def decide_approval(tool_call_id: str, body: dict, request: Request) -> Response:
        subject = request.headers.get("X-Auth-Subject", "local-user")
        resolved = context.resolve_approval(tool_call_id, bool(body.get("approve")), subject)
        if not resolved:
            return JSONResponse({"error": "no pending proposal"}, status_code=409)
        return JSONResponse({"ok": True})

    @app.post("/printers/{printer_id}/estop")
    async def estop(printer_id: str) -> Response:
        # Separate and minimal: bypasses the agent, gate, and queue.
        if context.emergency_stop is None:
            return JSONResponse({"error": "no printer control"}, status_code=503)
        context.emergency_stop(printer_id)
        return JSONResponse({"stopped": True})

    @app.post("/printers/import")
    async def import_printers(request: Request) -> Response:
        # The uploaded markdown is the request body (raw-upload style shared with /files).
        if context.ingest_kb is None:
            return JSONResponse({"error": "printer import is not available"}, status_code=503)
        declared = request.headers.get("Content-Length")
        if declared is not None and int(declared) > context.max_upload_bytes:
            return JSONResponse(
                {"error": "file too large",
                 "limit_bytes": context.max_upload_bytes, "declared": int(declared)},
                status_code=413,
            )
        body = await request.body()
        if not body:
            return JSONResponse({"error": "no document was uploaded"}, status_code=400)
        text = body.decode("utf-8", errors="replace")
        outcome = context.ingest_kb(text)
        return JSONResponse(
            {
                "printers_upserted": list(outcome.printers_upserted),
                "printers_degraded": list(outcome.printers_degraded),
                "printers_absent": list(outcome.printers_absent),
                "unnamed_sections": list(outcome.unnamed_sections),
                "shared_context_headings": list(outcome.shared_context_headings),
                "messages": list(outcome.messages),
            }
        )

    @app.get("/printers")
    async def list_printers() -> dict:
        return {
            "printers": [
                {"id": p.id, "name": p.name, "status": p.status.value,
                 "absent": p.absent_since is not None}
                for p in store.list_printers()
            ]
        }


async def _event_stream(hub: SseHub, session_id: str, last_id: int, request: Request):
    """Yield missed-then-live events as SSE frames."""
    for event in hub.missed_since(session_id, last_id):
        yield _frame(event)
    queue = hub.subscribe(session_id)
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if event is None:  # shutdown sentinel from hub.close(); stop so uvicorn drains
                break
            yield _frame(event)
    finally:
        hub.unsubscribe(session_id, queue)


def _frame(event) -> str:
    return f"id: {event.id}\nevent: {event.kind}\ndata: {json.dumps(event.data)}\n\n"


def _kind_for_upload(filename: str, content_type: str) -> ArtifactKind:
    """Classify an uploaded blob by its filename first, then its declared content type.

    The slicer artifacts are distinguished by extension: ``.gcode``/``.g`` is G-code (which gets
    an index), ``.3mf`` is a project. Photos and audio fall back to the content type.
    """
    lowered = filename.lower()
    if lowered.endswith((".gcode", ".g", ".gco")):
        return ArtifactKind.GCODE
    if lowered.endswith(".3mf"):
        return ArtifactKind.PROJECT
    if content_type.startswith("image/"):
        return ArtifactKind.PHOTO
    if content_type.startswith("audio/"):
        return ArtifactKind.AUDIO
    return ArtifactKind.PROJECT


def _store_upload(
    context: AppContext,
    session_id: str,
    body: bytes,
    kind: ArtifactKind,
    content_type: str,
    note: str | None = None,
) -> Any:
    """Persist an uploaded blob (when an artifact store is present) and record its metadata.

    The blob key is derived from identifiers, never from a user-supplied filename, so a served
    artifact can be found again by its stored key ([store.md §5]).
    """
    blob_key = artifact_key(session_id, uuid.uuid4().hex)
    if context.artifacts is not None:
        context.artifacts.put(blob_key, io.BytesIO(body))
    return context.store.add_artifact(
        session_id=session_id, kind=kind, blob_key=blob_key,
        size_bytes=len(body), content_type=content_type, note=note,
    )


def _iter_blob(stream: BinaryIO, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """Yield an artifact's bytes in bounded chunks, closing the stream when exhausted."""
    try:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        stream.close()
