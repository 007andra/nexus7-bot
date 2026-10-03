"""NOVO-F013A-1 — a timed-out entry is adopted from its NATIVE protection.

Composed LIVE-pilot runtime over the offline KuCoin fake: F-003 sizing ->
POST /api/v1/st-orders (native SL/TP legs) -> wait_for_fill times out ->
_reconcile_exchange_positions -> native protection of THIS opening lineage ->
F-013 / F-013A -> local Position. No ATR / liquidation estimate may ever
become the trade's geometry (INV-TIMEOUT-GEOMETRY-001,
INV-NATIVE-PROTECTION-AUTHORITY-001, INV-NO-FALLBACK-STRATEGY-001,
INV-TIMEOUT-RPARITY-001).
"""
import json
import random
import time
from unittest.mock import AsyncMock, patch

from tests.test_postfill_late_fill_composed import ComposedLateFillTests as _Harness

from bot import exit_geometry_durability as egd  # noqa: E402
from bot import postfill_geometry as pg  # noqa: E402
from bot.engine import Position  # noqa: E402
from bot.exit_geometry import initial_risk_per_unit, r_multiple  # noqa: E402
from bot.startup_position_unit_hardening import _startup_levels  # noqa: E402
from bot.strategy import Signal  # noqa: E402
from tests.postfill_fake import PostfillExchange  # noqa: E402

LEVELS = {"LONG": (100.0, 98.0, 104.0), "SHORT": (100.0, 102.0, 96.0)}
CLOSE = {"LONG": "sell", "SHORT": "buy"}
SL_KIND = {"LONG": "down", "SHORT": "up"}


def atr_fallback(direction, ep, liq=0.0):
    """What the pre-patch orphan adoption would have sent (engine formula)."""
    atr = ep * 0.007
    if direction == "LONG":
        return max(liq * 1.02, ep - atr * 1.5) if liq > 0 else ep - atr * 1.5
    return min(liq * 0.98, ep + atr * 1.5) if liq > 0 else ep + atr * 1.5


class TimeoutNativeProtectionTests(_Harness):
    async def _timeout_open(self, direction="LONG", fills=((10, 100.0),), *, mark=None, prepare=None,
                            keep_active=True, levels=None):
        entry, sl, tp = levels or LEVELS[direction]
        ex = PostfillExchange("SOLUSDTM", 0.1, fills=list(fills), mark=mark or fills[-1][1],
                              keep_active=keep_active)
        if prepare:
            prepare(ex)
        engine = self._engine(ex)
        await engine._open(Signal("SOLUSDT", direction, entry, sl, tp, .8, "t", 80))
        return engine, ex, engine.positions.get("SOLUSDT")

    def stop_posts(self, ex):
        return [float(b["stopPrice"]) for b in ex.posts() if "stop" in b]

    def assert_geometry(self, pos, ex, direction, sl, tp):
        _, _, stop, loss = self._exchange_truth(ex, direction)
        self.assertEqual(pos._postfill_state, pg.CONFIRMED)
        self.assertEqual((pos.sl, pos.initial_sl, stop), (sl, sl, sl))
        self.assertEqual(pos.tp, tp)
        self.assertLessEqual(loss, 2.25 + 1e-9)

    # A / B / C ---------------------------------------------------------------
    async def test_a_long_timeout_recovers_native_levels(self):
        engine, ex, pos = await self._timeout_open("LONG")
        self.assertIsNotNone(pos)
        self.assert_geometry(pos, ex, "LONG", 98.0, 104.0)
        self.assertEqual(self.stop_posts(ex), [], "no stop/TP order created by the adoption")
        self.assertEqual(pos._native_protection.level_source, "order_echo")
        self.assertEqual(pos._native_protection.sl_source, "native_leg")

    async def test_b_short_timeout_recovers_native_levels(self):
        engine, ex, pos = await self._timeout_open("SHORT")
        self.assert_geometry(pos, ex, "SHORT", 102.0, 96.0)
        self.assertEqual(self.stop_posts(ex), [])

    async def test_c_atr_fallback_never_wins_over_native(self):
        for direction in ("LONG", "SHORT"):
            engine, ex, pos = await self._timeout_open(direction)
            fallback = round(atr_fallback(direction, 100.0), 2)
            self.assertNotEqual(fallback, pos.sl)
            self.assertNotIn(fallback, self.stop_posts(ex))
            self.assertAlmostEqual(initial_risk_per_unit(pos), 2.0)

    async def test_d_liquidation_fallback_never_wins_over_native(self):
        for direction, liq in (("LONG", 97.5), ("SHORT", 102.5)):
            def prepare(ex, liq=liq):
                ex.liquidation = liq
            engine, ex, pos = await self._timeout_open(direction, prepare=prepare)
            fallback = round(atr_fallback(direction, 100.0, liq), 2)
            self.assertNotIn(fallback, self.stop_posts(ex))
            self.assertEqual(pos.initial_sl, LEVELS[direction][1])

    # E / F -------------------------------------------------------------------
    async def test_e_adverse_fill_repair_starts_from_native_stop(self):
        for direction, fill, repaired in (("LONG", 100.4, 98.4), ("SHORT", 99.6, 101.6)):
            engine, ex, pos = await self._timeout_open(direction, fills=((10, fill),))
            self.assertEqual(self.stop_posts(ex), [repaired], "F-013 distance/budget repair only")
            self.assert_geometry(pos, ex, direction, repaired, LEVELS[direction][2])
            self.assertAlmostEqual(initial_risk_per_unit(pos), 2.0)
            self.assertNotIn(round(atr_fallback(direction, fill), 2), self.stop_posts(ex))

    async def test_f_favorable_fill_never_loosens_native_stop(self):
        for direction, fill in (("LONG", 99.8), ("SHORT", 100.2)):
            engine, ex, pos = await self._timeout_open(direction, fills=((10, fill),))
            self.assertEqual(self.stop_posts(ex), [])
            self.assert_geometry(pos, ex, direction, LEVELS[direction][1], LEVELS[direction][2])

    # G / H -------------------------------------------------------------------
    async def test_g_stale_stop_of_older_lineage_is_never_used(self):
        old_ms = int(time.time() * 1000) - 3_600_000
        for direction, stale in (("LONG", 99.0), ("SHORT", 101.0)):
            def prepare(ex, stale=stale, direction=direction):
                ex.add_stop(stale, side=CLOSE[direction], stop=SL_KIND[direction], created_ms=old_ms)
            engine, ex, pos = await self._timeout_open(direction, prepare=prepare)
            self.assertNotEqual(pos.sl, stale)
            self.assertIsNone(pos.initial_sl, "a tighter non-lineage stop leaves geometry UNCONFIRMED")
            self.assertEqual(pos._postfill_state, pg.UNCONFIRMED)
        # a stale stop AT the same level as the native one is ignored, native leg used
        def same_level(ex):
            ex.add_stop(98.0, side="sell", stop="down", created_ms=old_ms)
        engine, ex, pos = await self._timeout_open("LONG", prepare=same_level)
        self.assert_geometry(pos, ex, "LONG", 98.0, 104.0)

    async def test_h_external_position_never_gains_bgx_geometry(self):
        ex = PostfillExchange("SOLUSDTM", 0.1, fills=[(10, 100.0)], mark=100.0, keep_active=True)
        engine = self._engine(ex)
        engine._external_position_symbols = {"SOLUSDT"}
        before = len(ex.posts())
        self.assertIsNone(await engine._reconcile_exchange_positions(only_symbol="SOLUSDT"))
        self.assertNotIn("SOLUSDT", engine.positions)
        self.assertEqual(len(ex.posts()), before)
        # manual stop on an adopted BGX position is never this trade's stop
        def manual(ex):
            ex.add_stop(99.0, side="sell", stop="down", client_oid="user-manual-1")
        engine, ex, pos = await self._timeout_open("LONG", prepare=manual)
        self.assertNotEqual(pos.initial_sl, 99.0)

    # I / J -------------------------------------------------------------------
    async def test_i_missing_native_tp_is_not_invented(self):
        def no_tp(ex):
            ex.native_legs = {"down"}
        engine, ex, pos = await self._timeout_open("LONG", prepare=no_tp)
        self.assertEqual(self.stop_posts(ex), [], "no TP order fabricated")
        self.assertEqual(ex.tp_levels(), [])
        self.assertEqual((pos.initial_sl, pos.tp), (98.0, 104.0), "local target = lineage's own TP")
        self.assertIsNone(pos._native_protection.tp)

    async def test_j_missing_native_sl_never_confirms_geometry(self):
        # original stop cannot be restored (market already beyond it)
        def no_sl(ex):
            ex.native_legs = {"up"}
        engine, ex, pos = await self._timeout_open("LONG", mark=97.5, prepare=no_sl)
        self.assertEqual((pos._postfill_state, pos.initial_sl), (pg.UNCONFIRMED, None))
        self.assertEqual(self.stop_posts(ex), [], "no ATR/liquidation stop invented")
        self.assertIn("SOLUSDT", engine._unprotected_symbols)

    async def test_j2_missing_native_sl_restores_only_the_original_stop(self):
        def no_sl(ex):
            ex.native_legs = {"up"}
        engine, ex, pos = await self._timeout_open("LONG", prepare=no_sl)
        self.assertEqual(self.stop_posts(ex), [98.0], "exactly the lineage's own trigger")
        self.assert_geometry(pos, ex, "LONG", 98.0, 104.0)

    # K / L -------------------------------------------------------------------
    async def test_k_most_protective_stop_of_the_lineage(self):
        def foreign_looser(ex):
            ex.add_stop(97.0, side="sell", stop="down", client_oid="user-manual-2")
        engine, ex, pos = await self._timeout_open("LONG", prepare=foreign_looser)
        self.assertEqual(pos.initial_sl, 98.0)
        self.assertTrue(await engine.client.set_position_stops("SOLUSDT", sl=98.5))   # BGX repair
        pos._postfill_dirty = True
        await egd.sync(engine)
        self.assertEqual((pos.sl, pos._postfill_state), (98.5, pg.CONFIRMED))
        self.assertEqual(pos.initial_sl, 98.0, "initial risk definition unchanged by a later tighten")

    async def test_l_repeated_adoption_and_reconcile_is_idempotent(self):
        engine, ex, pos = await self._timeout_open("LONG", fills=((10, 100.4),))
        snapshot = (pos.entry, pos.sl, pos.initial_sl, pos.tp, pos.qty)
        posts = len(ex.posts())
        for _ in range(10):
            await engine._reconcile_exchange_positions(only_symbol="SOLUSDT")
            pos._postfill_dirty = True
            await egd.sync(engine)
        self.assertEqual((pos.entry, pos.sl, pos.initial_sl, pos.tp, pos.qty), snapshot)
        self.assertEqual(len(ex.posts()), posts, "zero new orders")

    # M / O : restart ---------------------------------------------------------
    def _startup_position(self, ex, direction, oid):
        """What the startup loader rebuilds: ATR/liquidation LOCAL estimate."""
        ep = ex.position["entry"]
        sl, tp = _startup_levels(ep, 0.0, direction)
        pos = Position(Signal("SOLUSDT", direction, ep, sl, tp, .75, "Startup sync", 75),
                       abs(ex.position["qty"]) * 0.1)
        pos.initial_sl = None
        pos._forensic_lineage = {"opening_order_id": oid}
        pos._risk_reserved_usdt = 2.25
        return pos, (sl, tp)

    async def test_m_crash_before_confirmation_restart_rediscovers_native(self):
        with patch.object(pg, "reconcile", AsyncMock(return_value=pg.UNCONFIRMED)):   # crash
            engine, ex, pos = await self._timeout_open("SHORT")
        self.assertIsNone(pos.initial_sl)
        oid = ex.entry_order()["id"]
        restored, startup = self._startup_position(ex, "SHORT", oid)
        engine.positions = {"SOLUSDT": restored}
        self.assertFalse(await egd.restore(engine, "SOLUSDT"))          # nothing durable yet
        await egd.sync(engine)
        self.assert_geometry(restored, ex, "SHORT", 102.0, 96.0)
        self.assertNotIn(round(startup[0], 2), (restored.sl, restored.initial_sl))
        self.assertNotEqual(restored.tp, startup[1], "startup ATR TP replaced by the native TP")
        self.assertEqual(self.stop_posts(ex), [])
        record = json.loads(self.store[egd._key(oid)])
        self.assertEqual((record["initial_sl"], record["initial_tp"]), (102.0, 96.0))

    async def test_o_contradicting_durable_geometry_never_overrides_native(self):
        engine, ex, pos = await self._timeout_open("LONG")
        oid = ex.entry_order()["id"]
        from bot import trade_lifecycle
        await trade_lifecycle.open_trade(pos, pos.qty, "entry_confirmed")
        bogus = egd.build_record(pos)
        bogus.update(initial_sl=99.5, peak_price=100.0)                  # tighter than any live stop
        self.store[egd._key(oid)] = json.dumps(bogus)
        restored, _ = self._startup_position(ex, "LONG", oid)
        engine.positions = {"SOLUSDT": restored}
        self.assertFalse(await egd.restore(engine, "SOLUSDT"))
        self.assertIsNone(restored.initial_sl)
        await egd.sync(engine)
        self.assert_geometry(restored, ex, "LONG", 98.0, 104.0)

    # N -----------------------------------------------------------------------
    async def test_n_late_fill_after_native_adoption_is_revalidated(self):
        engine, ex, pos = await self._timeout_open("LONG", fills=((5, 100.0),))
        self.assertEqual((pos.qty, pos.initial_sl), (0.5, 98.0))
        self.assertFalse(pos._postfill_version["terminal"])
        ex.late_fill(5, 102.5)
        await egd.sync(engine)
        self.assertEqual((pos.qty, pos.entry, pos.sl), (1.0, 101.25, 99.25))

    # P + parity ----------------------------------------------------------------
    async def test_p_composed_timeout_partial_late_fill_no_fallback(self):
        for direction, late, vwap, stop in (("LONG", 102.5, 101.25, 99.25),
                                            ("SHORT", 97.5, 98.75, 100.75)):
            engine, ex, pos = await self._timeout_open(direction, fills=((5, 100.0),))
            self.assertEqual(int(ex.posts()[0]["size"]), 10, "F-003 sized 10 contracts")
            nsl, ntp = LEVELS[direction][1:]
            self.assertEqual((ex.sl_levels(), ex.tp_levels()), ([nsl], [ntp]), "native legs intact")
            self.assert_geometry(pos, ex, direction, nsl, ntp)
            ex.late_fill(5, late)
            await engine._reconcile_exchange_positions()
            await egd.sync(engine)
            self.assertEqual((pos.qty, pos.entry), (1.0, vwap))
            self.assert_geometry(pos, ex, direction, stop, ntp)
            self.assertEqual(self.stop_posts(ex), [stop], "only the F-003 budget repair")
            for estimate in (atr_fallback(direction, 100.0), atr_fallback(direction, vwap)):
                self.assertNotIn(round(estimate, 2), self.stop_posts(ex) + [pos.sl, pos.initial_sl])

    async def test_timeout_and_no_timeout_have_identical_r_geometry(self):
        for direction, fill in (("LONG", 100.4), ("LONG", 99.8), ("SHORT", 99.6), ("SHORT", 100.2)):
            out = []
            for keep_active in (False, True):                   # normal path vs timeout path
                engine, ex, pos = await self._timeout_open(direction, fills=((10, fill),),
                                                           keep_active=keep_active)
                sign = 1 if direction == "LONG" else -1
                one_r = initial_risk_per_unit(pos)
                out.append((pos.entry, pos.initial_sl, pos.tp, pos.qty, one_r,
                            pos.entry + sign * one_r, pos.entry + sign * 2 * one_r,
                            round(r_multiple(pos, pos.tp), 9), tuple(self.stop_posts(ex))))
            self.assertEqual(out[0], out[1], f"INV-TIMEOUT-RPARITY-001 {direction} fill={fill}")

    async def test_property_fallback_inputs_never_change_recovered_geometry(self):
        rng = random.Random(1301)
        adopted = 0
        for _ in range(40):
            direction = rng.choice(("LONG", "SHORT"))
            entry = round(rng.uniform(80, 120), 2)
            dist = round(rng.uniform(0.6, 2.0), 2)
            sign = 1 if direction == "LONG" else -1
            levels = (entry, round(entry - sign * dist, 2), round(entry + sign * 2 * dist, 2))
            fill = round(entry * (1 + rng.uniform(-0.002, 0.002)), 2)
            results = []
            for liq in (0.0, round(entry * (0.9 if direction == "LONG" else 1.1), 2),
                        round(entry - sign * dist * 0.3, 2)):
                def prepare(ex, liq=liq):
                    ex.liquidation = liq or None
                engine, ex, pos = await self._timeout_open(direction, fills=((5, fill),),
                                                           prepare=prepare, levels=levels)
                if pos is None:
                    results.append(None)
                    continue
                results.append((pos._postfill_state, pos.entry, pos.initial_sl, pos.sl, pos.tp,
                                tuple(self.stop_posts(ex))))
                fb = round(atr_fallback(direction, fill, liq), 2)
                if not any(abs(fb - lvl) < 1e-9 for lvl in (levels[1],) + tuple(self.stop_posts(ex))):
                    self.assertNotEqual(pos.sl, fb)
            self.assertEqual(len(set(results)), 1, (direction, levels, fill, results))
            if results[0] is not None:
                adopted += 1
                self.assertEqual(results[0][2], results[0][3])
                self.assertEqual(results[0][4], levels[2], "native TP, never the ATR estimate")
        # Some random sizes are refused before dispatch (see NEW FINDING on float
        # base qty); the property must still have been exercised broadly.
        self.assertGreaterEqual(adopted, 20)


for _name in [n for n in dir(_Harness) if n.startswith("test_")]:
    if _name not in TimeoutNativeProtectionTests.__dict__:
        setattr(TimeoutNativeProtectionTests, _name, None)
del _Harness
