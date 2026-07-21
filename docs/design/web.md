# Web layer and UI — module design

This module is everything the user touches: the pages, the camera and microphone, uploads, the
live stream of assistant output, the approval interface, and the emergency stop.

Requirements: [spec.md §6](../spec.md#6-interface-requirements),
[§5.3](../spec.md#53-conversation), [§5.4](../spec.md#54-file-ingestion), and
[§11](../spec.md#11-security-and-privacy-requirements). Component:
[architecture.md §3.1](../architecture.md#31-web-layer).

## 1. Scope

**In scope:** routes, server-rendered pages, the client-side JavaScript, SSE fan-out, upload
handling, audio capture and transcription handoff, the approval interface, the emergency stop, and
authentication.

**Out of scope:** visual design. This document specifies behaviour and structure; how it looks is
decided while building it.

## 2. Design decisions

### 2.1 Server-rendered, with JavaScript only where the browser requires it

Pages are HTML from the server. Hand-written JavaScript covers four things and nothing else:
camera capture, audio recording, upload progress, and consuming the SSE stream. No bundler, no
framework, no build step, one deployable artifact.

The estimate that justifies this is small: these four capabilities are a few hundred lines of
DOM and browser-API code. A framework would be a larger commitment than the problem.

### 2.2 SSE, not WebSockets

Assistant output is bulk-down, occasional-small-up. SSE matches that exactly: unidirectional,
plain HTTP, and it reconnects natively without reconnection logic. User messages and approvals go
up as ordinary POSTs.

### 2.3 Fan-out to every viewer of a session

A session may be open on several devices at once, and all of them see the same live stream. This
is the actual working pattern: photograph the part on the phone while reading the analysis on the
desktop.

Any viewer may approve a printer action, and the deciding identity is recorded
([orchestration.md §6](orchestration.md#6-the-approval-gate)). The cost is a per-session subscriber
list instead of a single stream, which is small.

## 3. Routes

| Route | Purpose |
| --- | --- |
| `GET /` | Session list |
| `POST /sessions` | Create a session, optionally with a project upload |
| `GET /sessions/{id}` | Session view: conversation, artifacts, printer state |
| `POST /sessions/{id}/messages` | Send text or an image-bearing message |
| `POST /sessions/{id}/audio` | Upload audio; transcribe; submit as a message |
| `POST /sessions/{id}/files` | Upload a `.3mf` or G-code |
| `GET /sessions/{id}/stream` | SSE: assistant output, tool activity, proposals, status |
| `POST /sessions/{id}/name` | Rename |
| `POST /sessions/{id}/close` | Close |
| `POST /sessions/{id}/printer` | Bind or rebind the printer |
| `POST /approvals/{id}` | Approve or reject a pending proposal |
| `GET /artifacts/{id}` | Serve an artifact — photo, thumbnail, still |
| `POST /printers/{id}/estop` | **Emergency stop** |
| `GET /printers` | Printer list with reachability |
| `GET /healthz` | Store and printer reachability, active mode |

### 3.1 The emergency stop route

Separate from everything else, and deliberately minimal. It calls
[printer_access.md §2.3](printer_access.md#23-emergency-stop-is-m112-and-bypasses-everything)
directly — no agent, no gate, no queue. It must work when the model is mid-turn, when a proposal
is pending, and when the session's stream has dropped.

In the UI it is present on every page while a printer is connected, visually distinct, and
confirms only that the user meant it — never what kind of stop it is, because there is only one.

## 4. Pages

### 4.1 Session list

Name, printer, last active, state. Enough to recognise a session
([spec.md §5.2](../spec.md#52-session-lifecycle)). Sorted by last active.

### 4.2 Session view

The working surface, and it is designed for a phone held in one hand in front of a printer:

- **Conversation**, streaming, with tool activity shown as it happens so a long turn is visibly
  working rather than apparently hung.
- **Composer**: text, a camera button, and a microphone button. The camera opens the device camera
  directly — `capture` on a file input — so a photo is two taps and never involves a file manager.
- **Attachments** visible inline; photos render, files show name and size.
- **Printer strip**: connection state, temperatures, print status when live, riding the session's
  SSE stream — the printer client already subscribes to these server-side
  ([printer_access.md §10](printer_access.md#10-open-questions)), so the strip consumes that stream
  rather than the browser polling. Shown as stale when the connection has dropped rather than
  silently frozen.
- **Pending proposal**, when one exists, as an unmissable block ([§5](#5-the-approval-interface)).
- **Emergency stop**, always reachable.

## 5. The approval interface

When a proposal arrives on the stream, the session view presents:

- **The exact command**, verbatim, in monospace. Not a paraphrase — the audit record stores what
  the user was shown, so what is shown must be the command itself
  ([design/store.md §4.8](store.md#48-approval)).
- **What it will do**, in the model's words, clearly separated from the command itself.
- **Danger flags**, prominently, when the static classifier raised any
  ([printer_access.md §6](printer_access.md#6-danger-classification)).
- **Approve and Reject**, requiring a deliberate action. Approve is not the default focus and is
  not reachable by pressing Enter — a stray keypress must not run a command.
- **Time remaining** on the five-minute window, so a user who steps away understands what happened
  when it expires.

Once decided, the block resolves on **every** viewer, not just the one that decided.

## 6. Uploads

- **Streamed to artifact storage**, never buffered whole in memory. A G-code file at the size limit
  would otherwise be hundreds of megabytes of resident memory.
- **The declared size is checked before the body is read**, so an oversized file is rejected
  immediately rather than after a long mobile upload
  ([spec.md §5.4](../spec.md#54-file-ingestion)).
- **Progress is shown**, because these files are large and phone uplinks are slow.
- **Type is validated** after upload; a mismatch is reported plainly and no artifact row is kept.

## 7. Audio and transcription

Recorded in the browser, uploaded, transcribed server-side
([architecture.md §3.9](../architecture.md#39-transcription)), and submitted as a message.

- **Capped at two minutes by default**, configurable
  ([spec.md §5.3](../spec.md#53-conversation)).
- **On reaching the cap, recording stops and what was captured is submitted**, with the user told
  it was cut short. Speech already given is never discarded.
- **The transcript is shown as the user's message**, so a mis-transcription is visible and
  correctable rather than silently becoming what the model reasons about.
- **On transcription failure**, the audio is retained and the user is told, with text entry
  unaffected.

## 8. Authentication

Follows the two modes from [spec.md §11](../spec.md#11-security-and-privacy-requirements):

- **Local mode**: no authentication, no identity provider, no login step. The trust boundary is
  the LAN, deliberately.
- **Exposed mode**: OIDC, with an allowlist of subjects all mapping to one system user. An
  unlisted subject is rejected before any handler runs. TLS required.
- **The active mode is visible in the UI**, so it is never ambiguous whether this instance is
  authenticating anyone.
- **Startup refuses** on an unset or contradictory mode.

### 8.1 Cross-site request protection

Authentication establishes who a request is for, not where it came from. With a cookie-backed OIDC
session, a page the user opens elsewhere could fire a cross-site POST that rides their session
cookie — a CSRF. **Every state-changing request therefore carries a CSRF defense**: session cookies
are `SameSite`, which already withholds the cookie from cross-site POSTs, and mutating routes
additionally verify request origin — an `Origin`/`Referer` check or a per-session token — so a
forged cross-site request is refused before its handler runs.

**`POST /approvals/{id}` is why this matters most.** A forged approval would run a printer command
with no human deciding, defeating the approval gate the whole security model rests on
([orchestration.md §6](orchestration.md#6-the-approval-gate)). The emergency stop and the other
mutating routes are protected by the same mechanism. Local mode has no cookie and no login — the LAN
is the trust boundary — so this is an exposed-mode property.

## 9. Startup output

On binding the listener, the process prints **every externally reachable URL**
([spec.md §9](../spec.md#9-deployment-requirements)). Loopback is either omitted or labelled
local-only. Where the machine has several addresses, all are printed rather than one being guessed
at — the phone's route to the host is not something the host can determine.

If configuration binds to loopback only, that is stated plainly at startup, because a printed URL
that looks reachable and is refused is worse than an accurate error.

## 10. Failure handling

| Failure | Behaviour |
| --- | --- |
| SSE connection drops | Client reconnects; missed output is fetched from the store on reconnect |
| Session open on two devices, one drops | The other is unaffected |
| Upload fails mid-transfer | Reported; no artifact row; partial blob removed |
| Oversized upload | Rejected before the body is read, naming size and limit |
| Transcription fails | Reported; audio retained; text entry unaffected |
| Printer unreachable | Printer strip shows offline; live features disabled with a reason |
| Model turn fails | Error shown in the conversation; the session remains usable |
| Approval arrives for an expired proposal | Rejected with an explanation of what happened |
| Cross-site POST to a mutating route | Refused before the handler runs; no state change |

**Reconnect must not lose output.** The stream carries a position, and a reconnecting client asks
for everything after the last it received, served from the store. Otherwise a phone that
backgrounds for thirty seconds loses part of an answer.

## 11. Testing

- **Route tests** cover every route's success and failure paths, including authentication in both
  modes.
- **Fan-out is tested with several subscribers**: all receive output, a decision from one resolves
  the proposal on all, one disconnecting does not affect the others.
- **Reconnect-without-loss is tested** by dropping a subscriber mid-stream and asserting the
  resumed stream contains exactly what was missed.
- **Upload tests use a real oversized file** to prove rejection happens before the body is read,
  rather than asserting on a mocked size.
- **The approval interface is tested for its refusal properties**: Enter does not approve, and an
  expired proposal cannot be approved.
- **The CSRF defense is tested**: a cross-site POST to a mutating route — the approval route in
  particular — is refused, while a same-origin request with a valid token succeeds.
- **The emergency stop is tested while a turn is in flight and while a proposal is pending**,
  asserting it is not queued behind either.
- **Browser-side JavaScript is tested** for the four capabilities it owns; camera and microphone
  are exercised against the browser's own test facilities rather than mocked away.

## 12. Open questions

1. **How the plate image is presented for photo matching.** The user needs to indicate which
   object on the plate a defect is on ([spec.md §5.7](../spec.md#57-diagnosis)). The data is
   settled: `get_plate_layout` gives object footprints in plate coordinates
   ([file_indexing.md §5.1](file_indexing.md#51-project)), so a tap maps to a real object identity
   rather than guessing on pixels, and a labelled object list is the fallback when footprints are
   too small or overlap to tap reliably (the overlap case in
   [file_indexing.md §8](file_indexing.md#8-open-questions)). Open is only which interaction wins
   on a phone, which needs trying.
2. **Offline behaviour on a phone.** Correctness is already handled: SSE reconnects natively and
   reconnect loses no output — the stream carries a position and refetches what was missed
   ([§10](#10-failure-handling)) — and the printer strip already shows stale on a dropped
   connection ([§4.2](#42-session-view)). Open is only the presentation during a drop: how long to
   wait before showing a "reconnecting" indicator rather than flashing one on a brief blip, and
   whether a message typed while offline is queued or blocked. A judgement best made after using it
   in a workshop with poor signal.

## 13. Implementation tasks

- [ ] **T3.1** Routing skeleton, error handling, and the health endpoint.
- [ ] **T8.1** Authentication: both modes, allowlist, startup refusal on bad configuration.
- [ ] **T8.2** CSRF defense on mutating routes: `SameSite` cookies plus an origin or per-session
      token check, refusing forged cross-site requests.
- [ ] **T9.1** Startup URL printing, all addresses, loopback labelled.
- [ ] **T4.1** Session list page.
- [ ] **T4.2** Session view: conversation rendering, attachments, printer strip.
- [ ] **T3.2** SSE endpoint with per-session fan-out and position-based reconnect.
- [ ] **T6.1** Streamed uploads with pre-body size check, progress, and type validation.
- [ ] **T4.3** Composer JavaScript: camera capture, upload progress.
- [ ] **T7.1** Audio recording, cap-and-submit behaviour, transcription handoff.
- [ ] **T5.1** Approval interface, including refusal properties and multi-viewer resolution.
- [ ] **T3.3** Emergency stop route and its always-present control.
- [ ] **T4.4** Artifact serving.
