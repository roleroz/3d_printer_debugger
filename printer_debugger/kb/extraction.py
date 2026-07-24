"""Per-section extraction: is this a printer, and its name, address, and config path.

Sectioning is mechanical; everything after is a model call ([kb_ingestion.md §2.1, §3.3]). A small
fast model answers, its result cached by the section's content hash so an unchanged section is
never re-extracted ([kb_ingestion.md §2.2]). The model call is a module-level function variable so
tests supply results without a network — honest here, since it returns three strings.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import threading
from collections.abc import Coroutine
from pathlib import PurePosixPath
from typing import TypeVar

from ..store.structured_store import StructuredStore
from .models import SectionExtraction

# A small fast model: extracting three values from prose is mechanical and runs on every change.
EXTRACTION_MODEL = "claude-haiku-4-5"

_T = TypeVar("_T")

# A single persistent event loop drives every SDK query for the process lifetime. It is created
# once, run with ``run_forever()`` on a daemon thread, and never torn down. A fresh
# ``asyncio.run`` loop per section was the crash's cause: ``_query_extraction`` breaks out of the
# ``async for`` at ``ResultMessage``, leaving the SDK's subprocess-backed async generator
# suspended, and ``asyncio.run`` then closed that loop — so the generator and its subprocess
# transport were finalized later against an already-closed loop (``aclose(): asynchronous
# generator is already running``; ``Loop ... that handles pid N is closed``; leaked subprocesses).
# One long-lived loop keeps the subprocess child-watcher valid and never tears a loop down while a
# query's generator/transport is still finalizing. A background-thread loop can still spawn
# subprocesses under Python 3.12's ``ThreadedChildWatcher``.
_loop_lock = threading.Lock()
_background_loop: asyncio.AbstractEventLoop | None = None


def _get_background_loop() -> asyncio.AbstractEventLoop:
    """Return the process-wide background event loop, creating and starting it once (thread-safe).

    Double-checked locking so the loop and its daemon thread are created exactly once even under
    concurrent first calls; every later call returns the same already-running loop.
    """
    global _background_loop
    loop = _background_loop
    if loop is not None:
        return loop
    with _loop_lock:
        if _background_loop is not None:
            return _background_loop
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="kb-extraction-loop", daemon=True)
        thread.start()
        _background_loop = loop
        return loop


def _run_on_background_loop(coro: Coroutine[object, object, _T]) -> _T:
    """Run ``coro`` on the shared background loop and block for its result.

    Dispatches with ``asyncio.run_coroutine_threadsafe`` and blocks on the future, giving the same
    synchronous, caller-blocking semantics whether called from the FastAPI request-loop thread or
    a worker thread — without ever creating or tearing down a loop per call.
    """
    loop = _get_background_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


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
    # Hold the async iterator so it can be fully closed inside this still-running loop. Breaking at
    # ResultMessage leaves it suspended; closing it here (rather than letting GC finalize it later
    # against a torn-down loop) is what prevents the ``aclose(): asynchronous generator is already
    # running`` crash and the leaked ``claude`` subprocess per section.
    agen = query(prompt=_build_prompt(section_text), options=options)
    try:
        async for message in agen:
            kind = type(message).__name__
            if kind == "AssistantMessage":
                for block in message.content:
                    if type(block).__name__ == "TextBlock":
                        last_text = block.text
            elif kind == "ResultMessage":
                structured = getattr(message, "structured_output", None)
                break
    finally:
        aclose = getattr(agen, "aclose", None)
        if aclose is not None:
            await aclose()
    if isinstance(structured, dict):
        return _dict_to_extraction(structured)
    return _parse_extraction(last_text)


def _extract_section(section_text: str) -> SectionExtraction:  # pragma: no cover - needs a model
    """Call the small fast model to classify and extract. Injected in tests.

    Runs a ``claude-agent-sdk`` query authenticated with the subscription OAuth token
    (``CLAUDE_CODE_OAUTH_TOKEN``, passed via ``ClaudeAgentOptions.env`` — never an API key). The
    SDK is imported only inside the async helper, so the module carries no SDK dependency until
    used. Stays synchronous: the query is dispatched to the single persistent background loop and
    blocks the caller until the result is ready. Because it always dispatches to that dedicated
    loop, it works identically whether called from the FastAPI request-loop thread or a worker
    thread — no need to detect whether a loop is already running.
    """
    import os

    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        raise RuntimeError(
            "CLAUDE_CODE_OAUTH_TOKEN is not set; section extraction needs the subscription "
            "token (ANTHROPIC_API_KEY is deliberately not used)."
        )
    return _run_on_background_loop(_query_extraction(section_text, token))
