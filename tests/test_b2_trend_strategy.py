"""B2 shadow strategy: same decisions as the TREND_BREAKOUT_V1 study simulator.

Synthetic price paths test the mechanics only; they are never evidence.
"""
import json
import logging
import random
import unittest
from types import SimpleNamespace

import aiosqlite

from bot import b2_trend_strategy as b2
from bot import database as db
from bot import trend_breakout_study_v1 as study

DAY0 = 1_640_995_200_000 // b2.DAY_MS  # 2022-01-01
LOG = logging.getLogger("test_b2_trend_strategy")


def random_days(n, seed, start=100.0):
    rng = random.Random(seed)
    days, price = [], start
    for i in range(n):
        drift = 0.004 * (1 if (i // 40) % 2 == 0 else -1)
        o = price
        c = max(1.0, o * (1 + drift + rng.gauss(0, 0.03)))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.01)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.01)))
        days.append({"ts": (DAY0 + i) * b2.DAY_MS, "o": o, "h": h, "l": l, "c": c, "v": 1.0})
        price = c
    return days


def as_15m(days):
    """96 bars per day: open, then the high, then the low, then the close.

    Every 15m bar is valid and reproduces the day's OHLC; the order inside the
    day is irrelevant for B2 because a stop is one-sided.
    """
    bars = []
    for d in days:
        base = d["ts"]
        path = [d["o"]] + [d["h"]] * 31 + [d["l"]] * 32 + [d["c"]] * 32
        prev = d["o"]
        for k, px in enumerate(path):
            o = prev
            bars.append({"ts": base + k * study.BAR_MS, "o": o, "h": max(o, px), "l": min(o, px),
                         "c": px, "v": 1.0})
            prev = px
    return bars


def study_trades(days):
    sdays = study.build_days(as_15m(days))
    rows = study.simulate_b2("X", sdays, entry_days=20, exit_days=10, slip=0.0,
                             funding={"events": [], "absent": []}, fees=[0.0006])
    return [(r["ts"] // b2.DAY_MS, r["direction"], r["reason"], round(r["exit_fill"], 8))
            for r in rows if r["reason"] != "DATA_END"]


def shadow_trades(days):
    state, out, entries = {"positions": {}}, [], {}
    for i in range(1, len(days) + 1):
        now = days[i - 1]["ts"] + 60_000  # just after the open of day i-1
        visible = days[:i]  # day i-1 is forming
        for ev in b2.step(state, "X", visible, now):
            if ev["event"] == "ENTRY":
                entries["X"] = ev["position"]["entry_day_ts"] // b2.DAY_MS
            else:
                out.append((ev["position"]["entry_day_ts"] // b2.DAY_MS, ev["position"]["direction"],
                            ev["reason"], round(ev["price"], 8)))
    return out


class ParityTests(unittest.TestCase):
    def test_shadow_matches_the_study_simulator(self):
        total = 0
        for seed in range(12):
            days = random_days(400, seed)
            expected = study_trades(days)
            got = shadow_trades(days)
            with self.subTest(seed=seed):
                self.assertEqual(got, expected)
            total += len(expected)
        self.assertGreater(total, 30)  # the paths actually produce trades


class RuleTests(unittest.TestCase):
    def flat(self, n=25, price=100.0):
        return [{"ts": (DAY0 + i) * b2.DAY_MS, "o": price, "h": price + 1, "l": price - 1,
                 "c": price, "v": 1.0} for i in range(n)]

    def test_breakout_long_and_short(self):
        days = self.flat()
        days.append({"ts": (DAY0 + 25) * b2.DAY_MS, "o": 100, "h": 103, "l": 100, "c": 102, "v": 1})
        sig = b2.entry_signal(days)
        self.assertEqual(sig["direction"], "LONG")
        self.assertAlmostEqual(sig["channel"], 101.0)
        days[-1].update(h=100, l=97, c=98)
        self.assertEqual(b2.entry_signal(days)["direction"], "SHORT")
        days[-1].update(h=100.5, l=99.5, c=100)
        self.assertIsNone(b2.entry_signal(days))

    def test_not_enough_history(self):
        self.assertIsNone(b2.entry_signal(self.flat(21)))

    def test_closed_days_drop_the_forming_day_and_gaps(self):
        days = self.flat(30)
        now = days[-1]["ts"] + 3600_000
        self.assertEqual(len(b2.closed_days(days, now)), 29)
        self.assertIs(b2.forming_day(days, now), days[-1])
        gapped = days[:10] + days[11:]
        self.assertEqual(len(b2.closed_days(gapped, now)), 18)


class RunOnceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig = (db._conn, db._is_pg)
        db._conn = await aiosqlite.connect(":memory:")
        db._is_pg = False
        await db._create_tables()

    async def asyncTearDown(self):
        await db._conn.close()
        db._conn, db._is_pg = self._orig

    async def test_runs_once_per_day_and_persists_state(self):
        days = random_days(120, 3)
        calls = []

        async def get_klines(symbol, interval, limit):
            calls.append((symbol, interval, limit))
            return days[-limit:]

        engine = SimpleNamespace(client=SimpleNamespace(get_klines=get_klines))
        now = days[-1]["ts"] + 300_000
        state = await b2.run_once(engine, LOG, now_ms=now)
        self.assertEqual(len(calls), len(b2.SYMBOLS))
        self.assertTrue(all(c[1] == "D" for c in calls))
        self.assertEqual(state["last_day_ts"], days[-1]["ts"])
        await b2.run_once(engine, LOG, now_ms=now + 3600_000)
        self.assertEqual(len(calls), len(b2.SYMBOLS))  # same UTC day: no second pass
        saved = json.loads(await db.load_key_value(b2.STATE_KEY))
        self.assertEqual(saved["last_day_ts"], days[-1]["ts"])

    async def test_fetch_errors_retry_the_same_day(self):
        async def get_klines(symbol, interval, limit):
            raise RuntimeError("HTTP 503")

        engine = SimpleNamespace(client=SimpleNamespace(get_klines=get_klines))
        state = await b2.run_once(engine, LOG, now_ms=(DAY0 + 50) * b2.DAY_MS + 300_000)
        self.assertEqual(state["last_day_ts"], 0)

    def test_disabled_by_default(self):
        engine = SimpleNamespace(_start_background=lambda c: self.fail("must not schedule"))
        self.assertFalse(b2.schedule_if_enabled(engine, LOG))


class IsolationTests(unittest.TestCase):
    def test_module_has_no_order_path(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(b2))
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertEqual(called & {"place_order", "_open", "set_position_stops", "set_sl",
                                   "close_position", "cancel_order", "cancel_all_orders",
                                   "set_leverage", "size"}, set())
        self.assertEqual(called & {"get_klines", "load_key_value", "save_key_value"},
                         {"get_klines", "load_key_value", "save_key_value"})


if __name__ == "__main__":
    unittest.main()
