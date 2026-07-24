"""Tests for TLS resolution (pure) and self-signed cert generation (offline, deterministic)."""

from __future__ import annotations

import ipaddress
import os
import tempfile
import unittest

from cryptography import x509

from printer_debugger.web import tls


class ResolveTlsTest(unittest.TestCase):
    """resolve_tls maps the PD_TLS* environment onto cert/key paths or plain HTTP."""

    def test_off_returns_none(self) -> None:
        """PD_TLS=off selects plain HTTP by returning None."""
        self.assertIsNone(tls.resolve_tls({"PD_TLS": "off"}, "/data"))

    def test_explicit_cert_and_key_used_as_is(self) -> None:
        """An explicit PD_TLS_CERT/PD_TLS_KEY pair is returned verbatim and marked non-auto."""
        env = {"PD_TLS_CERT": "/certs/c.pem", "PD_TLS_KEY": "/certs/k.pem"}
        result = tls.resolve_tls(env, "/data")
        assert result is not None
        self.assertEqual(result.cert_path, "/certs/c.pem")
        self.assertEqual(result.key_path, "/certs/k.pem")
        self.assertFalse(result.auto)

    def test_only_cert_falls_back_to_auto_path(self) -> None:
        """A lone PD_TLS_CERT (no key) does not count as explicit; the auto pair is chosen."""
        result = tls.resolve_tls({"PD_TLS_CERT": "/certs/c.pem"}, "/data")
        assert result is not None
        self.assertTrue(result.auto)
        self.assertEqual(result.cert_path, os.path.join("/data", "tls", "cert.pem"))

    def test_default_returns_auto_pair_under_data_dir(self) -> None:
        """With no PD_TLS* set, the auto-generated pair lives under <data_dir>/tls/."""
        result = tls.resolve_tls({}, "/data")
        assert result is not None
        self.assertTrue(result.auto)
        self.assertEqual(result.cert_path, os.path.join("/data", "tls", "cert.pem"))
        self.assertEqual(result.key_path, os.path.join("/data", "tls", "key.pem"))


class TlsHostnamesTest(unittest.TestCase):
    """tls_hostnames extracts concrete SAN candidates from the advertise/bind env."""

    def test_collects_concrete_hosts_and_skips_wildcards(self) -> None:
        """Concrete advertise/bind hosts are collected; 0.0.0.0 and empty values are skipped."""
        env = {"PD_ADVERTISE_HOST": "192.168.1.42", "PD_HOST": "0.0.0.0"}
        self.assertEqual(tls.tls_hostnames(env), ["192.168.1.42"])

    def test_empty_env_yields_no_hostnames(self) -> None:
        """No advertise/bind hosts set yields an empty list."""
        self.assertEqual(tls.tls_hostnames({}), [])


class EnsureSelfSignedTest(unittest.TestCase):
    """ensure_self_signed generates a persistable cert whose SAN covers the requested hosts."""

    def test_generates_files_when_absent(self) -> None:
        """When neither file exists, a cert and a 0600 key are written to the given paths."""
        with tempfile.TemporaryDirectory() as data_dir:
            cert_path = os.path.join(data_dir, "tls", "cert.pem")
            key_path = os.path.join(data_dir, "tls", "key.pem")
            tls.ensure_self_signed(cert_path, key_path, ["192.168.1.42"])
            self.assertTrue(os.path.exists(cert_path))
            self.assertTrue(os.path.exists(key_path))
            self.assertEqual(os.stat(key_path).st_mode & 0o777, 0o600)

    def test_noop_when_both_files_present(self) -> None:
        """When both files already exist the call is a no-op, leaving their contents untouched."""
        with tempfile.TemporaryDirectory() as data_dir:
            cert_path = os.path.join(data_dir, "cert.pem")
            key_path = os.path.join(data_dir, "key.pem")
            with open(cert_path, "w") as handle:
                handle.write("existing-cert")
            with open(key_path, "w") as handle:
                handle.write("existing-key")
            tls.ensure_self_signed(cert_path, key_path, ["192.168.1.42"])
            with open(cert_path) as handle:
                self.assertEqual(handle.read(), "existing-cert")
            with open(key_path) as handle:
                self.assertEqual(handle.read(), "existing-key")

    def test_san_covers_requested_hosts_and_defaults(self) -> None:
        """The generated cert's SAN holds the requested host plus localhost/127.0.0.1/::1."""
        with tempfile.TemporaryDirectory() as data_dir:
            cert_path = os.path.join(data_dir, "cert.pem")
            key_path = os.path.join(data_dir, "key.pem")
            tls.ensure_self_signed(cert_path, key_path, ["192.168.1.42", "printer.local"])
            with open(cert_path, "rb") as handle:
                certificate = x509.load_pem_x509_certificate(handle.read())
            san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            dns_names = san.get_values_for_type(x509.DNSName)
            ip_addresses = san.get_values_for_type(x509.IPAddress)
            self.assertIn("printer.local", dns_names)
            self.assertIn("localhost", dns_names)
            self.assertIn(ipaddress.ip_address("192.168.1.42"), ip_addresses)
            self.assertIn(ipaddress.ip_address("127.0.0.1"), ip_addresses)
            self.assertIn(ipaddress.ip_address("::1"), ip_addresses)


if __name__ == "__main__":
    unittest.main()
