"""Startup URL printing ([web.md §9]).

On binding the listener the process prints every externally reachable URL, never a localhost the
phone cannot use. Where the machine has several addresses, all are printed rather than one being
guessed at. If bound to loopback only, that is stated plainly.
"""

from __future__ import annotations

import socket


def reachable_urls(port: int, bound_host: str = "0.0.0.0") -> list[str]:
    """Return the URLs a phone could use to reach the server, or a loopback-only note."""
    if bound_host not in ("0.0.0.0", "::", ""):
        if _is_loopback(bound_host):
            return [f"http://{bound_host}:{port}  (loopback only — not reachable from your phone)"]
        return [f"http://{bound_host}:{port}"]
    addresses = _local_addresses()
    if not addresses:
        return [f"http://127.0.0.1:{port}  (loopback only — not reachable from your phone)"]
    return [f"http://{address}:{port}" for address in addresses]


def format_banner(port: int, bound_host: str = "0.0.0.0") -> str:
    """A human banner listing where the UI is available."""
    urls = reachable_urls(port, bound_host)
    lines = ["3D Printer Debugger is available at:"]
    lines.extend(f"  {url}" for url in urls)
    return "\n".join(lines)


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
