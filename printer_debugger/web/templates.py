"""Server-rendered HTML for the session list and the session view ([web.md §4]).

Pages are plain Python string templates with :func:`html.escape` on every interpolated value —
no templating engine, no bundler. The client JavaScript ([static/app.js][]) is a thin presentation
layer over the JSON/SSE routes; these functions produce the initial, already-usable HTML the
browser renders before any script runs. Mobile-first: one column, large touch targets.
"""

from __future__ import annotations

from html import escape
from typing import Any

from ..store.models import (
    Artifact,
    ArtifactKind,
    Message,
    MessageRole,
    Printer,
    Session,
    SessionState,
)
from .security import AuthConfig

_IMAGE_KINDS = frozenset({ArtifactKind.PHOTO, ArtifactKind.WEBCAM_STILL})


def render_page(title: str, body: str) -> str:
    """Wrap page ``body`` in the shared HTML document, pulling in the stylesheet and script."""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, '
        'viewport-fit=cover">\n'
        f"<title>{escape(title)}</title>\n"
        '<link rel="stylesheet" href="/static/styles.css">\n'
        "</head>\n"
        f"<body>\n{body}\n"
        '<script src="/static/app.js" defer></script>\n'
        "</body>\n"
        "</html>\n"
    )


def _mode_badge(auth: AuthConfig) -> str:
    """Render the always-visible authentication-mode badge ([web.md §8])."""
    mode = escape(auth.mode.value)
    return f'<span class="mode-badge mode-{mode}" title="authentication mode">{mode}</span>'


def render_session_list(
    sessions: list[Session], printers: dict[str, Printer], auth: AuthConfig
) -> str:
    """Render the session list: name, printer, last active, and state, sorted by recency."""
    rows: list[str] = []
    for session in sessions:
        printer = printers.get(session.printer_id or "")
        printer_name = printer.name if printer is not None else "no printer"
        state_class = "state-open" if session.state is SessionState.OPEN else "state-closed"
        rows.append(
            f'<li class="session-row {state_class}">'
            f'<a class="session-link" href="/sessions/{escape(session.id)}">'
            f'<span class="session-name">{escape(session.name)}</span>'
            f'<span class="session-meta">'
            f'<span class="session-printer">{escape(printer_name)}</span>'
            f'<span class="session-state">{escape(session.state.value)}</span>'
            f'<span class="session-active">{escape(session.last_active_at)}</span>'
            "</span></a></li>"
        )
    listing = (
        f'<ul class="session-list">{"".join(rows)}</ul>'
        if rows
        else '<p class="empty">No sessions yet. Start one to begin debugging a print.</p>'
    )
    body = (
        '<header class="top">'
        "<h1>3D Printer Debugger</h1>"
        f"{_mode_badge(auth)}"
        "</header>"
        '<main class="session-index">'
        '<button type="button" id="new-session" class="primary new-session">'
        "New session</button>"
        f"{listing}"
        "</main>"
    )
    return render_page("Sessions — 3D Printer Debugger", body)


def _render_block(block: Any) -> str:
    """Render one message content block: text as a paragraph, an image reference as a thumbnail."""
    if isinstance(block, dict):
        kind = block.get("type")
        if kind == "text":
            return f'<p class="block-text">{escape(str(block.get("text", "")))}</p>'
        artifact_id = block.get("artifact_id") or block.get("id")
        if kind in ("image", "photo") and artifact_id:
            src = f"/artifacts/{escape(str(artifact_id))}"
            return f'<img class="block-image" src="{src}" alt="attached image">'
        return f'<p class="block-text">{escape(str(block))}</p>'
    return f'<p class="block-text">{escape(str(block))}</p>'


def render_message(message: Message) -> str:
    """Render a single conversation message with its role for styling."""
    role = message.role.value if isinstance(message.role, MessageRole) else str(message.role)
    blocks = "".join(_render_block(block) for block in message.content)
    return (
        f'<div class="message role-{escape(role)}" data-role="{escape(role)}">'
        f'<span class="message-role">{escape(role)}</span>'
        f'<div class="message-body">{blocks}</div>'
        "</div>"
    )


def _format_size(size_bytes: int) -> str:
    """Render a byte count as a compact human-readable size."""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size_bytes} B"


def render_attachment(artifact: Artifact) -> str:
    """Render an attachment inline: photos as thumbnails, other files as name and size."""
    kind = artifact.kind if isinstance(artifact.kind, ArtifactKind) else None
    label = escape(artifact.note or (kind.value if kind else "file"))
    if kind in _IMAGE_KINDS:
        return (
            f'<figure class="attachment attachment-image">'
            f'<img src="/artifacts/{escape(artifact.id)}" alt="{label}">'
            f"<figcaption>{label}</figcaption></figure>"
        )
    pending = ""
    if kind is ArtifactKind.AUDIO:
        pending = '<span class="attachment-pending">transcription pending</span>'
    return (
        f'<div class="attachment attachment-file">'
        f'<span class="attachment-kind">{escape(kind.value if kind else "file")}</span>'
        f'<span class="attachment-name">{label}</span>'
        f'<span class="attachment-size">{escape(_format_size(artifact.size_bytes))}</span>'
        f"{pending}</div>"
    )


def _printer_strip(session: Session, printer: Printer | None) -> str:
    """Render the printer strip: connection state and placeholder temps/print status.

    The values are placeholders until the printer client is wired ([web.md §4.2]); the strip is
    structured now and its ``data-*`` hooks let the SSE consumer fill it live later.
    """
    connected = printer is not None
    name = escape(printer.name) if printer is not None else "no printer bound"
    printer_id = escape(printer.id) if printer is not None else ""
    state = "connected" if connected else "offline"
    return (
        f'<section class="printer-strip {"connected" if connected else "offline"}" '
        f'data-printer-id="{printer_id}">'
        f'<span class="printer-name">{name}</span>'
        f'<span class="printer-conn" data-field="connection">{state}</span>'
        '<span class="printer-temp" data-field="nozzle">nozzle --°C</span>'
        '<span class="printer-temp" data-field="bed">bed --°C</span>'
        '<span class="printer-print" data-field="print">idle</span>'
        "</section>"
    )


def _emergency_stop(printer: Printer | None) -> str:
    """Render the emergency-stop control, present only while a printer is connected."""
    if printer is None:
        return ""
    return (
        '<button type="button" id="estop" class="estop" '
        f'data-printer-id="{escape(printer.id)}">'
        "EMERGENCY STOP</button>"
    )


def _approval_template() -> str:
    """The hidden approval-block template the SSE consumer clones when a proposal arrives.

    Refusal properties baked into the markup ([web.md §5]): the block is not a ``<form>`` so Enter
    submits nothing; Reject precedes Approve in DOM and tab order; Approve carries no ``autofocus``
    and is not the default focus. The Enter guard itself lives in ``app.js``.
    """
    return (
        '<template id="approval-template">'
        '<section class="approval" role="alertdialog" aria-label="printer command approval" '
        'data-guard-enter="true">'
        '<h2 class="approval-title">Approval required</h2>'
        '<div class="approval-danger" data-field="danger" hidden></div>'
        '<p class="approval-intent" data-field="intent"></p>'
        '<pre class="approval-command" data-field="command"></pre>'
        '<p class="approval-countdown" data-field="countdown"></p>'
        '<div class="approval-actions">'
        '<button type="button" class="reject" data-role="reject">Reject</button>'
        '<button type="button" class="approve" data-role="approve">Approve</button>'
        "</div></section></template>"
    )


def render_session_view(
    session: Session,
    messages: list[Message],
    artifacts: list[Artifact],
    printer: Printer | None,
    auth: AuthConfig,
) -> str:
    """Render the session working surface: conversation, composer, attachments, printer strip."""
    conversation = "".join(render_message(message) for message in messages)
    attachments = "".join(render_attachment(artifact) for artifact in artifacts)
    body = (
        f'<div class="session-view" data-session-id="{escape(session.id)}" '
        f'data-stream="/sessions/{escape(session.id)}/stream">'
        '<header class="top">'
        f'<a class="back" href="/">&larr;</a>'
        f'<h1 class="session-title">{escape(session.name)}</h1>'
        '<button type="button" id="rename-btn" class="rename" '
        'title="Rename session">Rename</button>'
        f"{_mode_badge(auth)}"
        f"{_emergency_stop(printer)}"
        "</header>"
        f"{_printer_strip(session, printer)}"
        '<section class="approval-slot" id="approval-slot" aria-live="polite"></section>'
        f'<main class="conversation" id="conversation" aria-live="polite">{conversation}</main>'
        f'<section class="attachments" id="attachments">{attachments}</section>'
        '<form class="composer" id="composer" autocomplete="off">'
        '<textarea id="composer-text" name="text" rows="1" '
        'placeholder="Describe the problem…"></textarea>'
        '<div class="composer-actions">'
        '<label class="icon-btn camera" title="Take a photo">'
        '<input type="file" id="camera-input" accept="image/*" capture="environment" hidden>'
        "\U0001f4f7</label>"
        '<label class="icon-btn attach" title="Attach a .3mf or G-code file">'
        '<input type="file" id="file-input" accept=".3mf,.gcode,.gco,.g" hidden>'
        "\U0001f4ce</label>"
        '<button type="button" class="icon-btn mic" id="mic-btn" title="Record audio">'
        "\U0001f3a4</button>"
        '<button type="submit" class="icon-btn send" id="send-btn" title="Send">Send</button>'
        "</div>"
        '<div class="upload-progress" id="upload-progress" hidden>'
        '<progress id="upload-bar" max="100" value="0"></progress>'
        '<span class="upload-label" id="upload-label"></span>'
        "</div>"
        "</form>"
        f"{_approval_template()}"
        "</div>"
    )
    return render_page(f"{session.name} — 3D Printer Debugger", body)
