"""Live configuration provider, wiring the KB module's ``LiveConfigProvider`` seam (kb T4.3).

Fetches the running configuration over Moonraker and serialises it to INI text the KB module's
Klipper parser can consume. This is the concrete provider the composition root passes to
``import_live_config``.
"""

from __future__ import annotations

from .moonraker import MoonrakerClient, PrinterUnreachable


class LiveConfig:
    """Adapts a MoonrakerClient to the KB module's live-config provider protocol."""

    def __init__(self, client: MoonrakerClient) -> None:
        self._client = client

    def fetch_config(self, address: str) -> str:
        """Return the running configuration as INI text, including the SAVE_CONFIG values."""
        result = self._client.get_config()
        settings = result.get("status", {}).get("configfile", {}).get("settings")
        if settings is None:
            settings = result.get("configfile", {}).get("settings", {})
        return _to_ini(settings)


def _to_ini(settings: dict) -> str:
    """Serialise Moonraker's nested settings dict into Klipper-style INI text."""
    lines: list[str] = []
    for section, values in sorted(settings.items()):
        lines.append(f"[{section}]")
        if isinstance(values, dict):
            for key, value in values.items():
                lines.append(f"{key}: {value}")
        lines.append("")
    return "\n".join(lines)


def fetch_live_config_text(client: MoonrakerClient, address: str) -> str | None:
    """Convenience: fetch live config text, or None if the printer is unreachable."""
    try:
        return LiveConfig(client).fetch_config(address)
    except PrinterUnreachable:
        return None
