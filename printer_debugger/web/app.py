"""The FastAPI application ([web.md §3]).

Server-rendered behaviour with JSON payloads here; HTML templating and the client JavaScript
(camera, mic, SSE consumer) are a presentation layer over these routes. Every mutating request
passes the auth and CSRF checks first; the emergency stop is separate and minimal.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, BinaryIO, Callable, Iterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from ..kb.models import IngestOutcome
from ..store.artifact_store import ArtifactStore, artifact_key
from ..store.errors import ArtifactNotFoundError
from ..store.models import ArtifactKind, MessageRole
from ..store.structured_store import StructuredStore
from . import templates
from .security import AuthConfig, authorize, csrf_ok
from .sse import SseHub

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

    @app.middleware("http")
    async def guard(request: Request, call_next):
        subject = request.headers.get("X-Auth-Subject")
        if not authorize(context.auth, subject):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not csrf_ok(context.auth, request.method, request.headers.get("Origin")):
            return JSONResponse({"error": "cross-site request refused"}, status_code=403)
        return await call_next(request)

    _register_routes(app, context)
    return app


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
        declared = request.headers.get("Content-Length")
        if declared is not None and int(declared) > context.max_upload_bytes:
            return JSONResponse(
                {"error": "file too large",
                 "limit_bytes": context.max_upload_bytes, "declared": int(declared)},
                status_code=413,
            )
        content_type = request.headers.get("Content-Type", "application/octet-stream")
        filename = request.headers.get("X-Filename", "")
        body = await request.body()
        kind = _kind_for_upload(filename, content_type)
        artifact = _store_upload(context, session_id, body, kind, content_type)
        # Build any index the file needs synchronously, while the request is still open
        # ([decisions.md 2026-07-23]): a G-code index is stored now; a .3mf is stored whole.
        if context.on_upload is not None:
            context.on_upload(artifact, body)
        return JSONResponse({"artifact_id": artifact.id, "size": len(body), "kind": kind.value})

    @app.post("/sessions/{session_id}/audio")
    async def upload_audio(session_id: str, request: Request) -> Response:
        declared = request.headers.get("Content-Length")
        if declared is not None and int(declared) > context.max_upload_bytes:
            return JSONResponse(
                {"error": "file too large",
                 "limit_bytes": context.max_upload_bytes, "declared": int(declared)},
                status_code=413,
            )
        content_type = request.headers.get("Content-Type", "audio/webm")
        body = await request.body()
        # Whisper transcription is deferred; the clip is stored and marked pending.
        artifact = _store_upload(
            context, session_id, body, ArtifactKind.AUDIO, content_type,
            note="transcription pending",
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
