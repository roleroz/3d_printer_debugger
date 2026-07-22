"""The FastAPI application ([web.md §3]).

Server-rendered behaviour with JSON payloads here; HTML templating and the client JavaScript
(camera, mic, SSE consumer) are a presentation layer over these routes. Every mutating request
passes the auth and CSRF checks first; the emergency stop is separate and minimal.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..store.models import ArtifactKind, MessageRole
from ..store.structured_store import StructuredStore
from .security import AuthConfig, AuthMode, authorize, csrf_ok
from .sse import SseHub

# on_message(session_id, content) enqueues a turn; emergency_stop(printer_id) fires M112.
OnMessage = Callable[[str, list[Any]], Awaitable[None]]
EmergencyStop = Callable[[str], None]


@dataclass
class AppContext:
    """Everything the routes need, injected so the app is testable in isolation."""

    store: StructuredStore
    auth: AuthConfig
    hub: SseHub = field(default_factory=SseHub)
    resolve_approval: Callable[[str, bool, str], bool] = lambda *_: False
    on_message: OnMessage | None = None
    emergency_stop: EmergencyStop | None = None
    max_upload_bytes: int = 500 * 1024 * 1024


def create_app(context: AppContext) -> FastAPI:
    """Build the application with its routes and the auth/CSRF middleware."""
    app = FastAPI()

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

    @app.get("/")
    async def session_list() -> dict:
        return {
            "sessions": [
                {"id": s.id, "name": s.name, "state": s.state.value,
                 "last_active": s.last_active_at, "printer_id": s.printer_id}
                for s in store.list_sessions()
            ]
        }

    @app.post("/sessions")
    async def create_session(body: dict) -> dict:
        session = store.create_session(name=body.get("name", "New session"))
        return {"id": session.id, "name": session.name}

    @app.get("/sessions/{session_id}")
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
        store.add_message(session_id, MessageRole.USER, content)
        if context.on_message is not None:
            await context.on_message(session_id, content)
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
        body = await request.body()
        # A real deployment streams to the artifact store; here we record the metadata.
        artifact = store.add_artifact(
            session_id=session_id, kind=ArtifactKind.PROJECT,
            blob_key=f"sessions/{session_id}/upload", size_bytes=len(body),
            content_type=request.headers.get("Content-Type", "application/octet-stream"),
        )
        return JSONResponse({"artifact_id": artifact.id, "size": len(body)})

    @app.get("/sessions/{session_id}/stream")
    async def stream(session_id: str, request: Request) -> StreamingResponse:
        last_id = int(request.headers.get("Last-Event-ID", request.query_params.get("last_id", 0)))
        return StreamingResponse(
            _event_stream(context.hub, session_id, last_id, request),
            media_type="text/event-stream",
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
            yield _frame(event)
    finally:
        hub.unsubscribe(session_id, queue)


def _frame(event) -> str:
    return f"id: {event.id}\nevent: {event.kind}\ndata: {json.dumps(event.data)}\n\n"
