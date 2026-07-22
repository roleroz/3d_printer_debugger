"""Authentication modes and cross-site request protection ([web.md §8, §8.1]).

Local mode: no auth; the LAN is the trust boundary. Exposed mode: an allowlist of OIDC subjects
all mapping to one system user, plus a CSRF defense on every mutating request — a forged approval
would run a printer command with no human deciding, defeating the gate.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from urllib.parse import urlparse

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


class AuthMode(enum.Enum):
    """The two deployment modes."""

    LOCAL = "local"
    EXPOSED = "exposed"


class ConfigurationError(Exception):
    """Startup refuses on an unset or contradictory auth configuration ([web.md §8])."""


@dataclass(frozen=True, slots=True)
class AuthConfig:
    """Resolved authentication configuration."""

    mode: AuthMode
    allowed_subjects: frozenset[str] = frozenset()
    allowed_origin: str | None = None

    def __post_init__(self) -> None:
        if self.mode is AuthMode.EXPOSED and not self.allowed_subjects:
            raise ConfigurationError("exposed mode requires an allowlist of subjects")
        if self.mode is AuthMode.EXPOSED and not self.allowed_origin:
            raise ConfigurationError("exposed mode requires an allowed origin for CSRF checks")


def resolve_mode(raw: str | None) -> AuthMode:
    """Resolve the configured mode string; an unset or unknown value is fatal."""
    if raw is None:
        raise ConfigurationError("auth mode is unset; set it to 'local' or 'exposed'")
    try:
        return AuthMode(raw)
    except ValueError as exc:
        raise ConfigurationError(f"unknown auth mode {raw!r}") from exc


def authorize(config: AuthConfig, subject: str | None) -> bool:
    """Whether a request's subject is allowed. Local mode allows everyone on the LAN."""
    if config.mode is AuthMode.LOCAL:
        return True
    return subject is not None and subject in config.allowed_subjects


def csrf_ok(config: AuthConfig, method: str, origin: str | None) -> bool:
    """Whether a request passes the CSRF check.

    Local mode has no cookie and no auth, so nothing to forge. In exposed mode a mutating request
    must carry an Origin matching the allowed origin ([web.md §8.1]).
    """
    if config.mode is AuthMode.LOCAL:
        return True
    if method.upper() not in _MUTATING:
        return True
    if origin is None:
        return False
    return _same_origin(origin, config.allowed_origin)


def _same_origin(origin: str, allowed: str | None) -> bool:
    if allowed is None:
        return False
    a, b = urlparse(origin), urlparse(allowed)
    return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)
