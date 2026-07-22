"""Composition root: wire the modules together and serve.

Builds the store, the web app, and (in local mode) an unauthenticated LAN server, then prints every
reachable URL and runs uvicorn. ``--check`` builds everything and exits without serving, so the
container image can be smoke-tested at build time. This is the seam where the Agent-SDK adapter and
the printer/KB providers are wired in a full deployment (see docs/implementation_notes.md).
"""

from __future__ import annotations

import argparse
import os

from printer_debugger.store.artifact_store import LocalFilesystemArtifactStore
from printer_debugger.store.db import Database
from printer_debugger.store.structured_store import StructuredStore
from printer_debugger.web.app import AppContext, create_app
from printer_debugger.web.security import AuthConfig, AuthMode, resolve_mode
from printer_debugger.web.startup import format_banner


def build(data_dir: str) -> tuple[AppContext, StructuredStore]:
    """Build the application context: migrated store, artifact store, and auth config."""
    os.makedirs(data_dir, exist_ok=True)
    database = Database(os.path.join(data_dir, "printer_debugger.db"))
    database.migrate()
    store = StructuredStore(database)
    LocalFilesystemArtifactStore(os.path.join(data_dir, "artifacts"))
    mode = resolve_mode(os.environ.get("PD_AUTH_MODE", "local"))
    if mode is AuthMode.LOCAL:
        auth = AuthConfig(mode=AuthMode.LOCAL)
    else:
        subjects = frozenset(
            s for s in os.environ.get("PD_ALLOWED_SUBJECTS", "").split(",") if s
        )
        auth = AuthConfig(
            mode=AuthMode.EXPOSED,
            allowed_subjects=subjects,
            allowed_origin=os.environ.get("PD_ALLOWED_ORIGIN"),
        )
    return AppContext(store=store, auth=auth), store


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

    print(format_banner(args.port, args.host), flush=True)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
