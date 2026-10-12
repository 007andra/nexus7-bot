"""V3.1 read-only collector isolation and evidence shape. Fake transport, no network."""
import unittest
from decimal import Decimal as D

from bot import short_down_bos_v31_evidence_collector as col
from bot import short_down_bos_v31_net_evaluator as ev

M15 = 15 * 60_000
ENTRY = 1_791_900_000_000 - (1_791_900_000_000 % M15) + 1_000
SYM = "AAAUSDT"
ENV = {col.RESEARCH_KEY_ENV: "research-key", col.RESEARCH_SECRET_ENV: "research-secret",
       col.LIVE_KEY_ENV: "live-key"}
READ_ONLY = {"enableReading": True, **{key: False for key in col.FORBIDDEN_PERMISSIONS}}


class FakeTransport:
    def __init__(self, restrictions=READ_ONLY, clock=ENTRY, book_delay_ms=0):
        self.calls = []
        self.restrictions = dict(restrictions)
        self.clock = clock
        self.book_delay_ms = book_delay_ms

    async def __call__(self, host, path, query, headers):
        self.calls.append((host, path, dict(query), dict(headers)))
        if path == "/sapi/v1/account/apiRestrictions":
            return self.restrictions
        if path == "/fapi/v1/time":
            return {"serverTime": self.clock}
        if path == "/fapi/v1/ticker/bookTicker":
            return {"symbol": SYM, "bidPrice": "99.99", "askPrice": "100.01",
                    "time": self.clock - self.book_delay_ms}
        if path == "/fapi/v1/exchangeInfo":
            return {"symbols": [{"symbol": SYM, "status": "TRADING", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.1"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}
        if path == "/fapi/v1/commissionRate":
            return {"symbol": SYM, "makerCommissionRate": "0.0002", "takerCommissionRate": "0.0005"}
        if path == "/fapi/v1/leverageBracket":
            return [{"symbol": SYM, "brackets": [
                {"notionalFloor": "0", "notionalCap": "5000", "maintMarginRatio": "0.004"}]}]
        if path == "/fapi/v1/markPriceKlines":
            first = query["startTime"]
            return [[first + k * M15, "100", "100.5", "99.5", "100", "0", first + (k + 1) * M15 - 1]
                    for k in range(17)]
        if path == "/fapi/v1/fundingRate":
            return [{"symbol": SYM, "fundingTime": ENTRY + 2 * 3_600_000, "fundingRate": "0.0001",
                     "markPrice": "100", "rateType": "Regular"}]
        raise AssertionError(path)


PROTECTION = {"stop": "103", "tp_order_type": "TAKE_PROFIT_MARKET", "trailing": None,
              "working_type": "MARK_PRICE", "take_profits": [{"price": "94", "fraction": "1"}]}
CANDIDATE = {"candidate_id": "c0", "symbol": SYM, "captured_ms": ENTRY - 500,
             "entry": 100.0, "stop": 103.0, "target": 94.0}


class IsolationTests(unittest.IsolatedAsyncioTestCase):
    def test_refuses_missing_or_live_credentials(self):
        with self.assertRaisesRegex(col.CollectorRefused, "RESEARCH_CREDENTIALS_MISSING"):
            col.ReadOnlyBinanceResearchClient(FakeTransport(), environ={})
        with self.assertRaisesRegex(col.CollectorRefused, "LIVE_CREDENTIAL_REUSE"):
            col.ReadOnlyBinanceResearchClient(FakeTransport(), environ={
                **ENV, col.LIVE_KEY_ENV: "research-key"})

    async def test_market_reads_require_read_only_preflight(self):
        client = col.ReadOnlyBinanceResearchClient(FakeTransport(), environ=ENV)
        with self.assertRaisesRegex(col.CollectorRefused, "READ_ONLY_PREFLIGHT_REQUIRED"):
            await client.get(col.FAPI, "/fapi/v1/time")

    async def test_any_write_permission_on_research_key_is_refused(self):
        for perm in ("enableFutures", "enableWithdrawals", "enableInternalTransfer",
                     "enableSpotAndMarginTrading", "permitsUniversalTransfer",
                     "enableFixApiTrade"):
            with self.subTest(perm=perm):
                client = col.ReadOnlyBinanceResearchClient(
                    FakeTransport({**READ_ONLY, perm: True}), environ=ENV)
                with self.assertRaisesRegex(col.CollectorRefused, "WRITE_PERMISSION"):
                    await client.preflight()
                self.assertFalse(client.verified_read_only)

    async def test_missing_permission_fields_refuse_readonly_approval(self):
        # An omitted capability flag is UNKNOWN, not proof it is disabled.
        for perm in col.FORBIDDEN_PERMISSIONS:
            with self.subTest(perm=perm):
                restrictions = dict(READ_ONLY)
                restrictions.pop(perm)
                client = col.ReadOnlyBinanceResearchClient(
                    FakeTransport(restrictions), environ=ENV)
                with self.assertRaisesRegex(col.CollectorRefused, "WRITE_PERMISSION"):
                    await client.preflight()
                self.assertFalse(client.verified_read_only)

    async def test_order_and_account_endpoints_are_not_reachable(self):
        client = col.ReadOnlyBinanceResearchClient(FakeTransport(), environ=ENV)
        await client.preflight()
        for path in ("/fapi/v1/order", "/fapi/v1/batchOrders", "/fapi/v1/leverage",
                     "/fapi/v1/marginType", "/fapi/v3/account", "/fapi/v1/algoOrder"):
            with self.subTest(path=path), self.assertRaisesRegex(
                    col.CollectorRefused, "ENDPOINT_NOT_ALLOWLISTED"):
                await client.get(col.FAPI, path)

    async def test_signed_reads_carry_signature_unsigned_do_not(self):
        transport = FakeTransport()
        client = col.ReadOnlyBinanceResearchClient(transport, environ=ENV)
        await client.preflight()
        await client.get(col.FAPI, "/fapi/v1/time")
        await client.get(col.FAPI, "/fapi/v1/commissionRate", {"symbol": SYM}, now_ms=ENTRY)
        _, _, q_time, h_time = transport.calls[1]
        _, _, q_fee, h_fee = transport.calls[2]
        self.assertNotIn("signature", q_time)
        self.assertEqual(h_time, {})
        self.assertEqual(len(q_fee["signature"]), 64)
        self.assertEqual(h_fee["X-MBX-APIKEY"], "research-key")


class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def _collect(self):
        transport = FakeTransport()
        client = col.ReadOnlyBinanceResearchClient(transport, environ=ENV)
        await client.preflight()
        e = await col.capture_entry(client, CANDIDATE, reference_notional="100",
                                    leverage=3, slippage_fraction="0.0005", protection=PROTECTION)
        for h in (60, 240):
            transport.clock = ENTRY + h * 60_000 - 300
            await col.capture_horizon(client, e, h)
        transport.clock = ENTRY + 240 * 60_000 + col.BAR_MS
        await col.capture_path(client, e)
        return e

    async def test_collected_evidence_is_accepted_by_the_evaluator(self):
        e = await self._collect()
        self.assertEqual(e["filters"]["step_size"], "0.1")  # MARKET_LOT_SIZE, not LOT_SIZE
        self.assertEqual(e["quantity"], "1.0")
        out = ev.evaluate_candidate(CANDIDATE, e, as_of_ms=ENTRY + 10**8)
        self.assertEqual(out["status"], ev.PROVEN, out)
        self.assertGreater(out["net_240"], out["net_60"])  # funding received after 60m

    async def test_entry_metadata_precedes_the_timestamped_quote(self):
        transport = FakeTransport()
        client = col.ReadOnlyBinanceResearchClient(transport, environ=ENV)
        await client.preflight()
        evidence = await col.capture_entry(
            client, CANDIDATE, reference_notional="100", leverage=3,
            slippage_fraction="0.0005", protection=PROTECTION)
        paths = [call[1] for call in transport.calls]
        quote = paths.index("/fapi/v1/ticker/bookTicker")
        self.assertLess(paths.index("/fapi/v1/commissionRate"), quote)
        self.assertLess(paths.index("/fapi/v1/exchangeInfo"), quote)
        self.assertLess(paths.index("/fapi/v1/leverageBracket"), quote)
        self.assertEqual(paths[quote + 1], "/fapi/v1/time")
        self.assertEqual(evidence["signal_captured_ms"], CANDIDATE["captured_ms"])
        self.assertEqual(evidence["entry_book_read_ms"], ENTRY)

    async def test_pre_signal_and_stale_quotes_fail_closed(self):
        cases = [
            (FakeTransport(), {**CANDIDATE, "captured_ms": ENTRY + 1},
             "ENTRY_BOOK_PRECEDES_SIGNAL"),
            (FakeTransport(book_delay_ms=2_001), CANDIDATE, "ENTRY_BOOK_NOT_FRESH"),
            (FakeTransport(), {k: v for k, v in CANDIDATE.items()
                               if k != "captured_ms"}, "CANDIDATE_TIMESTAMP_INVALID"),
        ]
        for transport, candidate, reason in cases:
            with self.subTest(reason=reason):
                client = col.ReadOnlyBinanceResearchClient(transport, environ=ENV)
                await client.preflight()
                with self.assertRaisesRegex(col.CollectorRefused, reason):
                    await col.capture_entry(
                        client, candidate, reference_notional="100", leverage=3,
                        slippage_fraction="0.0005", protection=PROTECTION)

    async def test_canonical_epoch_seconds_are_accepted_as_signal_provenance(self):
        transport = FakeTransport()
        client = col.ReadOnlyBinanceResearchClient(transport, environ=ENV)
        await client.preflight()
        candidate = {k: v for k, v in CANDIDATE.items() if k != "captured_ms"}
        candidate["captured_epoch"] = (ENTRY - 500) / 1000
        evidence = await col.capture_entry(
            client, candidate, reference_notional="100", leverage=3,
            slippage_fraction="0.0005", protection=PROTECTION)
        self.assertEqual(evidence["signal_captured_ms"], ENTRY - 500)

    async def test_late_horizon_quote_is_never_accepted(self):
        transport = FakeTransport()
        client = col.ReadOnlyBinanceResearchClient(transport, environ=ENV)
        await client.preflight()
        e = await col.capture_entry(client, CANDIDATE, reference_notional="100",
                                    leverage=3, slippage_fraction="0.0005", protection=PROTECTION)
        transport.clock = ENTRY + 60 * 60_000 + 1
        await col.capture_horizon(client, e, 60)
        transport.clock = ENTRY + 240 * 60_000 - 1
        await col.capture_horizon(client, e, 240)
        transport.clock = ENTRY + 240 * 60_000 + col.BAR_MS
        await col.capture_path(client, e)
        out = ev.evaluate_candidate(CANDIDATE, e, as_of_ms=ENTRY + 10**8)
        self.assertEqual(out, {"status": ev.NOT_PROVEN, "reason": "EXIT_QUOTE_60_FUTURE"})

    async def test_contract_price_protection_is_refused_with_mark_bars(self):
        e = await self._collect()
        e["protection"]["working_type"] = "CONTRACT_PRICE"
        out = ev.evaluate_candidate(CANDIDATE, e, as_of_ms=ENTRY + 10**8)
        self.assertEqual(out["reason"], "TRIGGER_PRICE_SOURCE_MISMATCH")

    async def test_path_requires_matured_last_mark_bar(self):
        transport = FakeTransport()
        client = col.ReadOnlyBinanceResearchClient(transport, environ=ENV)
        await client.preflight()
        e = await col.capture_entry(
            client, CANDIDATE, reference_notional="100", leverage=3,
            slippage_fraction="0.0005", protection=PROTECTION,
        )
        end = e["entry_ms"] + 240 * 60_000
        closed_at = ((end + col.BAR_MS - 1) // col.BAR_MS) * col.BAR_MS
        transport.clock = closed_at - 1
        with self.assertRaisesRegex(col.CollectorRefused, "PATH_HORIZON_NOT_MATURED"):
            await col.capture_path(client, e)
        self.assertNotIn("bars", e)
        self.assertNotIn("funding", e)
        self.assertNotIn("/fapi/v1/fundingRate", [v[1] for v in transport.calls])
        self.assertNotIn("/fapi/v1/markPriceKlines", [v[1] for v in transport.calls])

        transport.clock = closed_at
        await col.capture_path(client, e)
        self.assertEqual(e["path_observed_ms"], closed_at)
        self.assertTrue(e["funding"]["coverage"]["complete"])

    def test_full_funding_page_has_no_coverage_proof(self):
        page = [{"symbol": SYM, "fundingTime": i, "fundingRate": "0", "markPrice": "1"}
                for i in range(col.FUNDING_PAGE_LIMIT)]
        with self.assertRaisesRegex(col.CollectorRefused, "FUNDING_PAGE_NOT_COMPLETE"):
            col.normalize_funding(page, SYM, 0, 10)

    def test_missing_rate_type_stays_absent(self):
        out = col.normalize_funding([{"symbol": SYM, "fundingTime": 5, "fundingRate": "0.0001",
                                      "markPrice": "100"}], SYM, 0, 10)
        self.assertIsNone(out["events"][0]["rate_type"])  # evaluator -> NOT_PROVEN

    def test_isolated_margin_never_exceeds_initial_margin_of_fill(self):
        m = D(col.isolated_margin("1.0", "99.99", 3, "0.0005"))
        self.assertLessEqual(m, D("1.0") * D("99.94") / 3)


if __name__ == "__main__":
    unittest.main()
