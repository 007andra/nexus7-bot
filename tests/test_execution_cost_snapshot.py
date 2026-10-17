"""Audit P0-2/P0-3: one execution-cost truth per candidate; R:R layers agree."""
import asyncio
import math
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import execution_cost as ec
from bot import exchange
from bot import nexus_ai
from bot import nexus_live_cost_calibration as calibration
from bot.operator_loss_policy import validate_technical_geometry


class _BinanceClient:
    def __init__(self, taker="0.0005", fail=False, ticker=None):
        self.calls = []
        self.taker, self.fail = taker, fail
        self.ticker = ticker if ticker is not None else {"bid": 3.9996, "ask": 4.0004}

    async def _get(self, endpoint, params=None, auth=False):
        self.calls.append((endpoint, dict(params or {}), auth))
        if self.fail:
            raise RuntimeError("HTTP 500")
        return {"symbol": params["symbol"], "takerCommissionRate": self.taker,
                "makerCommissionRate": "0.0002"}

    def get_cached_ticker(self, symbol):
        return self.ticker


def _sig(entry=4.0, sl=3.928, tp=4.128, symbol="ATOMUSDT"):
    return SimpleNamespace(symbol=symbol, direction="LONG", entry=entry, sl=sl, tp=tp,
                           _bgx_setup_id=f"{symbol}:LONG:TEST:1")


def _run(coro):
    return asyncio.run(coro)


class BinanceCostTruthTests(unittest.TestCase):
    def setUp(self):
        ec._FEE_CACHE.clear()
        self.p = patch.object(exchange, "EXCHANGE_NAME", "binance")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        ec._FEE_CACHE.clear()

    def test_binance_reads_commission_rate_never_kucoin_endpoint(self):
        client = _BinanceClient()
        snap, reused = _run(ec.snapshot_for(SimpleNamespace(client=client), _sig()))
        self.assertFalse(reused)
        self.assertEqual([c[0] for c in client.calls], ["/fapi/v1/commissionRate"])
        self.assertEqual(client.calls[0][1], {"symbol": "ATOMUSDT"})
        self.assertTrue(client.calls[0][2])
        self.assertEqual(snap.exchange, "binance")
        self.assertEqual(snap.fee_source, "binance_commission_rate")
        self.assertAlmostEqual(snap.taker_fee, 0.0005)
        self.assertAlmostEqual(snap.maker_fee, 0.0002)
        self.assertNotIn("kucoin", snap.log_fields())

    def test_fee_read_failure_uses_conservative_fallback(self):
        snap, _ = _run(ec.snapshot_for(SimpleNamespace(client=_BinanceClient(fail=True)), _sig()))
        self.assertEqual(snap.fee_source, "conservative_fallback")
        self.assertGreaterEqual(snap.taker_fee, ec.LEGACY_CONSERVATIVE_TAKER_FEE)
        self.assertTrue(snap.fallback)

    def test_invalid_commission_uses_fallback(self):
        for bad in ("-1", "0.5", "nan", None):
            ec._FEE_CACHE.clear()
            snap, _ = _run(ec.snapshot_for(SimpleNamespace(client=_BinanceClient(taker=bad)), _sig()))
            self.assertEqual(snap.fee_source, "conservative_fallback", bad)

    def test_missing_ticker_uses_static_symbol_slippage(self):
        client = _BinanceClient(ticker={})
        snap, _ = _run(ec.snapshot_for(SimpleNamespace(client=client), _sig()))
        self.assertEqual(snap.slippage_source, "static_symbol_fallback")
        self.assertAlmostEqual(snap.entry_slippage, ec.static_slippage_rate("ATOMUSDT"))

    def test_stress_cost_never_below_economic_or_static(self):
        snap, _ = _run(ec.snapshot_for(SimpleNamespace(client=_BinanceClient()), _sig()))
        self.assertGreaterEqual(snap.stress_round_trip_cost_fraction, snap.round_trip_cost_fraction)
        self.assertGreaterEqual(snap.stress_round_trip_cost_fraction,
                                ec.static_round_trip_cost_fraction("ATOMUSDT"))

    def test_fee_is_cached_per_exchange_and_symbol(self):
        client = _BinanceClient()
        for _ in range(3):
            _run(ec.build_snapshot(SimpleNamespace(client=client), _sig()))
        self.assertEqual(len(client.calls), 1)


class SnapshotReuseTests(unittest.TestCase):
    def setUp(self):
        ec._FEE_CACHE.clear()

    def test_reused_for_same_candidate_rebuilt_when_entry_changes_or_stale(self):
        client = _BinanceClient()
        engine = SimpleNamespace(client=client)
        sig = _sig()
        with patch.object(exchange, "EXCHANGE_NAME", "binance"):
            first, _ = _run(ec.snapshot_for(engine, sig))
            again, reused = _run(ec.snapshot_for(engine, sig))
            self.assertTrue(reused)
            self.assertIs(first, again)
            sig.entry = 4.01
            moved, reused = _run(ec.snapshot_for(engine, sig))
            self.assertFalse(reused)
            self.assertNotEqual(moved.snapshot_id, first.snapshot_id)
            with patch.object(ec.time, "time", return_value=time.time() + ec.SNAPSHOT_MAX_AGE_S + 1):
                self.assertIsNone(ec.reusable_snapshot(sig))


class RrReconciliationTests(unittest.TestCase):
    """ATOMUSDT production observation: technical 1.36 vs NEXUS 1.54."""

    ENTRY, SL, TP = 4.0, 4.0 * (1 - 0.01797), 4.0 * (1 + 0.0320)

    def test_root_cause_reproduced_from_the_two_old_cost_inputs(self):
        old_technical_cost = 2 * 0.0006 + 2 * 0.0010  # static alt slippage 10 bps
        old_nexus = nexus_ai.expected_value(0.5, self.ENTRY, self.SL, self.TP,
                                            taker_fee=0.0006, slippage=0.00025)
        tech = validate_technical_geometry(self.ENTRY, self.SL, self.TP, "LONG",
                                           old_technical_cost, 1.6)
        self.assertAlmostEqual(tech["estimated_net_rr"], 1.36, delta=0.01)
        self.assertAlmostEqual(old_nexus["rr_net"], 1.54, delta=0.01)

    def test_same_snapshot_gives_same_net_rr_in_both_layers(self):
        ec._FEE_CACHE.clear()
        with patch.object(exchange, "EXCHANGE_NAME", "binance"):
            snap, _ = _run(ec.snapshot_for(SimpleNamespace(client=_BinanceClient()),
                                           _sig(self.ENTRY, self.SL, self.TP)))
        tech = validate_technical_geometry(self.ENTRY, self.SL, self.TP, "LONG",
                                           snap.round_trip_cost_fraction, 1.6)
        nexus = nexus_ai.expected_value(0.5, self.ENTRY, self.SL, self.TP,
                                        taker_fee=snap.taker_fee, slippage=snap.one_way_slippage)
        shared = ec.rr_breakdown(self.ENTRY, self.SL, self.TP, snap.round_trip_cost_fraction)
        self.assertTrue(math.isclose(nexus["cost"], snap.round_trip_cost_fraction, abs_tol=1e-6))
        self.assertAlmostEqual(tech["estimated_net_rr"], shared["net_rr"], places=12)
        # nexus_ai rounds rr_net to 3 decimals.
        self.assertAlmostEqual(nexus["rr_net"], shared["net_rr"], delta=5e-4)


class _Log:
    def __init__(self):
        self.lines = []

    def __getattr__(self, _name):
        def emit(msg, *args, **_kw):
            self.lines.append(msg % args if args else msg)
        return emit


class EndToEndTechnicalThenNexusTests(unittest.IsolatedAsyncioTestCase):
    async def test_technical_policy_and_nexus_share_one_snapshot(self):
        ec._FEE_CACHE.clear()
        from bot import operator_loss_policy

        # Stand-in for the nexus_ai module; calibration.install replaces its
        # expected_value with the calibrated wrapper, looked up at call time.
        nexus_stub = SimpleNamespace(expected_value=nexus_ai.expected_value)
        seen = {}

        class Engine:
            paper_trade = False
            pilot = SimpleNamespace(enabled=True)

            def __init__(self):
                self.client = _BinanceClient()

            async def _check_stagnation_and_invalidation(self):
                return None

            async def _nexus_validate(self, sig):
                return nexus_stub.expected_value(0.5, sig.entry, sig.sl, sig.tp)

            async def _open(self, sig):
                seen["nexus"] = await self._nexus_validate(sig)
                return "done"

        log = _Log()
        with patch.object(exchange, "EXCHANGE_NAME", "binance"):
            calibration.install(Engine, nexus_stub, log)
            operator_loss_policy.install(Engine, log)
            engine = Engine()
            sig = _sig(4.0, 4.0 * (1 - 0.01797), 4.0 * (1 + 0.0320))
            result = await engine._open(sig)

        self.assertEqual(result, "done")
        self.assertEqual(len(engine.client.calls), 1, "one fee read per candidate")
        snap = ec.attached_snapshot(sig)
        self.assertIsNotNone(snap)
        self.assertEqual(seen["nexus"]["cost_snapshot_id"], snap.snapshot_id)
        self.assertEqual(seen["nexus"]["candidate_id"], "ATOMUSDT:LONG:TEST:1")
        tech_line = [l for l in log.lines if "[TECHNICAL_STOP_POLICY]" in l and "result=PASS" in l][0]
        nexus_line = [l for l in log.lines if "[NEXUS_COST]" in l][0]
        self.assertIn(f"cost_snapshot_id={snap.snapshot_id}", tech_line)
        self.assertIn(f"cost_snapshot_id={snap.snapshot_id}", nexus_line)
        self.assertIn("cost_snapshot_reused=false", tech_line)
        tech_rr = float(tech_line.split("estimated_net_rr=")[1].split()[0])
        self.assertAlmostEqual(tech_rr, seen["nexus"]["rr_net"], delta=5e-4)


if __name__ == "__main__":
    unittest.main()
