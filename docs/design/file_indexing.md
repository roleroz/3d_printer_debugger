# File indexing and MCP tools — module design

This module makes `.3mf` projects and G-code files answerable without ever putting them in the
model's context. It owns two indexers and the three MCP servers the agent reaches the world
through.

Requirements: [spec.md §7](../spec.md#7-large-file-access-requirements) and
[spec.md §5.7](../spec.md#57-diagnosis). Components:
[architecture.md §3.4](../architecture.md#34-mcp-capability-servers) and
[§3.5](../architecture.md#35-file-indexers).

## 1. Scope

**In scope:** the `.3mf` and G-code index formats, how they are built, and the `project` and
`gcode` MCP tool surfaces. The `printer` MCP server's tools are defined here too, since all three
share one response discipline, but its behaviour belongs to
[printer_access.md](printer_access.md).

**Out of scope:** what questions the model asks. This module guarantees that any question it can
answer is answered in bounded time and bounded size.

## 2. Design decisions

### 2.1 Index once, query many times

Both formats are parsed once at ingest into a compact index stored as an artifact
([design/store.md §4.10](store.md#410-file_index)). Queries read the index and, when they need
raw content, seek to a byte offset and read a bounded window. No query ever scans the file.

### 2.2 G-code indexing runs in the background

A session becomes usable immediately; capabilities appear as they become ready.

The header and configuration block are available within seconds — they are at the file's
extremities and need no full pass. Layer, coordinate, and state queries need the full pass, which
for a file at the size limit takes real time. Until it completes, those tools return **"index
still building", with progress**, rather than failing or blocking.

Blocking the session until indexing finished would mean staring at a progress bar before asking
the first question, when the first question is very often about slicer settings and answerable from
the `.3mf` alone. Lazy indexing on first query would move the same wait to an unexplained pause
mid-conversation.

### 2.3 Every tool bounds its own response

Each tool has a response ceiling. A request that would exceed it fails with a message naming the
limit and how to narrow it — a smaller layer range, a tighter window, a specific object. Never a
silent truncation, which would make the model reason confidently about partial data.

## 3. `.3mf` index

A `.3mf` is a zip of XML, JSON, and mesh geometry. At ingest the settings, metadata, and thumbnails
are extracted into a compact index small enough to hold in memory and store whole. The mesh
geometry — which can be tens of megabytes — is **not** loaded into that index, but it is
**retained**: it stays in the stored `.3mf` artifact and is read on demand by the geometry tools
([§3.1](#31-intended-geometry)). It is the record of what the part was supposed to look like, and
diagnosis depends on it.

Extracted, per [spec.md §7](../spec.md#7-large-file-access-requirements):

| Content | Notes |
| --- | --- |
| Print process settings | Layer height, widths, speeds, cooling, temperatures, retraction, walls |
| Filament settings | Material type, temperatures, flow |
| Printer settings | Nozzle diameter, printable area, machine limits — the identification inputs |
| Preset lineage | Preset names; the modified-from-preset diff is future work |
| Per-object and per-modifier overrides | Plus object placement on the plate |
| Plate and model metadata | Object names, counts, transforms |
| Preview thumbnails | Extracted as separate artifacts |
| Plate layout | Object footprints in plate coordinates |

**The modified-from-preset set is called out** because it would be the highest-signal thing in the
file: a project with two hundred settings at their defaults and three overridden — the three are
the story. But an OrcaSlicer `.3mf` does not record which settings were overridden, and it names
its presets (`print_settings_id`, `filament_settings_id`, `printer_settings_id`) without embedding
them — they live in the user's OrcaSlicer preset library. Producing the set therefore needs that
external library as a baseline. This version does not ingest it: it serves the resolved settings
and the preset names, and `get_modified_settings` reports the diff unavailable rather than guessing.
Ingesting the preset library to produce the diff is future work
([spec.md §14](../spec.md#14-future-work)).

**The plate layout is retained in plate coordinates**, which is what makes photo-to-plate matching
possible ([spec.md §5.7](../spec.md#57-diagnosis)): an object identified visually maps to an XY
region, and that region maps into the G-code.

**A single plate is assumed.** An OrcaSlicer project can hold several plates, each with its own
objects, layout, and sliced G-code. This version treats a project as one plate, using the first
when more than one is present; it does not associate a G-code with a particular plate or index the
others. Multi-plate support — indexing each plate and tying it to its own G-code — is future work
([spec.md §14](../spec.md#14-future-work)).

### 3.1 Intended geometry

The mesh is what the part was supposed to be, and diagnosis leans on it: the model compares the
intended shape against a photograph of what actually printed. A layer shift turns a cube into a
parallelogram; a missing feature is missing only against a reference that shows it should be there;
a sagged overhang is a departure from an intended surface. None of that is visible in settings or
toolpaths.

The mesh is exposed the way every large thing in this module is — through bounded, targeted views,
never dumped into the context, since raw triangle data is both huge and useless to a vision model:

- **Rendered views** — an object's intended geometry is rendered from a requested viewpoint (a
  named angle, or one described to match the angle the user photographed) into an image artifact.
  This is the surface a vision model uses to set intended against printed, side by side.
- **Measurements** — the object's bounding box, height, footprint, volume, and overhang extents, as
  bounded numbers, for questions like how tall a feature should be.

Rendering happens on demand from the retained artifact, so the in-memory index stays small while
the geometry stays available. Slicing the mesh at a given Z to produce the intended cross-section
at a specific layer is future work ([spec.md §14](../spec.md#14-future-work)); until then, the
toolpath at that layer approximates the intended outline there.

## 4. G-code index

One forward pass over the file, emitting a structure whose entries all carry byte offsets. The
pass interprets the G-code as it reads it — absolute and relative positioning (`G90`/`G91`),
absolute and relative extrusion (`M82`/`M83`), and arc moves (`G2`/`G3`) — so coordinates,
extrusion totals, and reconstructed state are correct rather than assuming linear absolute moves.

### 4.1 What the pass records

| Record | Contents |
| --- | --- |
| Layer table | Layer number, Z height, byte range, line range, print-time offset |
| Layer summary | Features present, speed range, extrusion total, layer time estimate |
| Object map | Object name → per-layer XY bounding boxes and byte ranges |
| State checkpoints | Full machine state at each layer start, with its byte offset |
| Event list | Temperature changes, fan changes, retractions, Z hops, tool changes |
| Anomaly list | Layers whose time, extrusion, or speed departs from their neighbours |
| Thumbnails | Extracted as artifacts |
| Header and config block | Stored verbatim; small |

**Object attribution** — mapping each extrusion to the object that owns it — uses the object
markers the slicer emits (the labelled-object comments) when they are present, which is the
reliable path. When they are absent, an extrusion is attributed geometrically, by which object
footprint in the plate layout its XY falls within, at lower confidence. Enabling object labelling
in the slicer therefore improves the object map and the photo-to-plate matching that depends on it.

The **print-time offset** in the layer table is read from the slicer's per-layer time markers where
present, rather than recomputed from feedrates, since the slicer's estimate accounts for the
acceleration that the raw moves do not.

### 4.2 State checkpoints are the important part

Machine state at an arbitrary point — nozzle and bed targets, fan speed, feedrate and acceleration
limits, pressure advance, flow multiplier, absolute or relative extrusion — is **cumulative**. It
is set by commands potentially megabytes earlier and is not visible in a window around the point of
interest ([spec.md §7](../spec.md#7-large-file-access-requirements)).

Recomputing it from the file start on every query would be a full scan, defeating the index. So
the pass writes a complete state snapshot at the start of every layer, and a state query replays
from the snapshot at the start of the layer containing the point. That bounds replay to a single
layer's worth of commands regardless of file size or where in the file the question lands.

The layer boundary is the natural snapshot point. It is where the layer table already carries a
byte offset, so a snapshot is one small record per layer — cheap, since a file holds thousands of
layers, not millions — and replay never crosses a layer. State questions are themselves usually
anchored on a layer, so the snapshot sits exactly where a query begins its replay.

### 4.3 Anomaly detection

Computed at index time by comparing each layer against a window of its neighbours: layer time,
extrusion volume per unit length, speed distribution, and feature composition. Layers that depart
are flagged.

This is what lets the system answer "which layer looks wrong" without the model paging through
thousands of them, and it is directly what
[spec.md §5.7](../spec.md#57-diagnosis) asks for when a defect is a single bad layer.

## 5. MCP tool surface

Three servers. Tool names follow `server.verb_noun`. Every tool returns structured data with a
`bounded` marker stating how much of the available answer was returned.

### 5.1 `project`

| Tool | Answers |
| --- | --- |
| `get_settings` | Process, filament, or printer settings, whole or by key |
| `get_modified_settings` | What differs from the preset; reports unavailable without the library |
| `get_objects` | Objects, counts, placement, per-object overrides |
| `get_plate_layout` | Footprints in plate coordinates, for photo matching |
| `get_object_render` | An object's intended geometry rendered from a viewpoint, as an artifact |
| `get_object_dimensions` | An object's bounding box, height, footprint, volume, overhang extents |
| `get_thumbnail` | A preview image as an artifact reference |
| `get_printer_identity` | Preset name, nozzle diameter, printable area, machine limits |

`get_printer_identity` exists as its own tool because session binding needs exactly those fields
([spec.md §5.2](../spec.md#52-session-lifecycle)) and should not have to read a settings blob to
find them. `get_object_render` and `get_object_dimensions` are the intended-geometry surface from
[§3.1](#31-intended-geometry): the render is what the model sets against a photo, the dimensions
answer "what should this measure".

### 5.2 `gcode`

| Tool | Answers |
| --- | --- |
| `get_header` | Slicer header and configuration block |
| `get_layer_table` | Layer count; Z, byte range, and time offset per layer |
| `locate` | Layer containing a given Z height, line number, time offset, or XY point |
| `summarise_layers` | Features, speeds, extrusion totals across a layer range |
| `get_commands` | Raw commands in a bounded window around a layer, Z, or line |
| `get_region` | Commands printing a given XY region or object on a given layer |
| `get_state_at` | Reconstructed machine state at a point |
| `get_events` | Temperature, fan, retraction, Z-hop events in a range |
| `get_anomalies` | Layers departing from their neighbours |
| `get_thumbnail` | Embedded preview as an artifact reference |
| `index_status` | Whether the index is ready, and progress if not |

`index_status` is what makes background indexing honest: the model can ask, and an unready tool
says so specifically rather than returning nothing.

### 5.3 `printer`

Read tools and one write tool, defined in [printer_access.md](printer_access.md). Listed here for
completeness because the response discipline is shared.

| Tool | Answers |
| --- | --- |
| `get_status` | Print state — idle, printing, paused, error — and progress |
| `get_temperatures` | Current and target temperatures for hotend, bed, and chamber |
| `get_position` | Current toolhead position and homing state |
| `get_config` | Configured and saved configuration, tiers distinguished |
| `get_runtime_state` | Live runtime values, including those overriding the saved config |
| `get_logs` | A bounded window of the printer's log |
| `capture_still` | A webcam still as an artifact reference |
| `propose_command` | The one write: a G-code or macro proposed for approval, not executed |

**`propose_command` is the only tool in any server that changes anything.** Everything else in all
three servers is a read. That is what makes the approval gate's job small enough to be verifiable
([architecture.md §3.3](../architecture.md#33-approval-gate)).

## 6. Failure handling

| Failure | Behaviour |
| --- | --- |
| File is not a valid `.3mf` or G-code | Rejected at ingest naming the problem; no index row |
| Truncated file | Indexed as far as it parses; index marked partial; tools report the limit |
| Produced by a different slicer | Indexed best-effort; unrecognised header reported, not fatal |
| Index build fails | Recorded; tools report unavailable with reason; artifact retained for retry |
| Query would exceed the ceiling | Fails with the limit and a narrowing suggestion |
| Query on an unbuilt index | Returns not-ready with progress, not an error |
| No usable mesh, or render fails | Rendering unavailable with reason; measurements still served |
| Modified settings, no preset library | Diff unavailable; resolved values and preset names served |
| Index format version outdated | Discarded and rebuilt from the retained artifact |

## 7. Testing

- **Real files are the fixtures.** A `.3mf` and its G-code, sliced from the example printers, held
  as test data. A synthetic G-code generator would be a re-implementation of a slicer, and its
  output would not exercise what real files contain.
- **A large file is used for the bounded-response and background-indexing tests**, since those
  behaviours only exist at size.
- **State reconstruction is tested against ground truth**: replay from a checkpoint is compared
  against a full replay from the file start, at points chosen throughout the file.
- **Arc moves and relative modes are tested**: a file using `G2`/`G3` and `M83` is indexed and its
  coordinates and extrusion totals match a hand-computed expectation, proving the pass does not
  assume linear absolute moves.
- **Object attribution is tested both ways**: a file with object markers attributes by them, and a
  file without falls back to geometric attribution against the plate layout.
- **Geometry tools are tested against a known object**: measurements match its true dimensions
  within tolerance, a render is produced for each requested viewpoint, and an object with no usable
  mesh reports rendering unavailable rather than erroring.
- **Every tool has a ceiling test** proving an oversized request fails with guidance rather than
  truncating.
- **Locate is tested in both directions** — height to layer and layer to height — and at
  boundaries: first layer, last layer, a Z between layers.
- **Anomaly detection is tested on a file with a known anomaly**, and on a uniform file to prove
  it reports nothing.
- **Malformed inputs are tested** — truncated mid-layer, wrong format, empty file — each asserting
  the error names the problem and no partial index row is written.

## 8. Open questions

1. **Whether the object map needs sub-layer granularity.** The per-object, per-layer bounding box
   is axis-aligned, so two objects' boxes can overlap on one layer even though the parts never
   do — a rotated part's box covers empty corners, and an L-shaped part's box covers its notch,
   into which another part may be packed. An XY point in an overlap region can't be attributed by
   the box alone. Attribution itself does not depend on the box — the slicer's labelled-object
   markers ([§4.1](#41-what-the-pass-records)) attribute each extrusion exactly and are the fallback
   — so the question is only whether the box summary is a good enough matching hint. Deferred until
   photo-to-plate matching is exercised on a real densely-packed multi-object plate.
2. **Index storage format.** The format must let each section be read independently, seeked to by
   the byte offsets the index already carries, so a state query loads one checkpoint and one
   window without deserialising the whole index, and the header and config block are served while
   the G-code pass is still running. That seek-by-offset property is a design constraint. The
   concrete format that satisfies it — a length-prefixed section layout, SQLite, a columnar
   format, an offset table over JSON — is an implementation choice measured against a real index at
   the size limit.
3. **Default render viewpoints and resolution.** Arbitrary-angle rendering is settled —
   `get_object_render` takes a named angle or one described to match the photograph
   ([§3.1](#31-intended-geometry)). Open is the tuning: which small set of convenient *named*
   preset views to offer, and the default resolution, which trades image size and token cost
   against how well the model can distinguish a subtle defect from the reference. Both are tuned
   against real diagnoses on real photos once rendering is in.

## 9. Implementation tasks

- [x] **T3.1** `.3mf` reader: extract settings, metadata, and thumbnails into the index; retain
      the mesh in the stored artifact for the geometry tools.
- [x] **T3.2** Modified-from-preset: expose the preset names and resolved settings; report the diff
      unavailable without the preset library (ingesting the library is future work).
- [x] **T3.3** Plate layout extraction in plate coordinates (first plate; single-plate assumption).
- [x] **T3.4** Object rendering from the retained mesh into image artifacts, by viewpoint.
- [x] **T3.5** Object measurements: bounding box, height, footprint, volume, overhang extents.
- [x] **T4.1** G-code pass: layer table, byte offsets, line ranges, time offsets, with
      absolute/relative positioning and extrusion and arc-move interpretation.
- [x] **T4.2** Layer summaries and the event list.
- [x] **T4.3** Per-layer state snapshots and the replay-from-layer-start reconstruction.
- [x] **T4.4** Object map with per-layer bounding boxes, attributed by slicer object markers with
      geometric fallback.
- [x] **T4.5** Anomaly detection.
- [ ] **T4.6** Background index build with progress reporting and status. Superseded for the MVP by
      the synchronous build inside the upload request ([decisions.md 2026-07-23]); the G-code index
      is built and stored during `POST /sessions/{id}/files` at the composition root. On-demand /
      background build is future work ([spec.md §14]); this box stays unchecked until then.
- [x] **T5.1** Shared MCP response discipline: ceilings, bounded markers, narrowing guidance.
- [x] **T5.2** `project` server tools.
- [x] **T5.3** `gcode` server tools.
- [x] **T6.1** Format versioning and rebuild-on-outdated.
