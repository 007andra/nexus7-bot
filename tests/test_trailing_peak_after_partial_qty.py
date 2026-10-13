"""Trailing stop after qty shrinks (partial TP / exchange reconcile).

Regression: peak_pnl is quote currency at the qty open when the peak
happened. Dividing it by a smaller post-partial qty doubled the excursion and
put the trailing stop beyond the market (LONG 104 -> SL 106 instead of 103).
"""
import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("EXCHANGE", "binance")
os.environ.setdefault("PAPER_TRADE", "true")

from bot import engine, trailing_safety_hardening  # noqa: E402
from bot.config import cfg  # noqa: E402


class _Log:
    def warning(self, *args, **kwargs):
        pass


def _position(direction, qty=2.0):
    if direction == "LONG":
        sig = SimpleNamespace(symbol="SOLUSDT", direction="LONG", entry=100.0,
                              sl=98.0, tp=106.0, score=70, rr=3.0)
    else:
        sig = SimpleNamespace(symbol="SOLUSDT", direction="SHORT", entry=100.0,
                              sl=102.0, tp=94.0, score=70, rr=3.0)
    return engine.Position(sig, qty)


class TrailingAfterPartialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        trailing_safety_hardening.install(engine.Position, cfg, _Log())
        cls.trigger, cls.lock = cfg.TRAILING_TRIGGER, cfg.TRAILING_LOCK
        cfg.TRAILING_TRIGGER, cfg.TRAILING_LOCK = 0.50, 0.25

    @classmethod
    def tearDownClass(cls):
        cfg.TRAILING_TRIGGER, cfg.TRAILING_LOCK = cls.trigger, cls.lock

    def test_long_partial_keeps_stop_below_market(self):
        p = _position("LONG")
        p.update_pnl(104.0)
        self.assertEqual(p.calc_trailing_sl(), 103.0)
        p.qty, p.sl = 1.0, p.entry          # 50% closed, breakeven stop
        p.update_pnl(104.0)
        sl = p.calc_trailing_sl()
        self.assertEqual(sl, 103.0)
        self.assertLess(sl, p.current_price)

    def test_short_partial_keeps_stop_above_market(self):
        p = _position("SHORT")
        p.update_pnl(96.0)
        self.assertEqual(p.calc_trailing_sl(), 97.0)
        p.qty, p.sl = 1.0, p.entry
        p.update_pnl(96.0)
        sl = p.calc_trailing_sl()
        self.assertEqual(sl, 97.0)
        self.assertGreater(sl, p.current_price)

    def test_new_peak_after_partial_is_tracked_per_unit(self):
        p = _position("LONG")
        p.update_pnl(104.0)
        p.qty = 1.0
        p.update_pnl(105.0)                 # new per-unit high after partial
        self.assertEqual(p.calc_trailing_sl(), 103.75)

    def test_restored_object_without_tracking_never_overstates(self):
        p = _position("LONG")
        p.update_pnl(104.0)
        del p._peak_unit_excursion          # e.g. rebuilt from durable state
        p.qty = 1.0
        p.pnl = (104.0 - p.entry) * p.qty
        sl = p.calc_trailing_sl()
        self.assertLessEqual(sl, 103.0)
        self.assertLess(sl, 104.0)

    def test_restored_peak_survives_first_update_after_restart(self):
        p = _position("LONG")
        p.update_pnl(104.0)
        del p._peak_unit_excursion          # rebuilt: peak_pnl=8 at qty 2
        p.update_pnl(103.0)                 # first tick after restart
        self.assertEqual(p.calc_trailing_sl(), 103.0)

    def test_reconcile_qty_shrink_without_partial(self):
        p = _position("SHORT", qty=3.0)
        p.update_pnl(96.0)
        p.qty = 1.5                         # exchange reports smaller size
        p.update_pnl(96.0)
        self.assertEqual(p.calc_trailing_sl(), 97.0)


if __name__ == "__main__":
    unittest.main()
