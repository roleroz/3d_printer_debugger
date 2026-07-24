"""Self-signed TLS for secure-context browser APIs ([decisions.md 2026-07-23]).

Browsers only expose ``getUserMedia``/``MediaRecorder`` in a secure context (HTTPS or localhost).
Phones reach the app at ``http://<laptop-ip>:8080`` — an insecure origin — so mic recording is
blocked. Serving HTTPS with a self-signed cert restores the secure context after a one-time
certificate warning the user accepts on the phone.

``resolve_tls`` is a PURE function that decides which cert/key to use from the environment, so it
is hermetically testable without generating anything. The caller materialises the auto-generated
pair with ``ensure_self_signed`` only when needed. ``cryptography`` is imported lazily inside
``ensure_self_signed`` so the resolve path and most tests never pull it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Hosts that browsers always treat as secure and that iOS expects in the SAN; always added.
_ALWAYS_IN_SAN = ("localhost", "127.0.0.1", "::1")

# Bind/advertise placeholders that are not concrete addresses and must not go in the SAN.
_WILDCARD_HOSTS = ("0.0.0.0", "::", "")


@dataclass(frozen=True)
class TlsPaths:
    """A resolved certificate/key pair, plus whether it is the auto-generated pair.

    ``auto`` tells the caller it must call ``ensure_self_signed`` to materialise the files (an
    explicit ``PD_TLS_CERT``/``PD_TLS_KEY`` pair is used as-is and is never generated).
    """

    cert_path: str
    key_path: str
    auto: bool


def resolve_tls(env: Mapping[str, str], data_dir: str) -> TlsPaths | None:
    """Decide the TLS cert/key from the environment, or ``None`` for plain HTTP.

    - ``PD_TLS`` == ``"off"`` → ``None`` (plain HTTP).
    - ``PD_TLS_CERT`` and ``PD_TLS_KEY`` both set → those paths, used as-is (``auto=False``).
    - otherwise → the auto-generated pair under ``<data_dir>/tls/`` (``auto=True``).
    """
    if env.get("PD_TLS") == "off":
        return None
    cert = env.get("PD_TLS_CERT")
    key = env.get("PD_TLS_KEY")
    if cert and key:
        return TlsPaths(cert_path=cert, key_path=key, auto=False)
    tls_dir = os.path.join(data_dir, "tls")
    return TlsPaths(
        cert_path=os.path.join(tls_dir, "cert.pem"),
        key_path=os.path.join(tls_dir, "key.pem"),
        auto=True,
    )


def tls_hostnames(env: Mapping[str, str]) -> list[str]:
    """Concrete advertise/bind addresses to add to the cert SAN (wildcards skipped).

    Built from ``PD_ADVERTISE_HOST`` and ``PD_HOST`` when they name concrete addresses; the
    ``0.0.0.0``/``::`` wildcards are dropped since they are not routable names.
    """
    hostnames: list[str] = []
    for key in ("PD_ADVERTISE_HOST", "PD_HOST"):
        value = env.get(key)
        if value and value not in _WILDCARD_HOSTS:
            hostnames.append(value)
    return hostnames


def ensure_self_signed(cert_path: str, key_path: str, hostnames: Iterable[str]) -> None:
    """Generate a self-signed cert+key at the given paths unless both already exist.

    Reused across restarts: a no-op when both files are present, so the cert persists under the
    mounted data volume. The SAN includes every requested hostname plus ``localhost``/
    ``127.0.0.1``/``::1``; IP addresses become ``IPAddress`` entries and DNS names ``DNSName``
    entries (iOS requires the address itself to be in the SAN). The key is written as PEM with no
    passphrase and ``0600`` perms; the cert is PEM and valid for ~10 years.
    """
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return

    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    names = list(dict.fromkeys(list(hostnames) + list(_ALWAYS_IN_SAN)))
    san_entries: list[x509.GeneralName] = []
    for name in names:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            san_entries.append(x509.DNSName(name))

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "3D Printer Debugger")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    os.makedirs(os.path.dirname(cert_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)

    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # Open with 0600 up front so the private key is never briefly world-readable.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as key_file:
        key_file.write(key_pem)
    os.chmod(key_path, 0o600)

    with open(cert_path, "wb") as cert_file:
        cert_file.write(certificate.public_bytes(serialization.Encoding.PEM))
