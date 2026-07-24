"""Composition root wiring: the agent turn, the approval gate, and the synchronous index build.

This is the seam the module design docs defer to the composition root ([orchestration.md §10 T4.1,
web.md, file_indexing.md]). It stays free of any top-level ``claude_agent_sdk`` import — every use
of the SDK is lazy (inside :class:`ClaudeAgentClient`, and inside :func:`build_servers` via
``indexing.mcp``). So the whole module imports, and every wiring seam here is exercised, without
fetching the ~273 MB SDK wheel; only ``//printer_debugger:main`` carries that Bazel dependency for
the container image.

Execution locus of a printer write ([decisions.md 2026-07-23]): the approval gate *decides only* —
it publishes the proposal, awaits the human, and records the decision, with a no-op ``execute``. The
``propose_command`` MCP tool performs the single actual submission to the printer, and only on the
approved path, so a command is submitted exactly once.
"""

from __future__ import annotations

import io
import logging
import re
import uuid
from collections.abc import Mapping
from typing import Any, Awaitable, Callable

from .indexing import gcode, index_format
from .indexing.gcode_server import GcodeTools
from .indexing.project_server import ArtifactSink, ProjectTools
from .indexing.threemf import Project
from .orchestration import prompt
from .orchestration.gate import ApprovalGate, Proposal
from .orchestration.prompt import PromptInputs
from .orchestration.sdk_client import ClaudeAgentClient
from .orchestration.turn import AgentEvent, ErrorEvent, TextEvent, ToolStartEvent, TurnLoop
from .procedures import catalog as procedures_catalog
from .store.artifact_store import ArtifactStore, artifact_key, index_key
from .store.errors import ArtifactNotFoundError
from .store.models import (
    ApprovalDecision,
    Artifact,
    ArtifactKind,
    BindingReason,
    FileIndexKind,
)
from .store.structured_store import StructuredStore
from .web.sse import SseHub

_LOG = logging.getLogger(__name__)

OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_EFFORT = "medium"

# submit(command) -> (status_code, body). The printer transport, injected so tests never hit HTTP.
Transport = Callable[[str, str, "bytes | None"], "tuple[int, bytes]"]


class StartupError(RuntimeError):
    """A required startup precondition is missing; the process must not serve."""


# -- credentials and model/effort configuration (startup) --------------------------------------


def require_oauth_token(env: Mapping[str, str]) -> str:
    """Return the subscription OAuth token, or raise so the process crashes at startup.

    There is no API-key fallback and no graceful degradation ([decisions.md 2026-07-23]): the token
    is ``CLAUDE_CODE_OAUTH_TOKEN`` (create one with ``claude setup-token``).
    """
    token = env.get(OAUTH_ENV, "").strip()
    if not token:
        raise StartupError(
            f"{OAUTH_ENV} is not set. The agent authenticates with a Claude subscription OAuth "
            "token; create one with `claude setup-token` and set it before starting."
        )
    return token


def resolve_model_effort(env: Mapping[str, str]) -> tuple[str, str]:
    """Resolve the model and effort from the environment, defaulting to Opus at medium effort."""
    return (
        (env.get("PD_MODEL") or DEFAULT_MODEL),
        (env.get("PD_EFFORT") or DEFAULT_EFFORT),
    )


# -- synchronous index build on upload ([decisions.md 2026-07-23]) ------------------------------


def build_index_for_upload(
    store: StructuredStore, artifacts: ArtifactStore, artifact: Artifact, body: bytes
) -> None:
    """Build and store a G-code index synchronously; a ``.3mf`` is stored whole (mesh on demand).

    Called from the upload request so the index is ready by the time the response returns. The
    index blob is stored under a version-stamped key and a ``file_index`` row records it.
    """
    if artifact.kind is not ArtifactKind.GCODE:
        return
    text = body.decode("utf-8", errors="replace")
    index = gcode.build_index(text)
    key = index_key(artifact.id, index.format_version)
    artifacts.put(key, io.BytesIO(index_format.dumps(index)))
    store.add_file_index(
        artifact_id=artifact.id,
        kind=FileIndexKind.GCODE,
        blob_key=key,
        format_version=index.format_version,
    )


def auto_bind_from_project(
    store: StructuredStore, artifacts: ArtifactStore, artifact: Artifact, body: bytes
) -> str | None:
    """Auto-bind the session to a known printer from an uploaded ``.3mf``'s printer identity.

    Runs on a ``.3mf`` upload and, conservatively, binds the session to a matching known printer
    with reason ``DETECTED``. An existing binding (user-chosen or previously detected) is never
    overwritten, and an ambiguous match is left for the manual picker. Every decision is logged at
    INFO so the outcome is observable on the console. The printer set is checked first: a single
    known printer binds without needing the project identity, so an identity-read failure can no
    longer silently kill the reliable single-printer case. Any unexpected failure is logged and
    swallowed — detection must never break the upload. Returns the bound printer id, or None.
    """
    if artifact.kind is not ArtifactKind.PROJECT:
        return None
    try:
        session = store.get_session(artifact.session_id)
        if session is None:
            return None
        if session.printer_id is not None:
            _LOG.info(
                "auto-bind: session %s already bound to %s; skipping",
                artifact.session_id,
                session.printer_id,
            )
            return None  # never auto-rebind over an existing/user binding.

        printers = store.list_printers()
        if len(printers) == 0:
            _LOG.info("auto-bind: no known printers; skipping")
            return None
        if len(printers) == 1:
            only = printers[0]
            store.bind_printer(artifact.session_id, only.id, BindingReason.DETECTED)
            _LOG.info(
                "auto-bind: single known printer '%s'; bound (DETECTED)", only.name
            )
            return only.id

        # Several printers: the project identity is needed to judge candidates. Reading it is
        # guarded — a malformed .3mf means the identity cannot be read, not an unexpected failure.
        try:
            identity = _project_identity_string(body)
        except Exception:
            identity = None
        if not identity:
            _LOG.info(
                "auto-bind: could not read project identity from upload; "
                "left for the manual picker"
            )
            return None
        _LOG.info("auto-bind: project identity = '%s'", identity)
        names = [printer.name for printer in printers]
        printer_id = _match_printer(printers, identity)
        if printer_id is None:
            _LOG.info(
                "auto-bind: no unique match among %s for identity '%s'; "
                "left for the manual picker",
                names,
                identity,
            )
            return None
        matched_name = next(
            (printer.name for printer in printers if printer.id == printer_id), printer_id
        )
        store.bind_printer(artifact.session_id, printer_id, BindingReason.DETECTED)
        _LOG.info("auto-bind: matched '%s'; bound (DETECTED)", matched_name)
        return printer_id
    except Exception:  # a detection failure must never break the upload.
        _LOG.exception("auto-bind failed")
        return None


def _project_identity_string(body: bytes) -> str | None:
    """Extract the project's most descriptive printer identity string from ``.3mf`` bytes."""
    project = Project.from_bytes(body)
    tools = ProjectTools(project)
    identity = tools.get_printer_identity()
    value = identity.get("printer_settings_id")
    if not value:
        value = tools.get_settings(key="printer_settings_id").get("value")
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return str(value) if value else None


def _normalize(text: str) -> str:
    """Lowercase and strip everything non-alphanumeric, for tolerant name matching."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _match_printer(printers: list[Any], identity: str) -> str | None:
    """Pick the one printer that matches ``identity``, conservatively, or None if ambiguous.

    One printer binds unconditionally (the reliable common case). With several, a printer is a
    candidate when its normalized name is a substring of the normalized identity, or vice-versa;
    only an unambiguous single candidate binds.
    """
    if len(printers) == 1:
        return printers[0].id
    normalized_identity = _normalize(identity)
    if not normalized_identity:
        return None
    candidates = []
    for printer in printers:
        normalized_name = _normalize(printer.name)
        if not normalized_name:
            continue
        if normalized_name in normalized_identity or normalized_identity in normalized_name:
            candidates.append(printer.id)
    return candidates[0] if len(candidates) == 1 else None


def load_gcode_tools(
    store: StructuredStore, artifacts: ArtifactStore, session_id: str
) -> GcodeTools | None:
    """Reconstruct ``GcodeTools`` over the session's most recent indexed G-code, or None."""
    for artifact in reversed(store.list_artifacts(session_id)):
        if artifact.kind is not ArtifactKind.GCODE:
            continue
        file_index = store.get_file_index(artifact.id)
        if file_index is None:
            continue
        with artifacts.open(file_index.blob_key) as blob:
            index = index_format.loads(blob.read())
        with artifacts.open(artifact.blob_key) as raw:
            text = raw.read().decode("utf-8", errors="replace")
        return GcodeTools(index, text)
    return None


def load_project_tools(
    store: StructuredStore, artifacts: ArtifactStore, session_id: str
) -> ProjectTools | None:
    """Reconstruct ``ProjectTools`` over the session's most recent uploaded ``.3mf``, or None.

    A ``.3mf`` is a ZIP and needs random access, so its blob is read whole into memory rather than
    streamed. The tools are given a sink that persists any render into the artifact store so the UI
    can fetch it.
    """
    for artifact in reversed(store.list_artifacts(session_id)):
        if artifact.kind is not ArtifactKind.PROJECT:
            continue
        with artifacts.open(artifact.blob_key) as blob:
            data = blob.read()
        project = Project.from_bytes(data)
        return ProjectTools(
            project, artifact_sink=make_render_sink(store, artifacts, session_id)
        )
    return None


def make_render_sink(
    store: StructuredStore, artifacts: ArtifactStore, session_id: str
) -> ArtifactSink:
    """Build the sink ``ProjectTools`` calls to persist a render and get back a fetchable reference.

    The PNG is stored under a session-scoped blob key, an image artifact row is recorded, and a
    ``/artifacts/{id}`` reference is returned — the same key/row machinery an upload uses, so the
    ``serve_artifact`` route resolves it.
    """

    def sink(data: bytes, name: str) -> str:
        blob_key = artifact_key(session_id, uuid.uuid4().hex, ".png")
        artifacts.put(blob_key, io.BytesIO(data))
        artifact = store.add_artifact(
            session_id=session_id,
            kind=ArtifactKind.PROCEDURE_OUTPUT,
            blob_key=blob_key,
            size_bytes=len(data),
            content_type="image/png",
            note=name,
        )
        return f"/artifacts/{artifact.id}"

    return sink


# -- the approval gate wiring (publish → await → record; no execution) --------------------------


def make_gate(store: StructuredStore, hub: SseHub, timeout_seconds: float = 300.0) -> ApprovalGate:
    """Build the approval gate: it publishes proposals to viewers and never executes anything."""
    return ApprovalGate(
        store, _publisher(hub), _no_execute, timeout_seconds=timeout_seconds
    )


def _publisher(hub: SseHub) -> Callable[[Proposal], None]:
    def publish(proposal: Proposal) -> None:
        hub.publish(
            proposal.session_id,
            "proposal",
            {
                "tool_call_id": proposal.tool_call_id,
                "command": proposal.command,
                "danger_flags": list(proposal.danger_flags),
            },
        )

    return publish


async def _no_execute(command: str) -> dict[str, Any]:
    """The gate decides only; the ``propose_command`` tool submits on the approved path."""
    return {}


def make_approve(
    store: StructuredStore, gate: ApprovalGate, session_id: str
) -> Callable[[str, tuple[str, ...]], Awaitable[bool]]:
    """Build the ``approve`` bridge for a session: route a proposal to the gate and report the vote.

    Correlates with the ``propose_command`` tool call the turn loop recorded (the latest one with no
    approval yet); if none exists, it records one so the decision has a home in the audit trail.
    """

    async def approve(command: str, danger_flags: tuple[str, ...]) -> bool:
        tool_call_id = _pending_propose_call_id(store, session_id, command)
        outcome = await gate.decide(
            session_id=session_id,
            tool_call_id=tool_call_id,
            command=command,
            danger_flags=tuple(danger_flags),
        )
        return outcome.decision is ApprovalDecision.APPROVED

    return approve


def _pending_propose_call_id(
    store: StructuredStore, session_id: str, command: str
) -> str:
    for call in reversed(store.list_tool_calls(session_id)):
        if call.tool == "propose_command" and store.get_approval(call.id) is None:
            return call.id
    call = store.start_tool_call(
        session_id=session_id,
        server="printer",
        tool="propose_command",
        arguments={"command": command},
    )
    return call.id


# -- the single printer-submission point (the propose_command tool's gate seam) -----------------


def make_command_submitter(
    base_url: str, transport: Transport
) -> Callable[[str, Any], dict[str, Any]]:
    """Build the submitter the ``propose_command`` tool calls once, only after approval.

    This is the *only* place a printer write reaches the machine on the agent path; the approval
    gate's ``execute`` is a no-op, so the command is submitted exactly once.
    """
    from urllib.parse import quote

    from .printer.moonraker import PrinterUnreachable

    def submit(command: str, _classification: Any) -> dict[str, Any]:
        url = base_url.rstrip("/") + "/printer/gcode/script?script=" + quote(command)
        try:
            status, _ = transport("POST", url, b"")
        except PrinterUnreachable as exc:
            return {"executed": False, "reason": f"printer unreachable: {exc}"}
        if status not in (200, 204):
            return {"executed": False, "reason": f"submit returned {status}"}
        return {"executed": True}

    return submit


# -- per-session MCP servers (live path; needs the SDK) -----------------------------------------


def build_printer_tools(store: StructuredStore, session_id: str):
    """Build ``PrinterTools`` for the session's bound, addressable printer, or None."""
    session = store.get_session(session_id)
    if session is None or session.printer_id is None:
        return None
    printer = store.get_printer(session.printer_id)
    if printer is None or not printer.address:
        return None
    from .printer.moonraker import MoonrakerClient
    from .printer.tools import PrinterTools

    client = MoonrakerClient(base_url=printer.address)
    snapshot = store.latest_config_snapshot(printer.id)
    submit = make_command_submitter(printer.address, client.transport)
    return PrinterTools(
        client, config_text=snapshot.contents if snapshot else "", gate=submit
    )


def build_servers(  # pragma: no cover - needs the Agent SDK
    store: StructuredStore, artifacts: ArtifactStore, session_id: str
) -> dict[str, Any]:
    """Construct whichever in-process MCP servers the session has data for."""
    from .indexing.mcp import build_gcode_server, build_project_server, build_sdk_server

    servers: dict[str, Any] = {}
    gtools = load_gcode_tools(store, artifacts, session_id)
    if gtools is not None:
        servers["gcode"] = build_gcode_server(gtools)
    ptools_project = load_project_tools(store, artifacts, session_id)
    if ptools_project is not None:
        servers["project"] = build_project_server(ptools_project)
    ptools = build_printer_tools(store, session_id)
    if ptools is not None:
        servers["printer"] = build_sdk_server("printer", ptools)
    return servers


# -- system prompt assembly per session --------------------------------------------------------


def build_prompt(
    store: StructuredStore, kb: Any, catalog_text: str, session_id: str
) -> str:
    """Assemble the system prompt from the catalog, the bound printer KB view, and session state."""
    session = store.get_session(session_id)
    shared = printer_section = snapshot = ""
    if session is not None and session.printer_id is not None and kb is not None:
        try:
            view = kb.assemble_view(session.printer_id)
        except KeyError:
            view = None
        if view is not None:
            shared = view.shared_context or ""
            printer_section = view.section_text or ""
            snapshot = view.snapshot_contents or ""
    return prompt.assemble(
        PromptInputs(
            procedure_catalog=catalog_text,
            shared_context=shared,
            printer_section=printer_section,
            printer_snapshot=snapshot,
            session_state=_session_state_text(store, session_id),
        )
    )


def _session_state_text(store: StructuredStore, session_id: str) -> str:
    artifacts = store.list_artifacts(session_id)
    if not artifacts:
        return "No files uploaded to this session yet."
    lines = []
    for artifact in artifacts:
        indexed = " (indexed)" if store.get_file_index(artifact.id) else ""
        lines.append(f"- {artifact.kind.value}{indexed}, {artifact.size_bytes} bytes")
    return "Uploaded to this session:\n" + "\n".join(lines)


# -- the turn into the request path ------------------------------------------------------------


def make_on_message(
    store: StructuredStore,
    hub: SseHub,
    client_factory: Callable[[str], Any],
) -> Callable[[str, list[Any]], Awaitable[None]]:
    """Build ``on_message``: run a turn via the turn loop, streaming each event to the SSE hub."""

    async def on_message(session_id: str, content: list[Any]) -> None:
        client = client_factory(session_id)
        loop = TurnLoop(
            store, client, on_event=lambda event: _forward_event(hub, session_id, event)
        )
        await loop.run(session_id, content)

    return on_message


def _forward_event(hub: SseHub, session_id: str, event: AgentEvent) -> None:
    """Stream one turn event to the session's viewers as an SSE frame."""
    if isinstance(event, TextEvent):
        if event.text:
            hub.publish(session_id, "assistant", {"text": event.text})
    elif isinstance(event, ToolStartEvent):
        hub.publish(session_id, "tool", {"server": event.server, "tool": event.tool})
    elif isinstance(event, ErrorEvent):
        # A distinct channel; not "error", which EventSource reserves for its connection-error
        # event (that would trip the client's reconnect handler instead of rendering the message).
        hub.publish(session_id, "agent_error", {"message": event.message})


def make_artifact_reader(
    store: StructuredStore, artifacts: ArtifactStore
) -> Callable[[str], tuple[bytes, str]]:
    """Build the reader the SDK client uses to inline an image artifact's bytes for the model.

    Given an artifact id, it returns the raw bytes and the stored content type, so an image
    reference block in a persisted user message can be expanded to a base64 image block at the SDK
    boundary. A missing artifact row raises ``ArtifactNotFoundError`` rather than reading nothing.
    """

    def read(artifact_id: str) -> tuple[bytes, str]:
        artifact = store.get_artifact(artifact_id)
        if artifact is None:
            raise ArtifactNotFoundError(artifact_id)
        with artifacts.open(artifact.blob_key) as blob:
            data = blob.read()
        return data, artifact.content_type

    return read


def make_client_factory(  # pragma: no cover - constructs the live SDK client
    store: StructuredStore,
    artifacts: ArtifactStore,
    gate: ApprovalGate,
    kb: Any,
    catalog_text: str,
    token: str,
    model: str,
    effort: str,
) -> Callable[[str], ClaudeAgentClient]:
    """Build the per-session ``ClaudeAgentClient`` factory used by ``on_message``."""

    def factory(session_id: str) -> ClaudeAgentClient:
        return ClaudeAgentClient(
            approve=make_approve(store, gate, session_id),
            build_servers=lambda sid: build_servers(store, artifacts, sid),
            build_prompt=lambda sid: build_prompt(store, kb, catalog_text, sid),
            resume_lookup=_resume_lookup(store),
            read_artifact=make_artifact_reader(store, artifacts),
            model=model,
            effort=effort,
            env={OAUTH_ENV: token},
        )

    return factory


def _resume_lookup(store: StructuredStore) -> Callable[[str], str | None]:
    def lookup(session_id: str) -> str | None:
        session = store.get_session(session_id)
        return session.sdk_session_id if session is not None else None

    return lookup


# -- emergency stop ----------------------------------------------------------------------------


def make_emergency_stop(store: StructuredStore) -> Callable[[str], None]:
    """Build the emergency-stop callback: fire ``M112`` at the printer's address, bypassing all."""
    from .printer.emergency import EmergencyStopFailed, emergency_stop

    def estop(printer_id: str) -> None:
        printer = store.get_printer(printer_id)
        if printer is None or not printer.address:
            raise EmergencyStopFailed(f"printer {printer_id} has no address to stop")
        emergency_stop(printer.address)

    return estop


def load_catalog_text() -> str:
    """Load and render the procedure catalog once, for the cached system-prompt prefix."""
    return procedures_catalog.render_for_prompt(procedures_catalog.load_catalog())
