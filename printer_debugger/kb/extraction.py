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


# The strict schema for structured output: the SDK maps ``output_format`` onto the CLI's
# ``--json-schema`` flag, so the model returns this shape on ``ResultMessage.structured_output``.
_EXTRACTION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "is_printer": {"type": "boolean"},
        "name": {"type": ["string", "null"]},
        "address": {"type": ["string", "null"]},
        "config_path": {"type": ["string", "null"]},
    },
    "required": ["is_printer"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You classify one markdown section describing home-lab hardware. Decide whether it describes "
    "a single 3D printer and, if so, extract its name (from the heading), its hostname/address, "
    "and its Klipper config path. Use null for any absent value and false for is_printer when the "
    "section is not a printer."
)


def _build_prompt(section_text: str) -> str:
    """The user prompt: the section text under a fixed extraction instruction."""
    return (
        "Classify this markdown section and extract the printer's name, address, and Klipper "
        "config path when present. Use null for anything absent.\n\n" + section_text
    )


def _opt_str(value: object) -> str | None:
    """Coerce a model-supplied field to ``str | None``: pass strings through, else None."""
    return value if isinstance(value, str) else None


def _dict_to_extraction(data: object) -> SectionExtraction:
    """Validate a parsed dict into a ``SectionExtraction`` with safe defaults.

    A non-dict, or a dict missing keys, yields a non-printer default rather than raising:
    ``is_printer`` defaults False and any absent name/address/config_path becomes None.
    """
    if not isinstance(data, dict):
        return SectionExtraction(is_printer=False)
    return SectionExtraction(
        is_printer=bool(data.get("is_printer", False)),
        name=_opt_str(data.get("name")),
        address=_opt_str(data.get("address")),
        config_path=_opt_str(data.get("config_path")),
    )


def _strip_json(text: str) -> str | None:
    """Isolate the outermost JSON object from prose or a code fence; None if none is present."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    return text[start : end + 1]


def _parse_extraction(json_text: str) -> SectionExtraction:
    """Parse a model's text response into a ``SectionExtraction``, defaulting safely on garbage.

    Strips any markdown code fence and isolates the outermost JSON object before parsing; any
    failure yields a non-printer default rather than raising into the synchronous ingest path.
    """
    import json

    stripped = _strip_json(json_text)
    if stripped is None:
        return SectionExtraction(is_printer=False)
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return SectionExtraction(is_printer=False)
    return _dict_to_extraction(data)


async def _query_extraction(  # pragma: no cover - needs a model
    section_text: str, token: str
) -> SectionExtraction:
    """Drive one small SDK query with json-schema output, reading the result from the stream.

    Prefers the SDK's structured output (``ResultMessage.structured_output``); if the model
    returned prose instead, falls back to robustly parsing the final assistant text.
    """
    from claude_agent_sdk import ClaudeAgentOptions, query

    options = ClaudeAgentOptions(
        model=EXTRACTION_MODEL,
        system_prompt=_SYSTEM_PROMPT,
        output_format={"type": "json_schema", "schema": _EXTRACTION_SCHEMA},
        max_turns=1,
        env={"CLAUDE_CODE_OAUTH_TOKEN": token},
    )
    structured: object = None
    last_text = ""
    async for message in query(prompt=_build_prompt(section_text), options=options):
        kind = type(message).__name__
        if kind == "AssistantMessage":
            for block in message.content:
                if type(block).__name__ == "TextBlock":
                    last_text = block.text
        elif kind == "ResultMessage":
            structured = getattr(message, "structured_output", None)
            break
    if isinstance(structured, dict):
        return _dict_to_extraction(structured)
    return _parse_extraction(last_text)


def _extract_section(section_text: str) -> SectionExtraction:  # pragma: no cover - needs a model
    """Call the small fast model to classify and extract. Injected in tests.

    Runs a ``claude-agent-sdk`` query authenticated with the subscription OAuth token
    (``CLAUDE_CODE_OAUTH_TOKEN``, passed via ``ClaudeAgentOptions.env`` — never an API key). The
    SDK and its event loop are entered only here, so the module carries no SDK dependency until
    used. Stays synchronous: it is called deep inside a running FastAPI request loop, so when a
    loop is already running it drives the async query on a private loop in a worker thread; with
    no loop running it uses ``asyncio.run`` directly.
    """
    import asyncio
    import concurrent.futures
    import os

    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        raise RuntimeError(
            "CLAUDE_CODE_OAUTH_TOKEN is not set; section extraction needs the subscription "
            "token (ANTHROPIC_API_KEY is deliberately not used)."
        )

    def _run() -> SectionExtraction:
        return asyncio.run(_query_extraction(section_text, token))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()  # No loop on this thread: run the coroutine directly.
    # A loop is already running (the FastAPI request thread): run on a private loop off-thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()
