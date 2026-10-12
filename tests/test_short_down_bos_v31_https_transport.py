"""Offline contract tests for the standalone V3.1 HTTPS GET adapter."""
import json
import unittest

from bot.short_down_bos_v31_evidence_collector import CollectorRefused
from bot.short_down_bos_v31_https_transport import (
    MAX_RESPONSE_BYTES, ReadOnlyResearchHTTPS,
)


class FakeContent:
    def __init__(self, body):
        self.body = body

    async def read(self, limit):
        return self.body[:limit]


class FakeResponse:
    def __init__(self, body=b'{"serverTime":123}', status=200, length=None):
        self.content = FakeContent(body)
        self.status = status
        self.content_length = len(body) if length is None else length

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, response=None):
        self.response = FakeResponse() if response is None else response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class ReadOnlyHttpsTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_only_explicit_https_get_path(self):
        session = FakeSession()
        transport = ReadOnlyResearchHTTPS(session)
        out = await transport(
            "fapi.binance.com", "/fapi/v1/time", {}, {})
        self.assertEqual(out, {"serverTime": 123})
        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://fapi.binance.com/fapi/v1/time")
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertIs(kwargs["ssl"], True)
        self.assertEqual(kwargs["timeout"].total, 7)
        self.assertFalse(hasattr(transport, "post"))

    async def test_refuses_host_and_path_outside_allowlist_before_io(self):
        session = FakeSession()
        transport = ReadOnlyResearchHTTPS(session)
        for host, path in (
            ("evil.example", "/fapi/v1/time"),
            ("fapi.binance.com", "/fapi/v1/order"),
            ("api.binance.com", "/sapi/v1/capital/withdraw/apply"),
        ):
            with self.subTest(host=host, path=path):
                with self.assertRaisesRegex(CollectorRefused, "ENDPOINT_NOT_ALLOWLISTED"):
                    await transport(host, path, {"timestamp": 123}, {})
        self.assertEqual(session.calls, [])

    async def test_redirect_and_upstream_error_are_not_followed_or_echoed(self):
        for status in (302, 403, 429, 500):
            with self.subTest(status=status):
                response = FakeResponse(body=b'{"secret":"sensitive"}', status=status)
                session = FakeSession(response)
                with self.assertRaises(CollectorRefused) as cm:
                    await ReadOnlyResearchHTTPS(session)(
                        "fapi.binance.com", "/fapi/v1/time", {}, {})
                self.assertEqual(str(cm.exception), "RESEARCH_HTTP_NON_200")
                self.assertIs(session.calls[0][1]["allow_redirects"], False)

    async def test_oversized_body_and_untrusted_content_length_are_refused(self):
        for response in (
            FakeResponse(body=b"{}", length=MAX_RESPONSE_BYTES + 1),
            FakeResponse(body=b"x" * (MAX_RESPONSE_BYTES + 1),
                         length=MAX_RESPONSE_BYTES),
        ):
            with self.assertRaisesRegex(CollectorRefused, "RESEARCH_HTTP_RESPONSE_TOO_LARGE"):
                await ReadOnlyResearchHTTPS(FakeSession(response))(
                    "fapi.binance.com", "/fapi/v1/time", {}, {})

    async def test_malformed_json_is_not_passed_through(self):
        response = FakeResponse(body=b'not json')
        with self.assertRaisesRegex(CollectorRefused, "RESEARCH_HTTP_JSON_INVALID"):
            await ReadOnlyResearchHTTPS(FakeSession(response))(
                "fapi.binance.com", "/fapi/v1/time", {}, {})

    async def test_transport_errors_do_not_leak_signed_url(self):
        class FailingSession:
            def get(self, *args, **kwargs):
                raise RuntimeError("confidential signature and account details")
        with self.assertRaises(CollectorRefused) as cm:
            await ReadOnlyResearchHTTPS(FailingSession())(
                "fapi.binance.com", "/fapi/v1/commissionRate",
                {"signature": "private-value"}, {"X-MBX-APIKEY": "private-key"})
        self.assertEqual(str(cm.exception), "RESEARCH_HTTP_TRANSPORT_FAILURE")
        self.assertIsNone(cm.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
