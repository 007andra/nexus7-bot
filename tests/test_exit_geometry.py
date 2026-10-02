"""Q-01 — exit geometry survives partial exits (offline).

Real ``bot.engine.Position`` with the real ``trailing_safety_hardening``
overlay, real ``durable_partial_exit`` / ``confirmed_rr_exit`` and the PAPER
durable record. A partial exit reduces size; it never changes the price path,
the initial risk or the trade MFE.
"""
import json
import math
import os
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("PAPER_TRADE", "true")

from bot import durable_execution as durable  # noqa: E402
from bot import engine as core  # noqa: E402
from bot import trailing_safety_hardening as ts  # noqa: E402
from bot.config import cfg  # noqa: E402
from bot.exit_geometry import (  # noqa: E402
    initial_risk_per_unit, peak_excursion, r_multiple, stop_on_valid_side,
)
from bot.strategy import Signal  # noqa: E402


class Pos(core.Position):
    pass


ts.install(Pos, cfg, SimpleNamespace(warning=lambda *a, **k: None))


def _pos(direction="LONG", entry=100.0, stop=None, tp=None, qty=10.0, cls=Pos):
    stop = stop if stop is not None else (entry - 2 if direction == "LONG" else entry + 2)
    tp = tp if tp is not None else (entry + 4 if direction == "LONG" else entry - 4)
    pos = cls(Signal("BTCUSDT", direction, entry, stop, tp, 0.8, "t", 80), qty)
    pos._forensic_lineage = {"order_id": "opening-1"}
    return pos


def _partial(pos, fraction=0.5):
    """durable_partial_exit mutation: qty = remainder, SL -> BE, tp1_hit."""
    pos.qty = pos.qty * (1 - fraction)
    pos.sl = pos.trailing_sl = pos.entry
    pos.tp1_hit = True


class ExitGeometryTests(unittest.TestCase):
    def _r_before_after(self, direction):
        pos = _pos(direction)
        s = 1 if direction == "LONG" else -1
        pos.update_pnl(100 + 2 * s)
        before = (r_multiple(pos, pos.current_price), peak_excursion(pos), pos.calc_trailing_sl())
        _partial(pos)
        pos.update_pnl(100 + 2 * s)
        after = (r_multiple(pos, pos.current_price), peak_excursion(pos), pos.calc_trailing_sl())
        return before, after

    def test_a_long_one_r_stays_one_r(self):
        before, after = self._r_before_after("LONG")
        self.assertEqual(before[:2], (1.0, 2.0))
        self.assertEqual(after[:2], (1.0, 2.0))
        self.assertEqual(after[2], 101.5)

    def test_b_short_one_r_stays_one_r(self):
        before, after = self._r_before_after("SHORT")
        self.assertEqual(after[:2], (1.0, 2.0))
        self.assertEqual(after[2], 98.5)

    def test_c_half_partial_does_not_double_excursion(self):
        pos = _pos()
        pos.update_pnl(102.0)
        candidate = pos.calc_trailing_sl()
        _partial(pos)
        pos.update_pnl(102.0)
        self.assertEqual(pos.calc_trailing_sl(), candidate)
        self.assertLess(pos.calc_trailing_sl(), pos.current_price)

    def test_d_multiple_partials_never_inflate(self):
        pos = _pos()
        pos.update_pnl(102.0)
        ref = (r_multiple(pos, 102.0), peak_excursion(pos), pos.calc_trailing_sl())
        for remaining in (5.0, 2.5, 1.25):
            pos.qty = remaining
            pos.update_pnl(102.0)
            self.assertEqual((r_multiple(pos, 102.0), peak_excursion(pos)), ref[:2])
            self.assertEqual(pos.calc_trailing_sl(), ref[2])

    def _walk(self, direction, path, partial_at):
        pos = _pos(direction)
        rs = []
        self.stops = []
        for price in path:
            pos.update_pnl(price)
            if price == partial_at and not pos.tp1_hit:
                _partial(pos)
                pos.update_pnl(price)
            candidate = pos.calc_trailing_sl()
            if candidate is not None:
                self.assertTrue(stop_on_valid_side(direction, candidate, price), (price, candidate))
                if (direction == "LONG" and candidate > pos.trailing_sl) or \
                        (direction == "SHORT" and candidate < pos.trailing_sl):
                    pos.sl = pos.trailing_sl = candidate
            rs.append(r_multiple(pos, price))
            self.stops.append(pos.trailing_sl)
        return rs, pos

    def test_e_long_trailing_always_below_price(self):
        rs, pos = self._walk("LONG", [100, 101, 102, 103, 104], partial_at=102)
        self.assertEqual(rs[2:], [1.0, 1.5, 2.0])
        self.assertEqual(self.stops[2:], [101.5, 102.25, 103.0], "ratchets on 75% of excursion")

    def test_f_short_trailing_always_above_price(self):
        rs, pos = self._walk("SHORT", [100, 99, 98, 97, 96], partial_at=98)
        self.assertEqual(rs[2:], [1.0, 1.5, 2.0])
        self.assertEqual(self.stops[2:], [98.5, 97.75, 97.0], "ratchets on 75% of excursion")

    def test_h_break_even_does_not_zero_r_denominator(self):
        pos = _pos()
        pos.sl = pos.entry
        self.assertEqual(initial_risk_per_unit(pos), 2.0)
        self.assertEqual(r_multiple(pos, 104.0), 2.0)

    def test_k_invalid_candidate_never_returned(self):
        pos = _pos()
        pos.update_pnl(104.0)
        pos.current_price, pos.pnl = 102.5, 2.5 * pos.qty    # sharp pull-back, stale peak
        logger = Mock()
        with patch("bot.exit_geometry.log", logger):
            self.assertIsNone(pos.calc_trailing_sl())          # 103.0 >= 102.5
        self.assertIn("TRAILING_REJECTED_INVALID_SIDE", logger.info.call_args.args[1])

    def test_property_partial_preserves_r_and_side(self):
        rng = random.Random(101)
        for _ in range(400):
            direction = rng.choice(["LONG", "SHORT"])
            entry = rng.uniform(0.01, 50_000)
            risk = entry * rng.uniform(0.002, 0.05)
            s = 1 if direction == "LONG" else -1
            pos = _pos(direction, entry, entry - s * risk, entry + s * risk * rng.uniform(1.5, 4),
                       qty=rng.uniform(0.001, 100))
            price = entry + s * risk * rng.uniform(0.6, 3.5)
            pos.update_pnl(price)
            r_before, c_before = r_multiple(pos, price), pos.calc_trailing_sl()
            _partial(pos, rng.uniform(0.05, 0.95))
            pos.update_pnl(price)
            self.assertTrue(math.isclose(r_multiple(pos, price), r_before, rel_tol=1e-9))
            c_after = pos.calc_trailing_sl()
            for candidate in (c_before, c_after):
                if candidate is not None:
                    self.assertTrue(stop_on_valid_side(direction, candidate, price))

    def test_reconstructed_positions_have_unknown_initial_risk(self):
        src = open(core.__file__, encoding="utf-8").read()
        self.assertEqual(src.count("pos.initial_sl = None"), 3)
        from bot import startup_position_unit_hardening as sp
        self.assertIn("pos.initial_sl = None", open(sp.__file__, encoding="utf-8").read())
        pos = _pos()
        pos.initial_sl = None
        self.assertIsNone(initial_risk_per_unit(pos))
        self.assertIsNone(r_multiple(pos, 104.0))


class _RRHarness(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = {}

        async def load(key, strict=False):
            return self.store.get(key)

        async def save(key, value, strict=False):
            self.store[key] = value
            return True
        for mod in ("bot.confirmed_rr_exit.db", "bot.durable_partial_exit.db"):
            for name, fn in (("load_key_value", load), ("save_key_value", save)):
                p = patch(f"{mod}.{name}", AsyncMock(side_effect=fn))
                p.start()
                self.addCleanup(p.stop)
        self.client = SimpleNamespace(
            build_client_oid=Mock(return_value="oid-1"),
            place_order=AsyncMock(return_value={"orderId": "x-1"}),
            get_order_by_client_oid=AsyncMock(return_value={}),
            wait_for_fill=AsyncMock(return_value={"filled": True}),
            get_positions=AsyncMock(return_value=[]),
            set_sl=AsyncMock(return_value=True))
        info = dict(multiplier=.001, lotSize=1, minQty=1, minNotional=0)
        self.client._instruments = {"BTCUSDT": info}
        self.engine = SimpleNamespace(client=self.client, instruments={"BTCUSDT": info},
                                      _sync_positions=AsyncMock(), _unprotected_symbols=set())


class ExitLifecycleTests(_RRHarness):
    async def test_g_two_r_uses_initial_risk_after_break_even(self):
        from bot.confirmed_rr_exit import check
        pos = _pos(qty=1.0)
        _partial(pos)                                 # sl == entry: old distance 0
        self.engine.positions = {"BTCUSDT": pos}
        pos.update_pnl(103.9)
        await check(self.engine)
        self.client.place_order.assert_not_awaited()  # < 2R (2 * 2.0)
        pos.update_pnl(104.0)
        await check(self.engine)
        self.client.place_order.assert_awaited_once()

    async def test_i_j_partial_fill_and_duplicate_are_exact_and_idempotent(self):
        from bot.durable_partial_exit import check
        pos = _pos(qty=1.0)
        self.engine.positions = {"BTCUSDT": pos}
        pos.update_pnl(102.1)
        # Planned 50 %, the exchange filled only 30 %: residual 0.7 is truth.
        self.client.get_positions.return_value = [
            dict(symbol="BTCUSDT", size=0.7, sizeUnit="BASE_ASSET")]
        await check(self.engine)
        self.assertEqual((pos.qty, pos.tp1_hit, pos.sl), (0.7, True, 100.0))
        self.assertEqual((pos.initial_sl, initial_risk_per_unit(pos)), (98.0, 2.0))
        for _ in range(3):                            # duplicate cycles / fill events
            await check(self.engine)
        self.assertEqual(pos.qty, 0.7)
        self.assertEqual(self.client.place_order.await_count, 1)
        pos.update_pnl(102.1)
        self.assertAlmostEqual(r_multiple(pos, 102.1), 1.05)

    async def test_m_unknown_initial_risk_fails_closed_for_discretionary_exits(self):
        from bot.confirmed_rr_exit import check as rr_check
        from bot.durable_partial_exit import check as partial_check
        pos = _pos(qty=1.0)
        pos.initial_sl = None                         # rebuilt / legacy, never invented
        self.engine.positions = {"BTCUSDT": pos}
        pos.update_pnl(110.0)
        await partial_check(self.engine)
        await rr_check(self.engine)
        self.client.place_order.assert_not_awaited()
        self.client.set_sl.assert_not_awaited()
        self.assertEqual(pos.sl, 98.0, "existing protection preserved")
        self.assertTrue(stop_on_valid_side("LONG", pos.calc_trailing_sl(), 110.0))

    async def test_k_engine_keeps_valid_stop_when_candidate_invalid(self):
        engine = core.TradingEngine.__new__(core.TradingEngine)
        engine.client = SimpleNamespace(set_sl=AsyncMock(return_value=True))
        engine.paper_trade, engine._durable_state_enforced = False, False
        pos = _pos()
        pos.update_pnl(104.0)
        pos.sl = pos.trailing_sl = 102.0
        pos.peak_price = 106.0                        # stale/odd peak -> candidate above price
        pos.current_price = 104.0
        engine.positions = {"BTCUSDT": pos}
        await core.TradingEngine._apply_trailing_stops(engine)
        engine.client.set_sl.assert_not_awaited()
        self.assertEqual((pos.sl, pos.trailing_sl), (102.0, 102.0))


class ExitPersistenceTests(unittest.TestCase):
    def _record(self, pos):
        return json.loads(json.dumps(durable._position_record(pos)))

    def test_l_restart_preserves_geometry(self):
        pos = _pos(qty=10.0)
        pos.update_pnl(103.0)
        _partial(pos)
        pos.update_pnl(102.0)
        restored = durable._restore_position(self._record(pos))
        self.assertEqual((restored.initial_sl, restored.peak_price, restored.qty, restored.tp1_hit),
                         (98.0, 103.0, 5.0, True))
        self.assertEqual(initial_risk_per_unit(restored), 2.0)
        self.assertEqual(r_multiple(restored, 102.0), 1.0)

    def test_m_legacy_snapshot_without_fields_is_safe(self):
        pos = _pos(qty=10.0)
        pos.update_pnl(103.0)                         # peak_pnl 30 on qty 10
        _partial(pos)
        record = self._record(pos)
        record.pop("initial_sl")
        record.pop("peak_price")
        restored = durable._restore_position(record)
        self.assertIsNone(restored.initial_sl)
        self.assertIsNone(initial_risk_per_unit(restored), "never invented")
        self.assertLessEqual(restored.peak_price, 103.0, "fallback never overstates MFE")
        restored.update_pnl(102.0)
        candidate = Pos.calc_trailing_sl(restored)
        self.assertTrue(candidate is None or stop_on_valid_side("LONG", candidate, 102.0))


class PayoffSimulationTests(unittest.TestCase):
    def test_o_dead_zone_removed(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "research", "strategy_audit"))
        import exit_payoff_sim as sim
        before_invalid = 0
        for R in (2.0, 3.0):
            for name, path in sim.PATHS.items():
                _, _, _, before = sim.simulate(R, path, mode="before")
                _, _, _, after = sim.simulate(R, path, mode="after")
                before_invalid += before["invalid_trigger"]
                self.assertEqual(after["invalid_trigger"], 0, (R, name))
        self.assertGreater(before_invalid, 0, "the simulator reproduces the old defect")
        g_before, _, _, _ = sim.simulate(2.0, sim.PATHS["+1.4R then reverse"], mode="before")
        g_after, _, _, st = sim.simulate(2.0, sim.PATHS["+1.4R then reverse"], mode="after")
        self.assertGreater(g_after, g_before + 0.4, "reversal after partial keeps trailed profit")
        self.assertGreater(st["max_fill_r"], 1.0, "remainder exits on a trailed stop above BE")
        _, _, _, st3 = sim.simulate(3.0, sim.PATHS["straight to target"], mode="after")
        self.assertTrue(st3["rr_exit"], "2R exit reachable after break-even")


if __name__ == "__main__":
    unittest.main()
