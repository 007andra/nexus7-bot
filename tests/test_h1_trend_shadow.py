"""H1 shadow strategy: same decisions as the TREND_LONG_V1 study simulator.

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
from bot import h1_trend_shadow as h1
from bot import trend_long_study_v1 as study

DAY = h1.DAY_MS
DAY0 = 1_640_995_200_000 // DAY  # 2022-01-01
LOG = logging.getLogger("test_h1_trend_shadow")


def random_days(n, seed, start=100.0, regime=90):
    rng = random.Random(seed)
    days, price = [], start
    for i in range(n):
        drift = 0.006 * (1 if (i // regime) % 2 == 0 else -1)
        o = price
        c = max(1.0, o * (1 + drift + rng.gauss(0, 0.03)))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.01)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.01)))
        days.append({"ts": (DAY0 + i) * DAY, "o": o, "h": h, "l": lo, "c": c, "v": 1.0})
        price = c
    return days


def study_trades(days, btc):
    sdays = study.daily_bars(days)
    up = study.btc_filter(study.daily_bars(btc))
    rows = study.simulate_h1("X", sdays, up, 0.0, ("ASSUMED", 0.0))
    last_close = days[-1]["ts"] + DAY - 1
    # A stop on the last day needs that day closed, which the shadow never sees.
    return [(r["ts"] // DAY, r["reason"], round(r["gross_r"], 8)) for r in rows if r["exit_ts"] != last_close]


def shadow_trades(days, btc):
    state, out = {"positions": {}}, []
    for i in range(1, len(days) + 1):
        now = days[i - 1]["ts"] + 60_000
        filt = h1.btc_above_sma(btc[:i], now)
        for ev in h1.step(state, "X", days[:i], now, filt):
            if ev["event"] == "ENTRY":
                assert ev["position"]["direction"] == "LONG"
            else:
                out.append((ev["position"]["entry_day_ts"] // DAY, ev["reason"], round(ev["r"], 8)))
    return out


class ParityTests(unittest.TestCase):
    def test_shadow_matches_the_study_simulator(self):
        total = 0
        for seed in range(20):
            btc = random_days(520, 1000 + seed, regime=110)
            days = random_days(520, seed)
            expected = study_trades(days, btc)
            with self.subTest(seed=seed):
                self.assertEqual(shadow_trades(days, btc), expected)
            total += len(expected)
        self.assertGreater(total, 30)  # the paths actually produce trades


class FilterTests(unittest.TestCase):
    def flat(self, n, price=100.0):
        return [{"ts": (DAY0 + i) * DAY, "o": price, "h": price + 1, "l": price - 1, "c": price, "v": 1}
                for i in range(n)]

    def test_needs_200_closed_days_and_compares_the_last_close(self):
        days = self.flat(200)
        days[-1]["c"] = 120
        now = days[-1]["ts"] + DAY + 60_000
        filt = h1.btc_above_sma(days, now)
        self.assertTrue(filt["up"])
        self.assertEqual(filt["day_ts"], days[-1]["ts"])
        self.assertIsNone(h1.btc_above_sma(days[1:], now)["up"])
        days[-1]["c"] = 80
        self.assertFalse(h1.btc_above_sma(days, now)["up"])

    def test_entries_need_a_long_signal_and_the_filter_of_the_same_day(self):
        sym = self.flat(25)
        sym.append({"ts": (DAY0 + 25) * DAY, "o": 100, "h": 103, "l": 100, "c": 102, "v": 1})
        sym.append({"ts": (DAY0 + 26) * DAY, "o": 102, "h": 102, "l": 102, "c": 102, "v": 1})
        now = sym[-1]["ts"] + 60_000
        signal_day = sym[-2]["ts"]
        up = {"day_ts": signal_day, "up": True}
        self.assertEqual([e["event"] for e in h1.step({}, "X", sym, now, up)], ["ENTRY"])
        self.assertEqual(h1.step({}, "X", sym, now, {"day_ts": signal_day, "up": False}), [])
        self.assertEqual(h1.step({}, "X", sym, now, {"day_ts": signal_day - DAY, "up": True}), [])
        sym[-2].update(h=100, l=97, c=98)  # breakdown: B2 would short, H1 does not
        self.assertEqual(h1.step({}, "X", sym, now, up), [])

    def test_exits_ignore_the_filter(self):
        state = {"positions": {"X": {"direction": "LONG", "entry": 100.0, "stop": 50.0, "distance": 50.0,
                                     "entry_day_ts": DAY0 * DAY, "checked_day_ts": 0,
                                     "signal_close": 100.0, "channel": 99.0}}}
        sym = self.flat(25)
        sym.append({"ts": (DAY0 + 25) * DAY, "o": 100, "h": 100, "l": 90, "c": 91, "v": 1})
        sym.append({"ts": (DAY0 + 26) * DAY, "o": 91, "h": 91, "l": 91, "c": 91, "v": 1})
        events = h1.step(state, "X", sym, sym[-1]["ts"] + 60_000, {"day_ts": None, "up": None})
        self.assertEqual([(e["event"], e["reason"]) for e in events], [("EXIT", "CHANNEL_EXIT")])


class RunOnceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig = (db._conn, db._is_pg)
        db._conn = await aiosqlite.connect(":memory:")
        db._is_pg = False
        await db._create_tables()

    async def asyncTearDown(self):
        await db._conn.close()
        db._conn, db._is_pg = self._orig

    async def test_btc_is_fetched_once_with_enough_history(self):
        days = random_days(260, 3)
        calls = []

        async def get_klines(symbol, interval, limit):
            calls.append((symbol, interval, limit))
            return days[-limit:]

        engine = SimpleNamespace(client=SimpleNamespace(get_klines=get_klines))
        now = days[-1]["ts"] + 300_000
        state = await h1.run_once(engine, LOG, now_ms=now)
        self.assertEqual(len(calls), len(h1.SYMBOLS))
        self.assertEqual(calls[0], ("BTCUSDT", "D", h1.FILTER_KLINE_LIMIT))
        self.assertEqual(state["last_day_ts"], days[-1]["ts"])
        self.assertIsNotNone(state["filter"]["up"])
        saved = json.loads(await db.load_key_value(h1.STATE_KEY))
        self.assertEqual(saved["last_day_ts"], days[-1]["ts"])
        self.assertNotEqual(h1.STATE_KEY, b2.STATE_KEY)

    async def test_filter_fetch_error_blocks_entries_and_retries(self):
        days = random_days(260, 4)

        async def get_klines(symbol, interval, limit):
            if symbol == "BTCUSDT":
                raise RuntimeError("HTTP 503")
            return days[-limit:]

        engine = SimpleNamespace(client=SimpleNamespace(get_klines=get_klines))
        state = await h1.run_once(engine, LOG, now_ms=days[-1]["ts"] + 300_000)
        self.assertEqual(state["positions"], {})
        self.assertEqual(state["last_day_ts"], 0)

    def test_disabled_by_default(self):
        engine = SimpleNamespace(_start_background=lambda c: self.fail("must not schedule"))
        self.assertFalse(h1.schedule_if_enabled(engine, LOG))


class IsolationTests(unittest.TestCase):
    def test_module_has_no_order_path(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(h1))
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertEqual(called & {"place_order", "_open", "set_position_stops", "set_sl",
                                   "close_position", "cancel_order", "cancel_all_orders",
                                   "set_leverage", "size"}, set())


if __name__ == "__main__":
    unittest.main()
