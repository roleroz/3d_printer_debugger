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

## 2026-07-20 — Architecture decisions

### Agent layer

**Decision:** The Claude Agent SDK (`claude-agent-sdk`, Python), used through its persistent
client, one client per active session. All of its built-in tools — shell, file read/write/edit,
search, web access — are disabled.

**Why:** The SDK supplies the agent loop, context management, session persistence and resume,
streaming, image input, MCP hosting, and a permission system, which is most of what this system
needs. Its built-in tools serve none of the requirements here and would put shell and filesystem
access on a host sitting next to machines with 60W heaters, so they are disabled through four
independent mechanisms: a tool allowlist naming only our tools, an explicit disallow of the
built-in set, a permission mode that denies anything unlisted, and the approval callback.

The alternative considered was the Claude API tool runner over hand-defined tools, which has no
built-in tool surface to disable. The Agent SDK was chosen for the session and context machinery
it brings.

### Approval gate

**Decision:** The confirmation requirement for printer writes is implemented as the SDK's
permission callback, which blocks the turn until the user decides in the browser.

**Why:** The gate sits below the model rather than beside it. No prompt content can route around
a callback the SDK invokes before executing a tool, which is the property this requirement needs.
Dangerous-action classification is a static check on the pending command in the same callback,
independent of what the model believes about it.

### MCP servers in-process

**Decision:** The three capability servers (`project`, `gcode`, `printer`) run in-process via the
SDK's in-process MCP mechanism, not as stdio subprocesses.

**Why:** They need the same artifact store, printer connections, and parsed-file indexes the rest
of the process holds. A subprocess boundary would mean serializing all of it across a pipe for no
isolation benefit — the isolation that matters is the capability surface, which the allowlist
already provides.

### Web interface

**Decision:** Server-rendered HTML with hand-written JavaScript only where browser APIs require
it (camera capture, audio recording, upload progress, SSE). Assistant output streams over
Server-Sent Events.

**Why:** No build step, no bundler, one deployable artifact. SSE is unidirectional and reconnects
natively, which matches the traffic shape — bulk output down, occasional small messages up.

### Structured storage

**Decision:** SQLite in WAL mode, accessed through a single writer. Artifacts sit behind a
separate blob interface: local filesystem locally, object storage in a cloud deployment.

**Why:** One file, no service to run, identical locally and in a container, trivially backed up.
A single user's concurrency does not justify a database service, and the single-writer discipline
avoids a class of write-lock bugs entirely. The artifact interface is the seam that keeps the
system provider-agnostic.

### Transcription

**Decision:** Local Whisper running in the container, weights baked into the image and pinned.

**Why:** Speech-to-text is not an Anthropic API, so this is a genuinely separate dependency. A
hosted provider would add a second AI vendor, a second API key, send workshop audio to another
provider, and break the requirement that a local deployment need no cloud account. The cost is
CPU and image size.

### Packaging

**Decision:** A single container image containing the application, its MCP servers, and the
Whisper weights. `docker compose` locally, the same image on Cloud Run for GCP.

**Why:** The same artifact runs everywhere, satisfying the requirement that local and cloud
deployments be the same build. Only the artifact backend and the storage volume are
provider-shaped, and both sit behind interfaces.

### Web access for the agent

**Decision:** Web search and web fetch are enabled, unrestricted. The Agent SDK's host-touching
built-ins — shell, file read/write/edit, filesystem search — stay disabled. Web-derived claims
must be cited and are explicitly ranked below first-hand evidence from the artifacts,
configuration, and live printer state.

**Why:** An earlier version of the architecture disabled every built-in tool on a single "deny by
default" argument. That conflated two different risk classes. Shell and filesystem access let the
model act on a host sitting on the printer network; web search and fetch read remote content and
never touch the host. Denying the web tools bought no host safety and cost real capability — the
firmware error strings, hardware quirks, and community fixes this domain runs on are not all in
the model's training data, and the hardware moves faster than any training cutoff.

The residual risk is prompt injection from a fetched page. That is contained structurally rather
than by prompting: the web tools cannot reach the printer, and every printer write stops at the
approval gate with the exact command displayed to a human. The citation-and-ranking rule addresses
the separate failure of a confident forum post outweighing what the G-code plainly shows.

## 2026-07-20 — Store module decisions

### GCP deployment shape

**Decision:** The GCP deployment runs the container on a Compute Engine VM with a persistent disk,
not on Cloud Run. This resolves the question the architecture deferred to the store's design.

**Why:** Cloud Run is a request-scoped autoscaler, and four of its properties are wrong for this
system: its request timeout caps SSE streams and blocked approval waits, scale-to-zero discards
the in-memory agent client a live session depends on, more than one instance breaks the
single-writer discipline SQLite depends on, and it has no persistent local disk.

The alternatives cost more than they save. Postgres behind the store interface means every query
must work on two engines and both must be tested, for a workload SQLite handles without effort.
SQLite on a network filesystem is worse — WAL is unreliable over NFS and locking is the classic
corruption path, so the failure mode is a damaged database rather than an error. A small always-on
VM removes all four problems and keeps local and cloud running the same code against the same
engine.

### Migrations

**Decision:** Hand-written numbered SQL files, applied in order inside a transaction, with the
applied version recorded in the database. Forward-only; no framework.

**Why:** The schema is small enough that the whole history stays readable, and the migration that
matters is the one someone can read in full before running it against data they cannot regenerate.
Down-migrations are omitted deliberately: with a single-user database and a backup taken before
upgrade, restore is the rollback, and it is the one that actually works.

### Database access

**Decision:** Hand-written SQL through the standard-library driver, with explicit typed functions
mapping rows to frozen dataclasses. No ORM, no query builder.

**Why:** The single-writer discipline is only verifiable if every statement is visible where it
runs. An ORM that decides when to flush puts a layer between the code and the thing being
disciplined. The cost — writing the mapping by hand — is bounded and mechanical at this schema
size.

### No general query method on the store

**Decision:** `StructuredStore` exposes named methods per access pattern and no general query
escape hatch.

**Why:** Every access pattern stays visible in one file, which is what makes it possible to know
the query set and index for it. An escape hatch would let query patterns accumulate anywhere in
the codebase, unindexed and unreviewed.

### Constraints carry the domain rules

**Decision:** Rules that can be expressed as schema constraints are expressed that way — notably
that a filament calibration must carry a filament and a machine calibration must not, and that a
tool call can have at most one approval.

**Why:** A rule enforced by the database cannot be forgotten by a caller. These two in particular
are load-bearing: the first is the scoping rule from the calibration spec, the second is what
makes double-approval of one printer command impossible rather than merely unlikely.

## 2026-07-20 — Remaining module design decisions

### Emergency stop semantics

**Decision:** The emergency stop issues Klipper's `M112` — heaters off, motion halted, MCU into
shutdown, recovery requiring a firmware restart. It bypasses the agent, the approval gate, and any
command queue.

**Why:** It matches the stop button in Mainsail. Someone reaching for a stop button in this system
gets what they would get reaching for it in the interface they already use, with no moment of
wondering which kind of stop this one was. The cost — a lost print and a firmware restart — is the
correct trade when the button is pressed. A graceful cancel is slower to take effect and does not
stop a runaway move already in flight.

### Approval timeout

**Decision:** A pending printer-write approval times out after five minutes and is recorded as a
denial with cause.

**Why:** Long enough to walk to the printer and look at something before deciding; short enough
that a forgotten proposal does not sit armed. A user who walks away never leaves a command waiting
to fire. The failure direction is always denial — a crash with a proposal pending resolves to
timed-out, never to approved.

### G-code indexing is asynchronous

**Decision:** Indexing runs in the background. The session is usable immediately; the header and
configuration block are available within seconds, and layer, coordinate, and state queries become
available when the pass completes. Tools report "still indexing" with progress rather than failing.

**Why:** Blocking until indexed means staring at a progress bar before asking the first question,
when the first question is very often about slicer settings and answerable from the `.3mf` alone.
Lazy indexing on first query moves the same wait to an unexplained pause mid-conversation.

### Procedures are data

**Decision:** Each calibration procedure is a structured document loaded at startup, not a code
module.

**Why:** A procedure is knowledge, not behaviour — purpose, preconditions, steps, commands, and
how to read the result are all prose and values. As code it would need a release to fix a wrong
temperature range, and the interesting content would sit inside functions where it cannot be read
whole. Data also lets the model read the entire procedure and adapt it, which is the right shape
for a step that depends on what a photo shows.

### Knowledge-base extraction model

**Decision:** A small fast model performs the per-section extraction, not the agent's model.

**Why:** Extracting three values from a section of prose is mechanical, well within a small
model's ability, and runs on every document change. Results are cached by content hash, so an
unchanged document costs nothing and editing one printer's notes costs one call.

### Webcam stills are all retained

**Decision:** Every webcam still the system captures is stored as an artifact and kept.

**Why:** A still is tens to hundreds of kilobytes against G-code files in the hundreds of
megabytes — noise in the storage budget. Keeping all of them means a session's visual record is
complete when reviewed months later, and avoids a rule about what counts as "referenced" that
would risk discarding the frame that turns out to matter.

### Multiple simultaneous viewers per session

**Decision:** A session may be open on several devices at once. All viewers receive the same live
stream, and any of them may approve a printer action; the deciding identity is recorded.

**Why:** It is the actual working pattern — photograph the part on the phone while reading the
analysis on the desktop. Single-viewer would mean picking up the phone kills the desktop view
mid-answer. The cost is a per-session subscriber list instead of a single stream.

### Persistent printer connections

**Decision:** A WebSocket is held to each reachable printer for the process lifetime, reconnecting
with backoff.

**Why:** Live state is already current when a session asks rather than paying connection latency
on every first query; reachability is known before a session asks, so the UI can say a printer is
offline instead of discovering it mid-conversation; and print progress and failures are
observable, which is what lets a procedure waiting on a physical print see the print finish.

## 2026-07-21 — Knowledge-base ingestion refinements

These resolve gaps found while reviewing `kb_ingestion.md` against `store.md` and the example
document.

### A printer section with no name is rejected, not stored

**Decision:** When a section reads as a printer but no name can be extracted, nothing is stored.
The user is told which section it was, by position and heading, and asked to add a name.

**Why:** The name is the identity and the matching key, unique in the schema; a nameless printer
cannot be stored or matched on the next ingest. The name is not guessed from the heading, because
the extraction step exists precisely because headings are not reliably the name. Address and
config-path absence still degrade rather than reject, since those gate capabilities, not identity.

### `degraded` describes the document, not the printer's ability to work

**Decision:** A printer missing its address or config path is `degraded`, but that means only that
the document did not supply everything. A printer with an address but no local config path is
degraded yet fully functional when reachable, because its configuration comes live from Moonraker.
`missing` records which value is absent and its capability impact.

**Why:** The two values gate different things — address gates all live features, config path gates
only the offline snapshot — and the earlier framing implied a missing config path made a printer
non-functional, which is wrong when it is reachable.

### Non-printer section classification is cached in a `section_cache` table

**Decision:** A `section_cache` table keyed by section hash holds the extraction result for every
section, printer or not.

**Why:** `printer.kb_content_hash` caches only printer sections; a section classified as not a
printer had no row to hold its hash, so it would be re-sent to the model on every document change —
undercutting the cache. Keying on the section hash regardless of outcome makes "an unchanged
section costs nothing" true for all sections.

### A printer removed from the document is flagged, not deleted

**Decision:** When a printer's section is removed, the row is kept — its sessions still reference
it — and an `absent_since` timestamp is set, shown in listings as no longer present. Reappearance
of the name clears it. Row deletion is not in this version.

**Why:** Upsert-never-replace left a removed printer silently indistinguishable from a current one.
Flagging it makes the staleness visible while preserving session history, and distinguishes a
removed printer (gone from the source of truth) from a merely offline one (reachable again later).

## 2026-07-21 — Configuration mechanism

**Decision:** Non-secret configuration is supplied in a single YAML file whose path comes from one
bootstrap environment variable with a default; secrets come only from the environment. Defaults
live in code, the file overrides them, and everything is validated at startup, with the process
refusing to serve on anything missing or contradictory. `architecture.md §9.1` is the single
authoritative description; every "configurable" value in the design documents means a key there.

**Why:** The docs called many values configurable and stated only that configuration "comes from
the environment," which read fine for secrets but badly for the rest. Configuration has three
natures — secrets, deployment wiring, and tunables — and they want different homes. Secrets must
never be in the repository ([spec.md §11](spec.md#11-security-and-privacy-requirements)), so the
environment (or a secret manager populating it) is correct for them. The wiring and tunables
include nested structure — the artifact backend and its parameters, the OIDC block, upload limits
— that reads naturally as a file and clumsily as flat prefixed environment variables, and a
readable file is what a local deployment wants to edit. Defaults in code keep a local run working
with a minimal file or none.

A layered "code < file < environment" scheme was considered and rejected as more machinery than a
single-user system needs, and because letting the environment override arbitrary file keys muddies
the provenance the system otherwise keeps clear. Pure environment variables were rejected because
the nested settings and the dozen-plus tunables flatten badly.

This also resolves the knowledge-base ingestion open question about how the user's Klipper config
paths (`~/...`, meaningless in a container) are located: the deployment mounts the config tree at
a known location and names it as the config-file base, a wiring setting, and the ingester resolves
paths against it rather than expanding `~`.

## 2026-07-21 — Knowledge-base ingestion open questions resolved

### Poll interval

**Decision:** The document watcher polls on a 60-second default, configurable. A manual "update
now" control that forces an immediate re-ingest is future work
([spec.md §14](spec.md#14-future-work)).

**Why:** A hand-edited document changes rarely and is trivial to hash, so a minute picks up an
edit quickly while keeping polling a non-cost. The residual lag after a deliberate edit is better
solved by an explicit trigger than by polling faster all the time.

### Renamed section handling

**Decision:** A rename stays remove-plus-add — the old printer is flagged `absent_since` with its
sessions intact, and the new name becomes a fresh printer. Sessions do not follow a rename.

**Why:** The document gives no reliable way to distinguish a rename from an unrelated
add-plus-remove in the same ingest. Guessing wrong would silently reattribute a session's history
to a different machine, which is worse than leaving old sessions on the old, now-absent printer,
where they remain correct. Detecting likely renames and asking the user to confirm was considered
and declined as machinery beyond what this version needs.

## 2026-07-21 — File indexing scope

### Single plate per project

**Decision:** The MVP treats an OrcaSlicer project as a single plate, using the first plate when a
project contains several. Indexing every plate and associating each with its own G-code is future
work ([spec.md §14](spec.md#14-future-work)).

**Why:** Multi-plate handling adds a plate dimension to the `.3mf` index, the tool surface, and
G-code association, for a case the common workflow does not need to start with. Deferring it keeps
the index and tools simple; the cost is that a session about a non-first plate in a multi-plate
project is not correctly served until the future work lands.

### Trust that the G-code matches the project

**Decision:** The MVP trusts that an uploaded G-code was sliced from the uploaded project; it does
not verify. Comparing the G-code's embedded configuration block against the project's settings and
flagging a mismatch is future work.

**Why:** Detection is genuinely useful — analysing a mismatched pair diagnoses the wrong artifacts,
the same failure class as the printer mismatch — but it is not needed to make the common case work,
and the comparison has real subtlety (which settings must match, and how closely). Deferring it
keeps the MVP focused; the risk is that a wrong-file upload is analysed silently until the future
work adds the check.

## 2026-07-21 — Intended geometry from the mesh

**Decision:** The `.3mf` mesh is retained (in the stored artifact) and exposed to the model through
two `project` tools: `get_object_render` (the intended geometry rendered from a viewpoint, as an
image artifact) and `get_object_dimensions` (bounding box, height, footprint, volume, overhang
extents). Rendering is done server-side by a headless mesh renderer, a pinned dependency. Slicing
the mesh for a per-layer intended cross-section is future work
([spec.md §14](spec.md#14-future-work)).

**Why:** An earlier draft skipped the mesh as "too large," which discarded the single most
important thing for diagnosis: what the part was supposed to look like. The model compares intended
geometry against a photograph of what printed — a layer shift, a missing feature, a sagged overhang
is a departure from the model, invisible in settings and G-code. Dropping the mesh to keep the
index small optimised a secondary concern at the cost of a primary capability. The mesh is exposed
the same way every large artifact in this module is: through bounded, targeted views (rendered
images the vision model can compare, and numeric measurements), never fed in wholesale. Raw
triangle data is both huge and useless to a vision model, so rendering is the right surface and the
headless renderer it requires is justified.

## 2026-07-21 — State snapshots at layer starts

**Decision:** The G-code pass writes a full machine-state snapshot at the start of every layer, and
a `get_state_at` query replays from the snapshot at the start of the layer containing the point.
This replaces the earlier "snapshot at a tunable interval," and closes the checkpoint-interval open
question in [file_indexing.md](design/file_indexing.md).

**Why:** The layer boundary is the natural snapshot point. The layer table already carries a byte
offset there, so a snapshot costs one small record per layer — cheap, since a file holds thousands
of layers, not millions — and replay never crosses a layer boundary. State questions are themselves
usually anchored on a layer, so the snapshot sits exactly where a query begins its replay. Choosing
a byte- or time-based interval would have been a free parameter to tune with no better answer than
the boundary the rest of the index is already organised around.

## 2026-07-21 — Modified-from-preset diff deferred behind the preset library

**Decision:** Confirmed against a real OrcaSlicer 2.4.2 project
(`~/debugger/rack_cable_manager_2u.3mf`) that a `.3mf` does not record which settings were
overridden — there is no `different_settings_to_system` or `inherits` key — and that
`project_settings.config` holds only fully-resolved values. The presets are named
(`print_settings_id`, `filament_settings_id`, `printer_settings_id`) but not embedded; they live
in the user's OrcaSlicer preset library. The MVP therefore does not compute the modified-from-preset
set: it serves the resolved settings and the preset names, and `get_modified_settings` reports the
diff unavailable. Ingesting the preset library to compute the diff is future work
([spec.md §14](spec.md#14-future-work)). This closes the changed-settings-record open question in
[file_indexing.md](design/file_indexing.md).

**Why:** The earlier design assumed the set could be read from the project or computed against a
readily-available baseline. Inspecting a real file showed neither holds: the diff is not recorded,
and the baseline presets are external to the file. Computing the set needs the user's preset
library, an ingestion path the MVP does not yet have and which cannot work in a cloud deployment
where the library is absent. Rather than guess a baseline (bundled system presets would be wrong
for the user's custom Voron templates) or silently omit the capability, the module serves what the
file actually contains and reports the diff unavailable, keeping the promise that it never reasons
from data it does not have.

## 2026-07-21 — Emergency-stop recovery via Mainsail for now

**Decision:** After an emergency stop (`M112`), the printer sits in a Klipper shutdown that a
`FIRMWARE_RESTART` clears. The MVP does not offer a restart control; the user recovers from
Mainsail. Adding a one-tap firmware-restart button to this system's own UI is future work
([spec.md §14](spec.md#14-future-work)). This closes the emergency-stop-recovery open question in
[printer_access.md](design/printer_access.md).

**Why:** Issuing the stop and reporting it is this module's job; recovery is a UI affordance, not a
safety-critical path. Mainsail already offers firmware restart and the user runs it alongside this
system, so the capability is not missing — only the convenience of recovering without switching
tools. That convenience is worth building later but is not needed to make the MVP safe or usable,
and deferring it keeps the emergency-stop path minimal, which is exactly the path that most needs
to stay simple and verifiable.

## 2026-07-21 — Web fetch restricted to public addresses

**Decision:** The agent's web-fetch tool is restricted to public addresses; loopback, RFC-1918
private ranges, and link-local addresses are refused. Whether the Agent SDK enforces this or it
must be fronted by our own fetch proxy is an open question in
[orchestration.md](design/orchestration.md), confirmed at implementation.

**Why:** The security model rests on the printer being reachable only through a human-approved gate.
The host runs on the printer's LAN, so an unrestricted fetch could read the printer's Moonraker HTTP
API directly — around the three-tier discipline and the approval gate — on a URL that may have come
from injected web content, which is exactly the threat the gate exists to contain. Moonraker's
mutations happen to be POST while web fetch is GET, so writes are incidentally blocked, but that is
an accident of Moonraker's API, not a chosen safety property, and it does nothing against
information-disclosure SSRF or a GET-triggerable action elsewhere on the LAN. Refusing private
addresses makes "web tools never touch the host" into "never touch the host or its LAN," which is
what the argument actually needs.

## 2026-07-21 — CSRF defense on mutating web routes

**Decision:** In exposed mode, every state-changing request carries a CSRF defense: session cookies
are `SameSite`, and mutating routes additionally verify request origin (an `Origin`/`Referer` check
or a per-session token). Forged cross-site requests are refused before the handler runs. Local mode
has no cookie and no auth — the LAN is the trust boundary — so this is an exposed-mode property.

**Why:** OIDC authenticates who a request is for, not where it originated. With a cookie-backed
session, a page the user opens elsewhere can fire a cross-site POST that rides their session cookie.
The critical target is `POST /approvals/{id}`: a forged approval would run a printer command with no
human deciding, defeating the approval gate that the entire security model rests on. The emergency
stop and the other mutating routes are exposed the same way and protected by the same mechanism.
Authentication alone does not close this; the request-origin check is what does.

## 2026-07-21 — Printer strip rides the SSE stream, no browser polling

**Decision:** The session view's printer strip (connection state, temperatures, print status) is
updated over the session's existing SSE stream, not by the browser polling a status endpoint. The
printer client already holds a server-side subscription to exactly these continuously-observed
fields ([printer_access.md](design/printer_access.md)), so the strip consumes that stream. This
closes the poll-versus-subscribe open question in [web.md](design/web.md).

**Why:** The "poll versus subscribe" framing was really about the server-to-printer link, which is
already settled: the printer client subscribes to reachability, print progress, and live
temperatures and fetches everything else over HTTP. The strip shows precisely those subscribed
fields, and SSE fan-out to viewers already exists for assistant output. Pushing strip updates over
that same stream is strictly less machinery than a browser poll loop and is naturally fresh, so
there was nothing left to defer.

## 2026-07-21 — first_layer's mechanical Z-offset lives in saved config, not procedure_result

**Decision:** The `first_layer` procedure is scoped `printer_and_filament`, and its
`procedure_result` row (which the `CHECK` requires to carry a filament) records only the
filament-scoped first-layer values. The mechanical Z-offset it establishes — printer-scoped — is a
saved-config value (Klipper `[probe] z_offset`, set with `SAVE_CONFIG`) captured by the
configuration snapshot ([printer_access.md §4](design/printer_access.md)), and is not written to
`procedure_result`. This resolves the mixed-scope tension in
[procedures.md](design/procedures.md) §4.1.

**Why:** Storing the whole first_layer result in one filament-scoped row would tag the mechanical
Z-offset — a printer fact independent of material — with a filament, asserting it is
filament-specific when it is not, and duplicating it across every filament's row. The three-tier
model already has the right home: the Z-offset is a saved value that lives in the printer and is
snapshotted, so it needs no procedure_result row. The row then carries only genuinely
filament-scoped values, and the database `CHECK` requiring a filament is correct as written. This
also fits the system owning no filament profiles: per-filament squish is a slicer setting the user
applies, not a probe change.

## 2026-07-21 — All procedures loaded in full in the cached prefix

**Decision:** All six procedure documents are placed in full in the cached stable prefix of the
system prompt ([orchestration.md §4.1](design/orchestration.md#41-system-prompt-assembly)), not
loaded selectively on demand. This closes the how-much-to-load open question in
[procedures.md](design/procedures.md). Transforming each procedure into a skill the agent loads
only when relevant is future work ([spec.md §14](spec.md#14-future-work)).

**Why:** The prompt-assembly order already places the catalog in the cross-session cache precisely
so it is paid for once. Six calibration documents are kilobytes; loading only the relevant one
would move the catalog out of the cached prefix — forfeiting that cache — to save an amount that
does not matter at this size. The on-demand-skills approach earns its keep only when the catalog
grows large enough that carrying every procedure on every session stops being negligible, which six
procedures are not, so it is deferred rather than built now.

## 2026-07-21 — Test models are referenced, never shipped

**Decision:** The repository ships no calibration test models. Each procedure that needs a printed
test names its `test_source`, which is one of: the slicer's own calibration generator (OrcaSlicer's
built-in temperature tower, flow, pressure advance, retraction); a named external G-code generator,
linked (for example Ellis's Pressure/Linear Advance tool for pressure advance); or a link to a
community model (a site such as Printables) for a physical object not covered by the first two.
Macro-based procedures (`input_shaper`, `pid_tune`) need no test source. This closes the test-print
-models open question in [procedures.md](design/procedures.md).

**Why:** Bundling STLs would raise a licensing question — many popular calibration models are
non-commercial or share-alike — for no real benefit, since the tests already exist where the user
works. OrcaSlicer, the assumed slicer, generates most of them directly; where it does not, a
well-known generator or a community model link covers the rest. Referencing rather than
redistributing keeps the repository free of third-party model licenses while still telling the user
exactly what to print.

## 2026-07-23 — Agent backend: claude-agent-sdk (Option A), on the Claude subscription

**Decision:** The agent loop uses `claude-agent-sdk` (as the architecture specified), not the raw
Anthropic API Tool Runner. It implements the orchestrator's `AgentClient` seam. Chosen over the
Tool Runner specifically because `claude-agent-sdk` drives the Claude Code CLI, which can
authenticate with a **Claude subscription** (OAuth login) rather than pay-per-token API billing —
the raw API SDK has no subscription path. Its `can_use_tool` permission callback maps directly onto
the approval gate, and `create_sdk_mcp_server` hosts our `project`/`gcode`/`printer` tool servers
in-process.

**Why / trade-offs made explicit:**
- `claude-agent-sdk` bundles a ~273 MB self-contained native `claude` executable (no Node needed)
  and runs the agent as a subprocess. That cost lands only in the container image, not the hermetic
  test suite: the SDK dependency is fetched by Bazel only for targets that depend on it (the image
  build and a `manual` live test), so `bazel test //...` stays lean.
- The adapter is split so its security-critical config (allow/deny tool lists, permission mode) and
  its message→event translation are pure and hermetically tested; the SDK-driving client is a lazy
  import behind a `manual`/live test that needs subscription credentials.
- Subscription auth fits the primary local-on-LAN mode (runs under `claude login`); a cloud
  deployment would provision OAuth credentials into the container or fall back to an API key, and
  automated subscription use should be checked against the plan's terms.

## 2026-07-23 — Agent credentials required at startup (subscription OAuth token)

**Decision:** The system authenticates to Claude with a **Claude-subscription OAuth token supplied
at runtime via an environment variable** (Claude Code's `claude setup-token` mechanism; the exact
variable name is confirmed against the current SDK/CLI at implementation). It is a **hard startup
requirement**: the process crashes at startup if the token is absent — no API-key fallback and no
graceful degradation. Tests and the `--check` build smoke test set a fake token so they run without
real credentials (the SDK path is never actually invoked hermetically). Secrets come from the
environment only, never the image.

**Why:** The subscription (not per-token API billing) is the intended cost model, and a token env
var is headless-friendly — identical for a local run and the container, with nothing mounted.
Crashing at startup rather than degrading makes a misconfiguration loud and immediate, which is
preferable to a UI that silently cannot answer.

## 2026-07-23 — G-code index built synchronously on upload (for now)

**Decision:** When a G-code file is uploaded, its index is built **synchronously during the upload
request**, and the built index is stored. This supersedes, for the first end-to-end wiring, the
background-build-with-progress design ([file_indexing.md T4.6](design/file_indexing.md)); building
on demand / in the background is now future work ([spec.md §14](spec.md#14-future-work)).

**Why:** Synchronous-on-upload is the simplest path to a working end-to-end agent that can answer
G-code questions, and it keeps the tool surface honest (the index is ready when the session is).
The background build matters at the file size limit, where the upload wait becomes noticeable; that
is the refinement, deferred until the simple path is proven.

## 2026-07-23 — Voice transcription: bundled faster-whisper base model

**Decision:** Server-side voice transcription uses **faster-whisper** (CTranslate2, CPU int8), with
the **`base`** model **bundled into the container image** (fetched hermetically via Bazel, not
downloaded at runtime — fully offline/self-contained). Model size is env-overridable. Browser audio
(webm/opus from MediaRecorder) is decoded with `av` (PyAV, which bundles its own ffmpeg — no system
ffmpeg needed). Transcription runs synchronously in the audio-upload request; the transcript is
submitted as the (editable) user message so a mis-hear is visible and correctable, per web.md §7.
`faster-whisper` is lazy-imported so `bazel test //...` stays lean — the transcription path is
tested behind a fake-transcriber seam, not the real model.

**Why:** Bundling keeps the "one container, no external services" property and avoids a
first-run network dependency; `base` is fast on CPU and adequate for short voice notes, with `small`
available by env for better accuracy on technical terms.

## 2026-07-23 — Web-fetch SSRF guard dropped (WebFetch runs server-side)

**Decision:** The web-fetch SSRF guard is **dropped** — it is not needed. This supersedes the
2026-07-21 "Web fetch restricted to public addresses" decision and resolves the orchestration
open question / task about enforcing a loopback/private/link-local refusal on web fetch
(orchestration.md §2.2 open question, task T4.3). Web search and fetch stay enabled as-is.

**Why:** The earlier decision assumed the SDK's `WebFetch` executes **locally**, on our host — which
sits on the printer LAN — so a fetch (possibly to a URL from injected web content) could reach
`http://<printer>:7125/...` and read Moonraker around the approval gate. Research against
Anthropic's documentation showed that assumption is wrong: **`WebFetch`/`WebSearch` are server tools
that
execute on Anthropic's infrastructure, not on the host.** The HTTP request never originates from the
machine on the printer LAN, so the model physically cannot reach the printer (or any LAN device) via
web fetch. The LAN-bypass threat that motivated the guard does not exist for this architecture.

Anthropic additionally applies server-side private-address filtering (documented via the
`url_not_allowed` error, "Anthropic-side restrictions, such as private addresses"), though the exact
blocked ranges are not enumerated publicly — so we rely on the **structural** guarantee (Anthropic's
data centers cannot route to the user's LAN), not on the completeness of that filter.

**Residual risk and optional hardening (not required):** confidence in the structural guarantee is
high; confidence in Anthropic's private-IP filter completeness is medium. If we ever want to
constrain *what* the agent browses (focus, not LAN safety), the SDK supports a domain allowlist
(`WebFetch(domain:...)`) or disabling the tool — cheap to add later. Building our own guarded
web-fetch MCP tool would be belt-and-suspenders against a hole that does not exist for us, so it is
not planned.

**Sources:** Anthropic web-fetch tool docs (server-tool execution model) and Claude Code / Agent SDK
permissions docs (domain allowlist, `disallowed_tools`), reviewed 2026-07-23.
