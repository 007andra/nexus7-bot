"""Q-01C — protective stops only tighten (INV-STOP-MONOTONIC-001), offline.

Pure helper ``bot.stop_monotonic.decide_stop`` plus the real break-even /
trailing call sites (``durable_partial_exit``, core ``_manage_partial_tp`` and
``_apply_trailing_stops``, PAPER ``partial_tp_execution_hardening``) and the
exchange boundary floor in ``native_stop_repair.set_stops``.
"""
import asyncio
import os
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("PAPER_TRADE", "true")

from bot import engine as core  # noqa: E402
from bot import trailing_safety_hardening as ts  # noqa: E402
from bot.config import cfg  # noqa: E402
from bot.stop_monotonic import (  # noqa: E402
    BETTER, EQUAL_AFTER_ROUNDING, FIRST_PROTECTION, INVALID_TRIGGER_SIDE,
    UNKNOWN_CURRENT_PROTECTION, WORSE, decide_stop,
)
from bot.strategy import Signal  # noqa: E402


class Pos(core.Position):
    pass


ts.install(Pos, cfg, SimpleNamespace(warning=lambda *a, **k: None))
INFO = dict(multiplier=.001, lotSize=1, minQty=1, minNotional=0, tickSize=0.01)


def _pos(direction="LONG", stop=None, qty=1.0):
    e = 100.0
    isl = 98.0 if direction == "LONG" else 102.0
    tp = 104.0 if direction == "LONG" else 96.0
    pos = Pos(Signal("BTCUSDT", direction, e, isl, tp, 0.8, "t", 80), qty)
    pos._forensic_lineage = {"order_id": "opening-1"}
    if stop is not None:
        pos.sl = pos.trailing_sl = stop
    return pos


class PureHelperTests(unittest.TestCase):
    def test_a_c_long_be_is_a_floor(self):
        self.assertEqual(decide_stop("LONG", 101.5, 100.0, market_price=102.1)[:3], (101.5, False, WORSE))
        self.assertEqual(decide_stop("LONG", 98.0, 100.0, market_price=102.1)[:3], (100.0, True, BETTER))

    def test_b_d_short_be_is_a_floor(self):
        self.assertEqual(decide_stop("SHORT", 98.5, 100.0, market_price=97.9)[:3], (98.5, False, WORSE))
        self.assertEqual(decide_stop("SHORT", 102.0, 100.0, market_price=97.9)[:3], (100.0, True, BETTER))

    def test_e_equal_is_noop(self):
        self.assertEqual(decide_stop("LONG", 100.0, 100.0).reason, EQUAL_AFTER_ROUNDING)

    def test_f_g_trailing_worse_noop_better_replaces(self):
        self.assertEqual(decide_stop("LONG", 101.5, 101.25).reason, WORSE)
        self.assertEqual(decide_stop("LONG", 101.5, 102.0).reason, BETTER)
        self.assertEqual(decide_stop("SHORT", 98.5, 98.75).reason, WORSE)
        self.assertEqual(decide_stop("SHORT", 98.5, 98.0).reason, BETTER)
        self.assertEqual(decide_stop("LONG", 102.0, 101.75).reason, WORSE)
        self.assertEqual(decide_stop("SHORT", 98.0, 98.25).reason, WORSE)

    def test_j_sequence_is_monotonic(self):
        for direction, seq, expected in (
            ("LONG", [98, 100, 101, 100.5, 102, 101.9], [98, 100, 101, 101, 102, 102]),
            ("SHORT", [102, 100, 99, 99.5, 98, 98.1], [102, 100, 99, 99, 98, 98]),
        ):
            current, effective = None, []
            for candidate in seq:
                current = decide_stop(direction, current, candidate).selected
                effective.append(current)
            self.assertEqual(effective, expected)

    def test_k_tick_equality_is_noop(self):
        d = decide_stop("LONG", 101.501, 101.504, tick_size=0.01)
        self.assertEqual((d.replace, d.reason), (False, EQUAL_AFTER_ROUNDING))
        self.assertEqual(decide_stop("LONG", 101.501, 101.509, tick_size=0.01).reason, BETTER)

    def test_l_first_protection_allowed(self):
        self.assertEqual(decide_stop("LONG", None, 98.0, market_price=100)[:3], (98.0, True, FIRST_PROTECTION))
        self.assertEqual(decide_stop("SHORT", 0, 102.0, market_price=100).reason, FIRST_PROTECTION)

    def test_m_invalid_trigger_side_keeps_old(self):
        self.assertEqual(decide_stop("LONG", 101.0, 102.5, market_price=102.0)[:3],
                         (101.0, False, INVALID_TRIGGER_SIDE))
        self.assertEqual(decide_stop("SHORT", 99.0, 97.5, market_price=98.0).reason, INVALID_TRIGGER_SIDE)

    def test_unknown_current_never_assumed_improvement(self):
        self.assertEqual(decide_stop("LONG", None, 100.0, current_known=False).reason,
                         UNKNOWN_CURRENT_PROTECTION)

    def test_property_effective_stop_is_running_extreme(self):
        rng = random.Random(1003)
        for _ in range(500):
            direction = rng.choice(["LONG", "SHORT"])
            tick = rng.choice([None, 0.01, 0.5, 0.0001])
            current, installed, effective = None, [], []
            for _ in range(rng.randint(1, 25)):
                candidate = rng.uniform(50, 150)
                d = decide_stop(direction, current, candidate, tick_size=tick)
                if d.replace:
                    installed.append(d.selected)
                current = d.selected
                effective.append(current)
            pick = max if direction == "LONG" else min
            self.assertEqual(current, pick(installed))
            pairs = list(zip(effective, effective[1:]))
            self.assertTrue(all((b >= a) if direction == "LONG" else (b <= a) for a, b in pairs))


class _Harness(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = {}

        async def load(key, strict=False):
            return self.store.get(key)

        async def save(key, value, strict=False):
            self.store[key] = value
            return True
        for name, fn in (("load_key_value", load), ("save_key_value", save)):
            p = patch(f"bot.durable_partial_exit.db.{name}", AsyncMock(side_effect=fn))
            p.start()
            self.addCleanup(p.stop)
        self.client = SimpleNamespace(
            build_client_oid=Mock(return_value="oid"),
            place_order=AsyncMock(return_value={"orderId": "p1"}),
            get_order_by_client_oid=AsyncMock(return_value={}),
            wait_for_fill=AsyncMock(return_value={"filled": True}),
            get_positions=AsyncMock(return_value=[dict(symbol="BTCUSDT", size=.5, sizeUnit="BASE_ASSET")]),
            set_sl=AsyncMock(return_value=True), _instruments={"BTCUSDT": INFO})
        self.engine = SimpleNamespace(client=self.client, instruments={"BTCUSDT": INFO},
                                      _sync_positions=AsyncMock(), _unprotected_symbols=set())

    async def _partial(self, pos, price):
        from bot.durable_partial_exit import check
        self.engine.positions = {"BTCUSDT": pos}
        pos.update_pnl(price)
        await check(self.engine)


class BreakEvenLifecycleTests(_Harness):
    async def test_h_long_partial_keeps_trailed_stop(self):
        pos = _pos("LONG", stop=101.5)
        await self._partial(pos, 102.1)
        self.assertEqual((pos.sl, pos.trailing_sl, pos.qty, pos.tp1_hit), (101.5, 101.5, .5, True))
        self.client.set_sl.assert_not_awaited()
        self.assertNotIn("BTCUSDT", self.engine._unprotected_symbols)

    async def test_h_short_partial_keeps_trailed_stop(self):
        pos = _pos("SHORT", stop=98.5)
        await self._partial(pos, 97.9)
        self.assertEqual((pos.sl, pos.trailing_sl), (98.5, 98.5))
        self.client.set_sl.assert_not_awaited()

    async def test_c_d_partial_raises_bad_stop_to_be(self):
        for direction, price in (("LONG", 102.1), ("SHORT", 97.9)):
            self.store.clear()
            self.client.set_sl.reset_mock()
            pos = _pos(direction)
            await self._partial(pos, price)
            self.client.set_sl.assert_awaited_once_with("BTCUSDT", 100.0)
            self.assertEqual(pos.sl, 100.0)

    async def test_n_o_partial_fill_and_duplicate_events_zero_churn(self):
        self.client.get_positions.return_value = [dict(symbol="BTCUSDT", size=.7, sizeUnit="BASE_ASSET")]
        pos = _pos("LONG")
        await self._partial(pos, 102.1)           # only 30 % filled
        self.assertEqual((pos.qty, pos.sl), (.7, 100.0))
        for _ in range(3):
            await self._partial(pos, 102.1)
        self.client.set_sl.assert_awaited_once()
        self.assertEqual(self.client.place_order.await_count, 1)

    async def test_be_invalid_side_keeps_stop_and_retries_without_exchange_call(self):
        self.client.set_sl = AsyncMock(return_value=False)     # BE not confirmed
        pos = _pos("LONG")
        await self._partial(pos, 102.1)
        self.assertEqual(pos.sl, 98.0)
        self.client.set_sl = AsyncMock(return_value=True)
        await self._partial(pos, 99.5)                          # BE now invalid side
        self.client.set_sl.assert_not_awaited()
        self.assertEqual(pos.sl, 98.0, "old stop kept")
        self.assertIn("BTCUSDT", self.engine._unprotected_symbols, "BE still owed: retry")

    async def test_i_same_cycle_be_and_trailing_converge_to_best(self):
        # Order 1: BE then trailing.
        pos = _pos("LONG")
        await self._partial(pos, 102.1)           # BE -> 100
        pos.peak_price = 103.0
        pos.update_pnl(102.9)
        engine = core.TradingEngine.__new__(core.TradingEngine)
        engine.client = SimpleNamespace(set_sl=AsyncMock(return_value=True))
        engine.paper_trade, engine._durable_state_enforced = False, False
        engine.instruments = {"BTCUSDT": INFO}
        engine.positions = {"BTCUSDT": pos}
        await core.TradingEngine._apply_trailing_stops(engine)
        self.assertEqual(pos.sl, 102.25)
        # Order 2: trailing first, then BE from a later partial.
        self.store.clear()
        self.client.set_sl.reset_mock()
        pos2 = _pos("LONG", stop=101.5)
        await self._partial(pos2, 102.1)
        self.assertEqual(pos2.sl, 101.5)
        self.client.set_sl.assert_not_awaited()

    async def test_concurrent_be_and_trailing_never_end_at_be(self):
        pos = _pos("LONG")
        engine = core.TradingEngine.__new__(core.TradingEngine)
        applied = []

        async def slow_set_sl(symbol, sl):
            await asyncio.sleep(0)
            applied.append(sl)
            return True
        engine.client = SimpleNamespace(set_sl=AsyncMock(side_effect=slow_set_sl))
        engine.paper_trade, engine._durable_state_enforced = False, False
        engine.instruments = {"BTCUSDT": INFO}
        engine.positions = {"BTCUSDT": pos}
        self.client.set_sl = AsyncMock(side_effect=slow_set_sl)
        self.engine.positions = {"BTCUSDT": pos}
        pos.peak_price = 102.0
        pos.update_pnl(102.1)
        from bot.durable_partial_exit import check
        await asyncio.gather(check(self.engine), core.TradingEngine._apply_trailing_stops(engine))
        await core.TradingEngine._apply_trailing_stops(engine)
        await check(self.engine)
        self.assertGreater(pos.sl, 100.0, f"applied={applied}")
        self.assertGreaterEqual(pos.sl, max(applied))

    async def test_paper_and_core_be_paths_are_monotonic(self):
        from bot import partial_tp_execution_hardening as hardening
        src = open(hardening.__file__, encoding="utf-8").read()
        self.assertIn("paper_partial_break_even", src)
        core_src = open(core.__file__, encoding="utf-8").read()
        self.assertIn("core_partial_break_even", core_src)
        self.assertNotIn("pos.sl          = pos.entry   # SL no breakeven", core_src)


class ExchangeFloorTests(unittest.IsolatedAsyncioTestCase):
    """P — after restart local state may be stale; the exchange truth wins."""

    def client(self):
        from bot import conditional_stop_lifecycle as lifecycle
        c = SimpleNamespace()
        c._instruments = {"AVAXUSDT": {"multiplier": "0.1", "lotSize": "1", "minQty": "1", "tickSize": "0.001"}}
        c.get_instruments = lambda: c._instruments
        c._round_price = lambda price, symbol: f"{round(price / 0.001) * 0.001:.3f}"
        c.get_positions = AsyncMock(return_value=[{"symbol": "AVAXUSDT", "side": "Buy", "size": 10,
                                                   "markPrice": 7.6, "entryPrice": 7.0}])
        self.posted = []

        async def post(path, body, **kwargs):
            self.posted.append(body)
            return {"orderId": "o"}
        c._post = AsyncMock(side_effect=post)
        c.lifecycle = lifecycle
        return c

    async def test_p_owned_better_exchange_stop_is_not_loosened(self):
        from bot import native_stop_repair
        c = self.client()
        owned = {"clientOid": "bgx-stop-AVAX-1", "symbol": "AVAXUSDTM", "side": "sell", "stop": "down",
                 "stopPrice": "7.400", "stopPriceType": "MP", "closeOrder": True, "isActive": True}
        c.get_stop_orders = AsyncMock(return_value=[owned])
        module = SimpleNamespace(PAPER_TRADE=False, API_KEY="k", to_kucoin=lambda s: s + "M")
        log = Mock()
        with patch.object(native_stop_repair.lifecycle, "owned_for_lineage", AsyncMock(return_value=True)), \
                patch("bot.stop_monotonic.log", log):
            ok = await native_stop_repair.set_stops(c, "AVAXUSDT", 7.0, 0, module, Mock())
        self.assertTrue(ok, "a more protective owned stop already protects")
        self.assertEqual(self.posted, [], "no new order, no cancellation")
        self.assertIn("native_stop_repair_exchange_floor", log.warning.call_args.args)

    async def test_p_foreign_lineage_stop_is_not_a_floor(self):
        from bot import native_stop_repair
        c = self.client()
        stale = {"clientOid": "bgx-stop-AVAX-old", "symbol": "AVAXUSDTM", "side": "sell", "stop": "down",
                 "stopPrice": "7.400", "stopPriceType": "MP", "closeOrder": True, "isActive": True}
        c.get_stop_orders = AsyncMock(return_value=[stale])
        floor = await native_stop_repair._owned_stop_floor(
            c, "AVAXUSDT", "buy", "sell", "lineage-x", [stale],
            {"size": 10, "sizeUnit": "CONTRACTS"}, c._instruments["AVAXUSDT"], True)
        self.assertIsNone(floor, "not owned for this lineage -> not trusted as protection")


if __name__ == "__main__":
    unittest.main()
