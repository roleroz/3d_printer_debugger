# Deployment

The system is built and packaged entirely with Bazel — no Dockerfile, no Docker daemon needed to
build the image.

## Build and test

```bash
bazel test //...                 # the full hermetic suite (live/hardware tests are excluded)
bazel run //printer_debugger:main -- --check   # build the app and exit (smoke check)
```

Live/hardware tests are tagged `manual` + `requires-network` and excluded from the default run. To
run the read-only printer integration test against a real machine:

```bash
PD_LIVE_PRINTER=http://<host>:7125 \
  bazel test //printer_debugger/printer:live_test --test_tag_filters= --test_output=all
```

## Container image (rules_oci)

The image layers the app binary — which bundles its own hermetic Python interpreter and all
dependencies — onto `debian:12-slim` (pinned by digest).

```bash
bazel build //:image          # build the OCI image
bazel run //:load             # load it into the local Docker daemon as printer-debugger:latest
docker run --rm -p 8080:8080 -v /srv/printer-debugger:/data printer-debugger:latest
```

The container prints every reachable URL on startup (never localhost). Configuration is via the
environment:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PD_AUTH_MODE` | `local` | `local` (no auth, LAN trust) or `exposed` (OIDC allowlist) |
| `PD_ALLOWED_SUBJECTS` | — | Comma-separated OIDC subjects, required in exposed mode |
| `PD_ALLOWED_ORIGIN` | — | Allowed origin for the CSRF check, required in exposed mode |
| `PD_PORT` | `8080` | Listen port |
| `PD_DATA_DIR` | `/data` | Where the SQLite database and artifacts live (mount a volume) |

Secrets (the model API key, the OIDC client secret) come from the environment only, never the
image.

## GCP

Per the architecture, GCP runs this container on a **Compute Engine VM with a persistent disk**
(not Cloud Run — a request-scoped autoscaler is the wrong shape for long SSE streams, blocked
approval waits, the in-memory agent client, and SQLite's single-writer discipline). Mount the
persistent disk at `PD_DATA_DIR`.
