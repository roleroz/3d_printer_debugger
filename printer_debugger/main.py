"""Composition root: wire the modules together and serve.

Builds the store, the artifact store, the approval gate, the Claude-agent turn loop, and the web
app, then prints every reachable URL and runs uvicorn. ``--check`` builds everything and exits
without serving, so the container image can be smoke-tested at build time.

Startup refuses to serve without the subscription OAuth token ([decisions.md 2026-07-23]): there is
no API-key fallback and no graceful degradation. ``--check`` and the tests set a fake token, since
the SDK is never actually invoked in those paths.
"""

from __future__ import annotations

import argparse
import os

from printer_debugger import composition
from printer_debugger.kb.ingester import KbIngester
from printer_debugger.logging_setup import configure_logging
from printer_debugger.store.artifact_store import ArtifactStore, LocalFilesystemArtifactStore
from printer_debugger.store.db import Database
from printer_debugger.store.models import Artifact
from printer_debugger.store.structured_store import StructuredStore
from printer_debugger.web.app import AppContext, create_app
from printer_debugger.web.security import AuthConfig, AuthMode, resolve_mode
from printer_debugger.web.sse import SseHub
from printer_debugger.web.startup import format_banner
from printer_debugger.web.tls import ensure_self_signed, resolve_tls, tls_hostnames
from printer_debugger.web.transcription import Transcriber

# Where the hermetically bundled faster-whisper ``base`` model is placed in the image
# (see //:whisper_model_layer); overridable for local runs via PD_WHISPER_MODEL_DIR.
_DEFAULT_WHISPER_MODEL_DIR = "/app/whisper-base"


def _on_upload(
    store: StructuredStore, artifacts: ArtifactStore, artifact: Artifact, body: bytes
) -> None:
    """Build the file index for an upload, then attempt printer auto-detection from a ``.3mf``."""
    composition.build_index_for_upload(store, artifacts, artifact, body)
    composition.auto_bind_from_project(store, artifacts, artifact, body)


def build(data_dir: str) -> tuple[AppContext, StructuredStore]:
    """Build the application context with the agent turn, gate, and index build wired in.

    Raises ``composition.StartupError`` if the OAuth token is absent, so the process exits before
    serving rather than accepting messages it cannot answer.
    """
    token = composition.require_oauth_token(os.environ)
    model, effort = composition.resolve_model_effort(os.environ)

    os.makedirs(data_dir, exist_ok=True)
    database = Database(os.path.join(data_dir, "printer_debugger.db"))
    database.migrate()
    store = StructuredStore(database)
    artifacts = LocalFilesystemArtifactStore(os.path.join(data_dir, "artifacts"))

    # Startup recovery ([orchestration.md §7]): mark in-flight tool calls interrupted and resolve
    # any proposal left pending by a dead process to timed-out — never to an approval.
    store.sweep_interrupted_tool_calls()
    hub = SseHub()
    gate = composition.make_gate(store, hub)
    gate.recover_pending()

    kb = KbIngester(store)
    # The model loads lazily on the first clip, so constructing this never touches the files or
    # delays startup; --check builds the context without transcribing anything.
    whisper_model_dir = os.environ.get("PD_WHISPER_MODEL_DIR", _DEFAULT_WHISPER_MODEL_DIR)
    transcriber = Transcriber(whisper_model_dir)
    catalog_text = composition.load_catalog_text()
    client_factory = composition.make_client_factory(
        store, artifacts, gate, kb, catalog_text, token, model, effort
    )

    context = AppContext(
        store=store,
        auth=_auth_config(),
        hub=hub,
        artifacts=artifacts,
        on_message=composition.make_on_message(store, hub, client_factory),
        on_upload=lambda artifact, body: _on_upload(store, artifacts, artifact, body),
        ingest_kb=lambda text: kb.ingest(text),
        resolve_approval=gate.resolve,
        emergency_stop=composition.make_emergency_stop(store),
        transcribe=transcriber.transcribe,
    )
    return context, store


def _auth_config() -> AuthConfig:
    mode = resolve_mode(os.environ.get("PD_AUTH_MODE", "local"))
    if mode is AuthMode.LOCAL:
        return AuthConfig(mode=AuthMode.LOCAL)
    subjects = frozenset(
        s for s in os.environ.get("PD_ALLOWED_SUBJECTS", "").split(",") if s
    )
    return AuthConfig(
        mode=AuthMode.EXPOSED,
        allowed_subjects=subjects,
        allowed_origin=os.environ.get("PD_ALLOWED_ORIGIN"),
    )


def main() -> int:
    """Entry point: build the app and either smoke-check or serve it."""
    parser = argparse.ArgumentParser(description="3D Printer Debugger")
    parser.add_argument("--check", action="store_true", help="build and exit without serving")
    parser.add_argument("--host", default=os.environ.get("PD_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PD_PORT", "8080")))
    parser.add_argument("--data-dir", default=os.environ.get("PD_DATA_DIR", "/data"))
    args = parser.parse_args()

    context, _ = build(args.data_dir)
    app = create_app(context)
    if args.check:
        # --check must not generate a cert or serve; it only proves the app builds. Logging
        # configuration is a side effect, so it stays out of this path.
        print("printer_debugger: build check OK")
        return 0

    # Configure application logging before serving so our printer_debugger.* records (auto-bind
    # decisions, upload handling) reach the console; uvicorn only configures its own loggers.
    configure_logging(os.environ.get("PD_LOG_LEVEL", "INFO"))

    # Serve HTTPS by default so browsers grant a secure context for mic recording; the cert
    # persists under the data dir so it survives restarts ([decisions.md 2026-07-23]).
    tls = resolve_tls(os.environ, args.data_dir)
    if tls is not None and tls.auto:
        ensure_self_signed(tls.cert_path, tls.key_path, tls_hostnames(os.environ))

    print(
        format_banner(
            args.port,
            args.host,
            os.environ.get("PD_ADVERTISE_HOST"),
            tls=tls is not None,
            self_signed=tls is not None and tls.auto,
        ),
        flush=True,
    )
    import uvicorn

    # The app's lifespan shutdown closes the SSE hub so every stream returns; this bounded
    # graceful-shutdown timeout is a backstop in case any connection is still draining.
    run_kwargs: dict[str, object] = {
        "host": args.host,
        "port": args.port,
        "timeout_graceful_shutdown": 5,
    }
    if tls is not None:
        run_kwargs["ssl_certfile"] = tls.cert_path
        run_kwargs["ssl_keyfile"] = tls.key_path
    uvicorn.run(app, **run_kwargs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
