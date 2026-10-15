import unittest
from bot.binance_usdm_oos_feasibility import assess_candidate_feasibility

DEPTH = {"status": "DISPLAYED_DEPTH_BOUND", "quantity": "2",
         "weighted_price": "100"}
FILTERS = {"exchange_info_verified": True, "min_qty": "0.1",
           "step_size": "0.1", "min_notional": "5"}
COSTS = {"status": "HYPOTHETICAL_NET_CALCULATED",
         "base_net_usdt": "1", "stressed_net_usdt": "0.5"}
SOURCE = {"verified": True}


def check(depth=DEPTH, filters=FILTERS, costs=COSTS, source=SOURCE):
    return assess_candidate_feasibility(candidate_id="candidate-1",
                                        depth=depth, filters=filters,
                                        costs=costs, source_proof=source)


class TestFeasibility(unittest.TestCase):
    def test_conditional_not_live(self):
        result = check()
        self.assertEqual(result["status"], "CONDITIONAL_HYPOTHETICAL_FEASIBILITY")
        self.assertFalse(result["fill_proven"])
        self.assertFalse(result["live_allowed"])

    def test_missing_source_blocks(self):
        self.assertIn("SOURCE_PROOF_MISSING", check(source={})["blockers"])

    def test_min_notional_blocks(self):
        f = dict(FILTERS, min_notional="300")
        self.assertIn("BELOW_MIN_NOTIONAL", check(filters=f)["blockers"])

    def test_step_mismatch_blocks(self):
        d = dict(DEPTH, quantity="2.05")
        self.assertIn("INVALID_STEP_SIZE", check(depth=d)["blockers"])

    def test_missing_net_blocks(self):
        self.assertIn("NET_COSTS_UNPROVEN", check(costs={})["blockers"])


if __name__ == "__main__":
    unittest.main()
