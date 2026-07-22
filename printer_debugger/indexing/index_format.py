"""Serialise a G-code index to bytes and back for storage as an artifact.

The stored index must be seekable by the byte offsets it carries so a state query loads one
checkpoint and one window without deserialising the whole thing ([file_indexing.md §8, OQ2]). This
version uses a JSON document keyed by section; a specialised seek-by-offset container is a later
refinement of the same interface. ``format_version`` makes a stale index disposable and rebuildable.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from .gcode import FORMAT_VERSION, Event, GcodeIndex, Layer, ObjectSpan
from .gcode_state import MachineState


def dumps(index: GcodeIndex) -> bytes:
    """Serialise an index to JSON bytes."""
    document = {
        "format_version": index.format_version,
        "total_bytes": index.total_bytes,
        "total_lines": index.total_lines,
        "header_text": index.header_text,
        "config_text": index.config_text,
        "layers": [asdict(layer) for layer in index.layers],
        "events": [asdict(event) for event in index.events],
        "object_spans": [asdict(span) for span in index.object_spans],
        "anomalies": index.anomalies,
        "thumbnails": index.thumbnails,
    }
    return json.dumps(document).encode("utf-8")


def loads(data: bytes) -> GcodeIndex:
    """Deserialise an index from JSON bytes."""
    document = json.loads(data)
    index = GcodeIndex(
        total_bytes=document["total_bytes"],
        total_lines=document["total_lines"],
        header_text=document["header_text"],
        config_text=document["config_text"],
        layers=[_layer_from_dict(item) for item in document["layers"]],
        events=[Event(**item) for item in document["events"]],
        object_spans=[ObjectSpan(**item) for item in document["object_spans"]],
        anomalies=list(document["anomalies"]),
        thumbnails=list(document["thumbnails"]),
        format_version=document["format_version"],
    )
    return index


def is_current(data: bytes) -> bool:
    """Whether a stored index matches the current format version."""
    try:
        return json.loads(data).get("format_version") == FORMAT_VERSION
    except (ValueError, AttributeError):
        return False


def _layer_from_dict(item: dict) -> Layer:
    state = MachineState(**item["state_at_start"])
    return Layer(
        number=item["number"],
        z=item["z"],
        start_line=item["start_line"],
        end_line=item["end_line"],
        start_offset=item["start_offset"],
        end_offset=item["end_offset"],
        state_at_start=state,
        extrusion=item["extrusion"],
        features=tuple(item["features"]),
        speed_min=item["speed_min"],
        speed_max=item["speed_max"],
    )
