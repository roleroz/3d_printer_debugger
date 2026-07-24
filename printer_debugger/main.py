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
from printer_debugger.store.artifact_store import LocalFilesystemArtifactStore
from printer_debugger.store.db import Database
from printer_debugger.store.structured_store import StructuredStore
from printer_debugger.web.app import AppContext, create_app
from printer_debugger.web.security import AuthConfig, AuthMode, resolve_mode
from printer_debugger.web.sse import SseHub
from printer_debugger.web.startup import format_banner


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
        on_upload=lambda artifact, body: composition.build_index_for_upload(
            store, artifacts, artifact, body
        ),
        ingest_kb=lambda text: kb.ingest(text),
        resolve_approval=gate.resolve,
        emergency_stop=composition.make_emergency_stop(store),
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
        print("printer_debugger: build check OK")
        return 0

    print(
        format_banner(args.port, args.host, os.environ.get("PD_ADVERTISE_HOST")), flush=True
    )
    import uvicorn

    # The app's lifespan shutdown closes the SSE hub so every stream returns; this bounded
    # graceful-shutdown timeout is a backstop in case any connection is still draining.
    uvicorn.run(app, host=args.host, port=args.port, timeout_graceful_shutdown=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
