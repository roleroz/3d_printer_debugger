# Decisions

A log of decisions made about this project. Newest section last. Each entry records what was
decided and why, so later work does not re-litigate settled questions.

All documentation for this repository lives under `docs/`, including this file.

## 2026-07-20 — Initial scoping decisions

### Documentation layout

All project documentation — spec, architecture, per-module design docs, and this decisions
log — lives under `docs/`. Nothing documentation-related is placed at the repository root.

### Development workflow

Work proceeds in strictly sequential, individually approved stages: spec → architecture doc →
per-module design docs → implementation. No stage begins before the previous one is approved.

### Printer access from a cloud deployment

**Decision:** Live printer access is local-only for the MVP. The system talks to printers only
when it runs on the same LAN as them.

**Why:** The printers sit behind NAT on a home network. Reaching them from a public cloud
requires either an outbound relay agent or user-managed VPN/port-forwarding, both of which add
significant scope. A cloud deployment still works against uploaded files, photos, and stored
printer/config snapshots. An outbound relay agent is recorded as future work.

### Printer control

**Decision:** The system may both read printer state and send commands, but every write action
requires explicit user confirmation in the UI before it is executed.

**Why:** Real guided calibration requires running macros and G-code. Unattended writes to a
machine with heaters and moving parts are unacceptable for a system whose actions originate
from a language model.

### Voice input

**Decision:** Voice is captured in the browser and transcribed server-side for the MVP.

**Why:** The Web Speech API is inconsistent across mobile browsers, notably on iOS Safari,
which is a primary target. Browser-side recognition with a server fallback is future work.

### Authentication

**Decision:** The system runs in one of two explicitly configured modes.

In **local mode** — reachable only from the user's own network — there is no authentication at
all. No login, no identity provider, no dependency on anything outside the LAN.

In **exposed mode** — reachable from the internet, which includes every cloud deployment —
access is gated by OIDC against an external identity provider. Multiple identities may be on the
allowlist; all of them map to one single system user and therefore share all printers, sessions,
and data.

The mode is never inferred. If it is unset, or if exposed mode is configured without an identity
provider, the system refuses to start rather than serving unauthenticated.

**Why:** On a home network the LAN is already the trust boundary — the same assumption the
system makes for Moonraker — and forcing a login to reach a tool on your own network is friction
with no security benefit. Once the system is on the internet that reasoning disappears entirely,
so the two cases get different rules. OIDC for the exposed case is provider-agnostic, involves
no password handling, and extends cleanly to real multi-user support later. Genuine per-user
isolation is future work.

The system cannot tell whether it is reachable from the internet, which is why the mode must be
stated rather than detected, and why a missing setting is a startup failure instead of a
default.

### Printer definitions

**Decision:** Printer definitions are ingested, not authored. The system reads an externally
maintained markdown knowledge-base document in the shape of `~/git/ai_agent/3d_printers.md`,
plus the Klipper configuration at the paths that document references, or fetched from the
printer over Moonraker.

**Why:** That document already exists and is maintained by hand as the user's source of truth.
Duplicating it inside the application would create two competing records.

The file belongs to the user in this version — the system reads it and never writes to it.
Having the system take ownership of the file, so it can create and edit printer definitions and
write updates such as calibration status back into the document itself, is future work.

### Filament profiles

**Decision:** Filament settings are read out of the uploaded slicer project. The system does
not own filament profile objects and does not emit slicer-importable files. Recommendations
are delivered as text for the user to apply in their slicer.

**Why:** Keeps the MVP focused on diagnosis and calibration rather than profile management.
System-owned profiles with slicer export is future work.

### Calibration procedures

**Decision:** A small catalog of structured procedures, with freeform agent reasoning as the
fallback for anything outside it. Catalog: input shaper, PID tuning (bed and hotend), first
layer / Z-offset, pressure advance / flow ratio, temperature tuning, and stringing /
retraction tuning.

**Why:** The common procedures need to be reproducible and safe; everything else benefits more
from the model's flexibility than from a rigid workflow.

### Session media

**Decision:** Sessions accept still photos (phone camera or upload) and on-demand webcam stills
pulled from the printer when running locally. Video and timelapse are out of scope.

**Why:** Stills cover defect diagnosis at a fraction of the processing and token cost.

### Session lifecycle

**Decision:** Sessions can be created, auto-named, renamed, listed, resumed, and closed.
Deleting a session is not in the MVP.

**Why:** Deletion requires deciding how to purge associated artifacts and is not needed to
validate the system. Recorded as future work.

### Slicer support

**Decision:** OrcaSlicer only for the MVP — its `.3mf` project format and the G-code it emits.

**Why:** It is the only slicer in use here. Other slicers are future work.

### Moonraker authentication

**Decision:** Not supported in this version. The system assumes it can reach Moonraker without
authentication from the local network.

**Why:** That is the normal configuration for a printer on a home network, and the MVP only
talks to printers from the same LAN. A printer requiring authentication is reported as
unreachable rather than partially supported. Moonraker authentication is future work, and
becomes important alongside the relay agent, since a printer reachable from a remote server is
no longer protected by sitting on a trusted network.

### Printer firmware support

**Decision:** Klipper (via Moonraker) only for the MVP.

**Why:** Both target printers run stock Klipper. Other firmware is future work.

### Large file handling

**Decision:** `.3mf` and `.gcode` files are never passed to the model in full. An MCP server
exposes tools that extract only the specific parts the model asks for, with bounded response
sizes.

**Why:** These files routinely exceed any usable context window, and most of their content is
irrelevant to a given question.

## 2026-07-20 — Resolving the specification's open questions

Each entry below closes a question that was recorded as open in the first draft of the spec.

### Knowledge-base document parsing

**Decision:** No schema. Ingestion splits the document into per-printer sections and uses the
model to extract the three required values from each. Extraction is cached until the document
changes, and each section's raw text is retained for the model to reason from.

**Why:** The document is hand-maintained prose whose value is its detail and its freedom of
form. A parser that imposes field names would make the user maintain the file to the parser's
rules, which inverts the relationship — the system adapts to the document, not the reverse.

### Missing values during ingestion

**Decision:** Tell the user precisely what is missing from which section and ask them to correct
the document. If they decline, that printer remains usable in a degraded form, with its
unavailable capabilities stated. One bad section never blocks other printers or fails the
ingest.

**Why:** The user maintains the document, so the correct first move is to ask them to fix it.
Refusing to work until they do would be obstructive when the missing field may not matter for
what they are trying to do right now.

### Configuration precedence

**Decision:** Live Moonraker config (including `SAVE_CONFIG`) wins when the printer is
reachable; the stored snapshot of local files wins when it is not; the knowledge-base document
is never authoritative for a value. Every reported value carries its source and read time.
Discrepancies are detected and recorded at ingest but raised only when the value in question is
relevant, with a full audit available on request.

**Why:** Only the machine reflects what is actually running. Config files record intent, and
Klipper routinely supersedes them — a commented-out PID alongside a different `SAVE_CONFIG`
value is the normal case here. Announcing every discrepancy on every run would be noise, since
both printers have known stale comments.

### Upload size limits

**Decision:** 500MB for G-code and 100MB for `.3mf` by default, both configurable. Oversized
files are rejected from the declared size before the transfer completes.

**Why:** A dense 350mm plate at a fine layer height approaches 300MB, so these leave headroom.
The limit catches mistakes — wrong file, corrupt upload — rather than rationing normal use.
Failing at the end of a long upload over a phone connection would waste the user's time.

### Locating a defect in the G-code

**Decision:** Four complementary routes, usable in combination: a measured height converted to a
layer; a stated layer number; conversational narrowing from landmarks the user can describe; and
matching the user's photo against the plate image to identify which object and region. No G-code
renderer in this version.

**Why:** The user is holding a part, not a coordinate, and no single route covers every case.
The plate match supplies XY, the height or layer supplies Z, and the conversation resolves what
is left. A renderer would be the most intuitive route and by far the most expensive to build.

### Session-to-printer binding

**Decision:** A session concerns exactly one printer, detected from the uploaded project's
printer preset and corroborated by nozzle diameter and build volume. A confident match binds
silently; ambiguity, no match, or a session with no project file prompts the user. The binding
can be changed later, and the change is recorded. A disagreement between the project's printer
and the session's printer is raised as a diagnostic finding.

**Why:** The project file already knows which printer it was sliced for, so asking the user to
restate it is redundant. But it records intent, not fact — slicing for one machine and printing
on another is a real mistake with a characteristic signature, and it explains a whole class of
defects, so the mismatch is worth surfacing rather than reconciling away.

### Long-running procedures

**Decision:** Procedure runs live in the conversation. There is no run object, no tracked state,
no printer polling, and no notifications. A procedure must leave the transcript unambiguous when
read hours later.

**Why:** The conversation is already durable and complete, and a separate state machine would be
a second record to keep in sync with a physical process the system cannot observe. Tracked runs
with notifications are recorded as future work if this proves too loose.

### Voice message limits

**Decision:** Two minutes by default, configurable. On reaching the cap, recording stops and the
audio captured so far is transcribed and submitted, with the user told it was cut short.

**Why:** Describing a problem out loud takes well under two minutes; the cap bounds upload size
and transcription latency on a phone connection. Discarding speech the user has already given is
never acceptable.

### Cost visibility

**Decision:** Token usage and estimated cost are tracked per session and viewable on request,
not displayed continuously. No spending limit is enforced.

**Why:** These sessions are image-heavy, so an unusually expensive one should be diagnosable
afterwards — but a running total is noise on a phone screen mid-diagnosis. A hard cap on a
single-user system running against the user's own API key would mainly serve to stop a session
working mid-problem.
