"""Tests for security (auth + CSRF), the SSE hub, and startup URL printing."""

from __future__ import annotations

import asyncio
import unittest

from printer_debugger.web import startup
from printer_debugger.web.security import (
    AuthConfig,
    AuthMode,
    ConfigurationError,
    authorize,
    csrf_ok,
    resolve_mode,
)
from printer_debugger.web.sse import SseHub


class SecurityTest(unittest.TestCase):
    """Auth modes and the CSRF defense behave per web.md §8/§8.1."""

    def _exposed(self) -> AuthConfig:
        return AuthConfig(
            mode=AuthMode.EXPOSED,
            allowed_subjects=frozenset({"me@example.com"}),
            allowed_origin="https://app.example",
        )

    def test_local_allows_everyone(self) -> None:
        """Local mode authorises any request and skips CSRF."""
        config = AuthConfig(mode=AuthMode.LOCAL)
        self.assertTrue(authorize(config, None))
        self.assertTrue(csrf_ok(config, "POST", None))

    def test_exposed_requires_allowlisted_subject(self) -> None:
        """Exposed mode rejects a subject not on the allowlist."""
        config = self._exposed()
        self.assertTrue(authorize(config, "me@example.com"))
        self.assertFalse(authorize(config, "stranger@example.com"))
        self.assertFalse(authorize(config, None))

    def test_exposed_csrf_blocks_cross_site_post(self) -> None:
        """A mutating request from another origin is refused; the allowed origin passes."""
        config = self._exposed()
        self.assertFalse(csrf_ok(config, "POST", "https://evil.example"))
        self.assertTrue(csrf_ok(config, "POST", "https://app.example"))
        self.assertFalse(csrf_ok(config, "POST", None))
        self.assertTrue(csrf_ok(config, "GET", "https://evil.example"))  # reads are fine

    def test_misconfiguration_is_fatal(self) -> None:
        """Exposed mode without an allowlist or origin, or an unset mode, refuses at startup."""
        with self.assertRaises(ConfigurationError):
            AuthConfig(mode=AuthMode.EXPOSED)
        with self.assertRaises(ConfigurationError):
            resolve_mode(None)
        with self.assertRaises(ConfigurationError):
            resolve_mode("sideways")


class SseHubTest(unittest.TestCase):
    """The hub fans out to every subscriber and replays missed events on reconnect."""

    def test_fanout_to_all_subscribers(self) -> None:
        """A published event reaches every subscriber of the session."""

        async def scenario() -> None:
            hub = SseHub()
            q1 = hub.subscribe("s")
            q2 = hub.subscribe("s")
            hub.publish("s", "text", "hello")
            self.assertEqual((await q1.get()).data, "hello")
            self.assertEqual((await q2.get()).data, "hello")
            self.assertEqual(hub.subscriber_count("s"), 2)

        asyncio.run(scenario())

    def test_reconnect_replays_missed(self) -> None:
        """A reconnecting client gets exactly the events after its last id."""
        hub = SseHub()
        hub.publish("s", "text", "a")
        second = hub.publish("s", "text", "b")
        hub.publish("s", "text", "c")
        missed = hub.missed_since("s", second.id - 1)
        self.assertEqual([e.data for e in missed], ["b", "c"])

    def test_unsubscribe_does_not_affect_others(self) -> None:
        """One subscriber disconnecting leaves the others subscribed."""
        hub = SseHub()
        q1 = hub.subscribe("s")
        hub.subscribe("s")
        hub.unsubscribe("s", q1)
        self.assertEqual(hub.subscriber_count("s"), 1)

    def test_close_unblocks_and_primes_subscribers(self) -> None:
        """close() hands the None shutdown sentinel to both current and future subscribers."""

        async def scenario() -> None:
            hub = SseHub()
            existing = hub.subscribe("s")
            self.assertFalse(hub.is_closed)
            hub.close()
            self.assertTrue(hub.is_closed)
            self.assertIsNone(await existing.get())  # a parked subscriber is unblocked
            later = hub.subscribe("s")  # someone connecting during shutdown
            self.assertIsNone(await later.get())  # is primed with the sentinel at once

        asyncio.run(scenario())

    def test_close_is_idempotent(self) -> None:
        """Calling close() more than once is safe and keeps the hub marked closed."""
        hub = SseHub()
        hub.subscribe("s")
        hub.close()
        hub.close()
        self.assertTrue(hub.is_closed)

    def test_close_terminates_subscribed_stream(self) -> None:
        """A live SSE generator returns promptly once the hub is closed on shutdown.

        This is the regression test for the Ctrl+C hang: before the sentinel/close wiring the
        stream loops on ``queue.get()`` forever, so uvicorn's graceful shutdown never drains.
        """

        async def scenario() -> None:
            from printer_debugger.web.app import _event_stream

            hub = SseHub()

            class _NeverDisconnects:
                async def is_disconnected(self) -> bool:
                    return False

            frames: list[str] = []

            async def drain() -> None:
                async for frame in _event_stream(hub, "s", 0, _NeverDisconnects()):
                    frames.append(frame)

            task = asyncio.create_task(drain())
            while hub.subscriber_count("s") == 0:  # let it reach the blocking queue.get()
                await asyncio.sleep(0)
            hub.close()
            await asyncio.wait_for(task, timeout=2.0)  # hangs forever without the fix
            self.assertEqual(hub.subscriber_count("s"), 0)  # generator unsubscribed on exit

        asyncio.run(scenario())


class StartupTest(unittest.TestCase):
    """Startup prints reachable URLs, not localhost, and flags loopback-only binds."""

    def test_all_interfaces_lists_non_loopback(self) -> None:
        """Binding to all interfaces lists concrete addresses (or a loopback note)."""
        urls = startup.reachable_urls(8080, "0.0.0.0")
        self.assertTrue(urls)
        self.assertTrue(all(":8080" in url for url in urls))

    def test_loopback_bind_is_flagged(self) -> None:
        """Binding to loopback only is stated plainly as not reachable from a phone."""
        urls = startup.reachable_urls(8080, "127.0.0.1")
        self.assertIn("loopback only", urls[0])

    def test_banner_lists_urls(self) -> None:
        """The banner names where the UI is available."""
        banner = startup.format_banner(8080, "127.0.0.1")
        self.assertIn("available at", banner)

    def test_advertise_host_overrides_detection(self) -> None:
        """PD_ADVERTISE_HOST is used verbatim, for the container case where detection is wrong."""
        banner = startup.format_banner(8080, "0.0.0.0", advertise_host="192.168.1.42")
        self.assertIn("http://192.168.1.42:8080", banner)
        self.assertNotIn("NOTE", banner)

    def test_container_bridge_address_triggers_hint(self) -> None:
        """A Docker-bridge address triggers the PD_ADVERTISE_HOST / --network host hint."""
        self.assertTrue(startup._looks_containerised(["http://172.17.0.2:8080"]))
        self.assertFalse(startup._looks_containerised(["http://192.168.1.42:8080"]))

    def test_http_scheme_by_default(self) -> None:
        """Without TLS the banner and reachable URLs use the plain http scheme."""
        banner = startup.format_banner(8080, "192.168.1.42")
        self.assertIn("http://192.168.1.42:8080", banner)
        self.assertNotIn("https://", banner)

    def test_https_scheme_when_tls_on(self) -> None:
        """With TLS on, the banner advertises https URLs."""
        banner = startup.format_banner(8080, "192.168.1.42", tls=True)
        self.assertIn("https://192.168.1.42:8080", banner)
        self.assertNotIn("http://192.168.1.42:8080", banner)

    def test_self_signed_warning_only_under_tls(self) -> None:
        """The one-time certificate-warning line appears only for a self-signed HTTPS cert."""
        plain = startup.format_banner(8080, "192.168.1.42")
        self.assertNotIn("self-signed", plain)
        secure = startup.format_banner(8080, "192.168.1.42", tls=True, self_signed=True)
        self.assertIn("self-signed", secure)
        self.assertIn("certificate", secure)

    def test_advertise_host_uses_https_and_warning(self) -> None:
        """An advertised host renders https and carries the self-signed warning when TLS is on."""
        banner = startup.format_banner(
            8080, "0.0.0.0", advertise_host="192.168.1.42", tls=True, self_signed=True
        )
        self.assertIn("https://192.168.1.42:8080", banner)
        self.assertIn("self-signed", banner)


if __name__ == "__main__":
    unittest.main()
