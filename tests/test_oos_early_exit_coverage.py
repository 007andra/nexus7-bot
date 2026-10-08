"""Research-only early-exit candle coverage regression checks."""
import unittest
from research.oos_rca_v1.binance_path_replay import evaluate

BASE = {"candidate_id":"A","symbol":"BTCUSDT","side":"LONG","captured_epoch":1,
        "entry":100,"stop":95,"target":110,"bars":[]}

def bar(t, low=99, high=101, op=100, close=100):
    return {"ts":t,"o":op,"h":high,"l":low,"c":close}

class EarlyExitCoverageTests(unittest.TestCase):
    def test_stop_first_bar_survives_missing_later_candles(self):
        a,b=evaluate({**BASE,"bars":[bar(900,low=94)]},as_of_epoch=17000)
        self.assertEqual((a["exit_reason"],b["exit_reason"]),("STOP","STOP"))
        self.assertEqual(b["status"],"MODELED_GROSS_ONLY")
    def test_target_first_bar_survives_missing_later_candles(self):
        b=evaluate({**BASE,"bars":[bar(900,high=112)]},as_of_epoch=17000)[1]
        self.assertEqual(b["exit_reason"],"TARGET")
    def test_gap_before_any_exit_fails_closed(self):
        b=evaluate({**BASE,"bars":[bar(1800,low=94)]},as_of_epoch=17000)[1]
        self.assertEqual(b["status"],"UNKNOWN_CANDLE_GAP")
    def test_future_horizon_not_mature_even_if_stopped(self):
        b=evaluate({**BASE,"bars":[bar(900,low=94)]},as_of_epoch=4500)[1]
        self.assertEqual(b["status"],"NOT_MATURED")
    def test_later_extreme_gap_after_stop_does_not_change_exit(self):
        b=evaluate({**BASE,"bars":[bar(900,low=94),bar(5400,low=91)]},
                   as_of_epoch=17000)[1]
        self.assertEqual(b["exit_reference"],95)
    def test_second_bar_stop_when_first_bar_present(self):
        b=evaluate({**BASE,"bars":[bar(900),bar(1800,low=94)]},
                   as_of_epoch=17000)[1]
        self.assertEqual(b["exit_candle_epoch"],1800)
    def test_full_horizon_no_exit_requires_full_path(self):
        bars=[bar(t) for t in range(900,15300,900) if t!=3600]
        b=evaluate({**BASE,"bars":bars},as_of_epoch=17000)[1]
        self.assertEqual(b["status"],"UNKNOWN_CANDLE_GAP")

if __name__=="__main__":
    unittest.main()
