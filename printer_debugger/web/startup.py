"""Startup URL printing ([web.md §9]).

On binding the listener the process prints every externally reachable URL, never a localhost the
phone cannot use. Where the machine has several addresses, all are printed rather than one being
guessed at. If bound to loopback only, that is stated plainly.
"""

from __future__ import annotations

import re
import socket


def reachable_urls(port: int, bound_host: str = "0.0.0.0", scheme: str = "http") -> list[str]:
    """Return the URLs a phone could use to reach the server, or a loopback-only note."""
    loopback_note = "  (loopback only — not reachable from your phone)"
    if bound_host not in ("0.0.0.0", "::", ""):
        if _is_loopback(bound_host):
            return [f"{scheme}://{bound_host}:{port}{loopback_note}"]
        return [f"{scheme}://{bound_host}:{port}"]
    addresses = _local_addresses()
    if not addresses:
        return [f"{scheme}://127.0.0.1:{port}{loopback_note}"]
    return [f"{scheme}://{address}:{port}" for address in addresses]


def format_banner(
    port: int,
    bound_host: str = "0.0.0.0",
    advertise_host: str | None = None,
    *,
    tls: bool = False,
    self_signed: bool = False,
) -> str:
    """A human banner listing where the UI is available.

    ``advertise_host`` overrides auto-detection — needed inside a container, whose own addresses
    (the Docker bridge) are not reachable from a phone. When detecting, a hint explains the
    override for exactly that case. ``tls`` switches the printed scheme to ``https``; when the
    HTTPS cert is self-signed, a line explains the one-time certificate warning the phone shows.
    """
    scheme = "https" if tls else "http"
    lines = ["3D Printer Debugger is available at:"]
    if advertise_host:
        lines.append(f"  {scheme}://{advertise_host}:{port}")
    else:
        urls = reachable_urls(port, bound_host, scheme)
        lines.extend(f"  {url}" for url in urls)
        if _looks_containerised(urls):
            lines.append("")
            lines.append(
                "  NOTE: that looks like a container-internal address your phone cannot reach. "
                "Set PD_ADVERTISE_HOST to the host's LAN IP, or run the container with "
                "--network host."
            )
    if tls and self_signed:
        lines.append("")
        lines.append(
            "  NOTE: this uses a self-signed certificate — your phone will show a one-time "
            "security warning; accept it once to enable microphone recording."
        )
    return "\n".join(lines)


def _looks_containerised(urls: list[str]) -> bool:
    """Whether every detected URL is a Docker bridge address (172.16–172.31.x)."""
    for url in urls:
        match = re.search(r"://(\d+)\.(\d+)\.", url)
        if match is None or not (int(match.group(1)) == 172 and 16 <= int(match.group(2)) <= 31):
            return False
    return bool(urls)


def _is_loopback(host: str) -> bool:
    return host.startswith("127.") or host in ("localhost", "::1")


def _local_addresses() -> list[str]:
    """Best-effort list of the host's non-loopback IPv4 addresses."""
    addresses: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                addresses.add(ip)
    except socket.gaierror:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))
        addresses.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    return sorted(a for a in addresses if not a.startswith("127."))
