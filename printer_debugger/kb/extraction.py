"""Per-section extraction: is this a printer, and its name, address, and config path.

Sectioning is mechanical; everything after is a model call ([kb_ingestion.md §2.1, §3.3]). A small
fast model answers, its result cached by the section's content hash so an unchanged section is
never re-extracted ([kb_ingestion.md §2.2]). The model call is a module-level function variable so
tests supply results without a network — honest here, since it returns three strings.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

from ..store.structured_store import StructuredStore
from .models import SectionExtraction

# A small fast model: extracting three values from prose is mechanical and runs on every change.
EXTRACTION_MODEL = "claude-haiku-4-5"


def section_hash(text: str) -> str:
    """Content hash keying a section's cached extraction."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_with_cache(store: StructuredStore, section_text: str) -> SectionExtraction:
    """Return a section's extraction, from the cache if the text is unchanged, else via the model.

    The cache entry is written for **every** section, printer or not, so a non-printer section is
    not re-sent to the model on the next document change ([kb_ingestion.md §2.2]).
    """
    digest = section_hash(section_text)
    cached = store.get_section_cache(digest)
    if cached is not None:
        return _from_cache(cached.result)
    extraction = _extract_section(section_text)
    store.put_section_cache(digest, _to_cache(extraction))
    return extraction


def normalize_address(raw: str | None) -> str | None:
    """Strip backticks and trailing parentheticals from a hostname, keeping the bare address."""
    if raw is None:
        return None
    cleaned = raw.replace("`", "").strip()
    cleaned = re.split(r"[\s(]", cleaned, maxsplit=1)[0]
    return cleaned or None


def normalize_config_path(raw: str | None, config_base: str) -> str | None:
    """Resolve a config path against the config-file base, not by expanding ``~``.

    ``~`` means nothing inside a container, so a leading ``~`` is replaced by ``config_base`` (the
    setting naming where the user's Klipper tree is mounted). Absolute paths pass through; a
    relative path joins onto the base ([kb_ingestion.md §3.3]).
    """
    if raw is None:
        return None
    cleaned = raw.replace("`", "").strip()
    cleaned = re.split(r"\s+\(", cleaned, maxsplit=1)[0].strip()
    if not cleaned:
        return None
    base = PurePosixPath(config_base)
    if cleaned.startswith("~/"):
        return str(base / cleaned[2:])
    if cleaned == "~":
        return str(base)
    path = PurePosixPath(cleaned)
    if path.is_absolute():
        return str(path)
    return str(base / cleaned)


def _to_cache(extraction: SectionExtraction) -> dict[str, object]:
    return {
        "is_printer": extraction.is_printer,
        "name": extraction.name,
        "address": extraction.address,
        "config_path": extraction.config_path,
    }


def _from_cache(result: object) -> SectionExtraction:
    data = dict(result)  # type: ignore[arg-type]
    return SectionExtraction(
        is_printer=bool(data.get("is_printer", False)),
        name=data.get("name"),
        address=data.get("address"),
        config_path=data.get("config_path"),
    )


def _extract_section(section_text: str) -> SectionExtraction:  # pragma: no cover - needs a model
    """Call the small fast model to classify and extract. Injected in tests.

    Uses the Anthropic Messages API with a strict JSON tool so the result is validated rather than
    parsed out of prose. Imported lazily so the module carries no SDK dependency until used.
    """
    import json

    import anthropic

    client = anthropic.Anthropic()
    tool = {
        "name": "record_printer",
        "description": "Record whether the section describes a 3D printer and its key values.",
        "input_schema": {
            "type": "object",
            "properties": {
                "is_printer": {"type": "boolean"},
                "name": {"type": ["string", "null"]},
                "address": {"type": ["string", "null"]},
                "config_path": {"type": ["string", "null"]},
            },
            "required": ["is_printer"],
        },
    }
    message = client.messages.create(
        model=EXTRACTION_MODEL,
        max_tokens=512,
        tools=[tool],
        tool_choice={"type": "tool", "name": "record_printer"},
        messages=[
            {
                "role": "user",
                "content": (
                    "Does the following markdown section describe a single 3D printer? If so, give "
                    "its name (from the heading), its hostname/address, and its Klipper config "
                    "path when present. Use null for anything absent.\n\n" + section_text
                ),
            }
        ],
    )
    for block in message.content:
        if getattr(block, "type", None) == "tool_use":
            data = block.input  # type: ignore[attr-defined]
            return SectionExtraction(
                is_printer=bool(data.get("is_printer", False)),
                name=data.get("name"),
                address=data.get("address"),
                config_path=data.get("config_path"),
            )
    raise RuntimeError("extraction model returned no tool use")
