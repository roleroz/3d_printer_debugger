"""Application logging configuration for the container console.

Nothing else configures ``printer_debugger.*`` logging: ``uvicorn.run`` only sets up its own
``uvicorn``/``uvicorn.access`` loggers, so our package's INFO records never reach the console and a
swallowed exception in a log-and-continue handler is indistinguishable from "nothing happened".

This attaches a ``StreamHandler`` writing to ``sys.stdout`` directly on the ``printer_debugger``
package logger, with ``propagate = False``. Because the handler lives on the package logger rather
than on the root, records emit regardless of the root/uvicorn configuration, and uvicorn — which
only ever configures its own ``uvicorn`` loggers — cannot clobber it. The level defaults to
``INFO`` and is chosen by the caller from ``PD_LOG_LEVEL``.
"""

from __future__ import annotations

import logging
import sys

# The package logger every ``printer_debugger.*`` module logs through.
PACKAGE_LOGGER = "printer_debugger"
# Marks the handler we own, so repeated configuration replaces rather than stacks it.
_HANDLER_NAME = "printer_debugger.console"
# Timestamp, level, logger name, message — enough to trace a decision to its module.
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(level: str) -> None:
    """Attach a stdout ``StreamHandler`` to the ``printer_debugger`` logger at ``level``.

    Sets the package logger and its handler to ``level``, stops propagation so uvicorn's root
    configuration neither drops nor duplicates our records, and is idempotent: a handler this
    function previously attached is removed first, so repeated calls never stack duplicates.
    """
    resolved = _resolve_level(level)
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.setLevel(resolved)
    logger.propagate = False
    for existing in list(logger.handlers):
        if getattr(existing, "name", None) == _HANDLER_NAME:
            logger.removeHandler(existing)
    handler = logging.StreamHandler(sys.stdout)
    handler.name = _HANDLER_NAME
    handler.setLevel(resolved)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(handler)


def _resolve_level(level: str) -> int:
    """Translate a level name (e.g. ``INFO``) or numeric string to a level int, defaulting INFO."""
    if not level:
        return logging.INFO
    text = level.strip()
    if text.isdigit():
        return int(text)
    resolved = logging.getLevelName(text.upper())
    return resolved if isinstance(resolved, int) else logging.INFO
