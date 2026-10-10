import unittest
from decimal import Decimal
from bot.binance_usdm_exec_net_oos_v1 import CUTOFF_EPOCH, enroll, pair_outcomes, conservative_path, net_proof


def candidate(cid, ts, symbol="SOLUSDT"):
    return {"candidate_id": f"HARD_GATE_SHADOW:{symbol}:SHORT:BOS_BREAK:{cid}",
            "symbol": symbol, "captured_epoch": ts,
            "payload": {"side": "SHORT", "regime": "TRENDING_DOWN",
                        "setup": "BOS_BREAK", "nexus_called": True,
                        "nexus_allowed": True, "shadow_only": True,
                        "live_eligible": False, "decision_effect": "NONE",
                        "execution_effect": "NONE"}}


class TestBinanceExecNetOosV1(unittest.TestCase):
    def test_cutoff_excludes_equal_and_earlier(self):
        result = enroll([candidate("a", CUTOFF_EPOCH),
                         candidate("b", CUTOFF_EPOCH - 1),
                         candidate("c", CUTOFF_EPOCH + 1)])
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["selected"][0]["candidate_id"].endswith(":c"))

    def test_symbol_cap_and_stable_chronology(self):
        rows = [candidate(str(i), CUTOFF_EPOCH + i + 1) for i in range(14)]
        result = enroll(list(reversed(rows)))
        self.assertEqual(result["count"], 12)
        self.assertEqual(len(result["excluded"]), 2)

    def test_no_hindsight_outcome_selection(self):
        rows = [candidate("1", CUTOFF_EPOCH + 1)]
        result = enroll(rows)
        self.assertEqual(result["count"], 1)
        self.assertFalse(pair_outcomes(result["selected"], [])["paired_complete"])

    def test_exact_pair_and_duplicate_fail(self):
        item = candidate("1", CUTOFF_EPOCH + 1)
        selected = enroll([item])["selected"]
        outcomes = [{"candidate_id": item["candidate_id"], "horizon": h,
                     "payload": {"outcome": "OBSERVED"}} for h in (60, 240)]
        self.assertTrue(pair_outcomes(selected, outcomes)["paired_complete"])
        self.assertFalse(pair_outcomes(selected, outcomes + [outcomes[0]])["paired_complete"])

    def test_short_same_bar_stop_first(self):
        r = conservative_path(side="SHORT", entry=Decimal("100"),
                              stop=Decimal("101"), target=Decimal("98"),
                              candles=[{"high": 102, "low": 97, "close": 99}])
        self.assertEqual(r["exit"], "STOP_FIRST")

    def test_missing_cost_fail_closed(self):
        path = {"status": "PATH_OBSERVED"}
        r = net_proof(path=path, authenticated_bars=True, contiguous_bars=True,
                      symbol_filters_verified=True, fees_verified=True,
                      spread_verified=True, slippage_verified=True,
                      funding_settlement_verified=False, size_verified=True)
        self.assertEqual(r["status"], "NET_PROOF_MISSING")
        self.assertFalse(r["live_allowed"])


if __name__ == "__main__":
    unittest.main()
