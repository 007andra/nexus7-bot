"""Offline regressions for the one-shot V3.1 read-only key preflight.

Fake transport only: tests must never access Binance or read any real secrets.
"""
import io
import unittest
from contextlib import redirect_stdout

from bot.short_down_bos_v31_api_preflight import (
    DISALLOWED_RUNTIME_VARS, dry_run, isolation_preflight, main,
    probe_capabilities,
)
from bot.short_down_bos_v31_evidence_collector import (
    FORBIDDEN_PERMISSIONS, CollectorRefused,
)

ENV = {
    "V31_RESEARCH_BINANCE_API_KEY": "offline-research-only",
    "V31_RESEARCH_BINANCE_API_SECRET": "offline-test-secret",
}
PERMISSIONS = {"enableReading": True, **{field: False for field in FORBIDDEN_PERMISSIONS}}


class OfflineTransport:
    def __init__(self, restrictions=None, deny=None):
        self.restrictions = dict(PERMISSIONS if restrictions is None else restrictions)
        self.deny = deny
        self.paths = []

    async def __call__(self, host, path, params, headers):
        self.paths.append(path)
        if path == self.deny:
            raise CollectorRefused("RESEARCH_HTTP_NON_200")
        if path == "/sapi/v1/account/apiRestrictions":
            return self.restrictions
        if path == "/fapi/v1/time":
            return {"serverTime": 1791834000000}
        if path == "/fapi/v1/exchangeInfo":
            return {"symbols": [{
                "symbol": "BTCUSDT", "status": "TRADING",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"}
                ]
            }]}
        if path == "/fapi/v1/ticker/bookTicker":
            return {"symbol": "BTCUSDT", "time": 1791834000000,
                    "bidPrice": "100.0", "askPrice": "100.1"}
        if path == "/fapi/v1/commissionRate":
            return {"symbol": "BTCUSDT", "takerCommissionRate": "0.0005"}
        if path == "/fapi/v1/leverageBracket":
            return [{"symbol": "BTCUSDT", "brackets": [
                {"notionalFloor": "0", "notionalCap": "100000",
                 "maintMarginRatio": "0.004"}
            ]}]
        if path == "/fapi/v1/markPriceKlines":
            return [[1791833990000, "100", "101", "99", "100", "0",
                     1791834049999]]
        if path == "/fapi/v1/fundingRate":
            return []
        raise AssertionError("Unexpected endpoint: " + path)


class GateTests(unittest.TestCase):
    def test_dry_run_reports_no_access_and_no_credential_loading(self):
        with io.StringIO() as buf, redirect_stdout(buf):
            self.assertEqual(main([]), 0)
            self.assertIn("DRY_RUN_ONLY", buf.getvalue())
            self.assertNotIn("offline-test-secret", buf.getvalue())
        self.assertEqual(dry_run()["network_calls"], 0)

    def test_probe_requires_explicit_confirmation(self):
        with io.StringIO() as buf, redirect_stdout(buf):
            self.assertEqual(main(["--probe"]), 2)
            self.assertIn("EXPLICIT_READONLY_PROBE_APPROVAL_REQUIRED", buf.getvalue())

    def test_production_env_aliases_are_rejected(self):
        for alias in DISALLOWED_RUNTIME_VARS:
            with self.subTest(alias=alias):
                with self.assertRaisesRegex(CollectorRefused, "LIVE_RUNTIME_ENVIRONMENT_PRESENT"):
                    isolation_preflight({**ENV, alias: "present"}, "BTCUSDT")

    def test_invalid_symbol_or_missing_research_key_is_refused(self):
        for symbol in ("", "btcUSDT", "BTCUSDT/../../order", "BTCBUSD"):
            with self.subTest(symbol=symbol):
                with self.assertRaisesRegex(CollectorRefused, "RESEARCH_SYMBOL_INVALID"):
                    isolation_preflight(ENV, symbol)
        with self.assertRaisesRegex(CollectorRefused, "RESEARCH_CREDENTIALS_MISSING"):
            isolation_preflight({}, "BTCUSDT")


class AsyncProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_read_endpoints_and_report_without_private_data(self):
        fake = OfflineTransport()
        report = await probe_capabilities(fake, ENV, "BTCUSDT")
        self.assertEqual(report["status"], "READ_ONLY_API_CAPABILITIES_PASS")
        self.assertTrue(report["read_only_permissions_verified"])
        self.assertFalse(report["candidate_enrollment_started"])
        self.assertFalse(report["live_allowed"])
        self.assertEqual(fake.paths, [
            "/sapi/v1/account/apiRestrictions",
            "/fapi/v1/time",
            "/fapi/v1/exchangeInfo",
            "/fapi/v1/ticker/bookTicker",
            "/fapi/v1/commissionRate",
            "/fapi/v1/leverageBracket",
            "/fapi/v1/markPriceKlines",
            "/fapi/v1/fundingRate",
        ])
        self.assertNotIn("offline-test-secret", str(report))
        self.assertNotIn("BTCUSDT", str(report))
        self.assertNotIn("0.0005", str(report))

    async def test_write_permission_fails_before_any_market_read(self):
        fake = OfflineTransport(restrictions={**PERMISSIONS, "enableFutures": True})
        with self.assertRaisesRegex(CollectorRefused, "RESEARCH_KEY_HAS_WRITE_PERMISSION"):
            await probe_capabilities(fake, ENV, "BTCUSDT")
        self.assertEqual(fake.paths, ["/sapi/v1/account/apiRestrictions"])

    async def test_missing_key_permission_field_is_unknown_not_false(self):
        p = dict(PERMISSIONS)
        del p["enableFutures"]
        fake = OfflineTransport(restrictions=p)
        with self.assertRaisesRegex(CollectorRefused, "RESEARCH_KEY_HAS_WRITE_PERMISSION"):
            await probe_capabilities(fake, ENV, "BTCUSDT")
        self.assertEqual(fake.paths, ["/sapi/v1/account/apiRestrictions"])

    async def test_signed_futures_fee_unavailable_is_not_proven(self):
        fake = OfflineTransport(deny="/fapi/v1/commissionRate")
        with self.assertRaisesRegex(CollectorRefused, "RESEARCH_HTTP_NON_200"):
            await probe_capabilities(fake, ENV, "BTCUSDT")
        self.assertNotIn("/fapi/v1/leverageBracket", fake.paths)
        self.assertNotIn("/fapi/v1/fundingRate", fake.paths)


if __name__ == "__main__":
    unittest.main()
