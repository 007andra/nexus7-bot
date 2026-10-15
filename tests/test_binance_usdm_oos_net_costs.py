import unittest
from bot.binance_usdm_oos_net_costs import evaluate_costs

PROOFS = dict.fromkeys(("historical_path", "entry_feasible", "quantity_filters",
                        "fee_schedule", "spread_at_decision", "slippage_assumption",
                        "funding_settlement", "capital_basis", "time_alignment"), True)
EVIDENCE = {"entry_price": "100", "exit_price": "98", "quantity": "1",
            "capital_usdt": "20", "entry_fee_bps": "4", "exit_fee_bps": "4",
            "spread_bps": "2", "entry_slippage_bps": "1",
            "exit_slippage_bps": "1", "funding_usdt": "0.01"}


class TestOosNetCosts(unittest.TestCase):
    def test_short_net_and_stress(self):
        r = evaluate_costs(side="SHORT", evidence=EVIDENCE, proofs=PROOFS)
        self.assertEqual(r["status"], "HYPOTHETICAL_NET_CALCULATED")
        self.assertLess(float(r["stressed_net_usdt"]), float(r["base_net_usdt"]))
        self.assertLess(float(r["base_net_usdt"]), 2)
        self.assertFalse(r["live_allowed"])

    def test_long_loss(self):
        r = evaluate_costs(side="LONG", evidence=EVIDENCE, proofs=PROOFS)
        self.assertLess(float(r["base_net_usdt"]), 0)

    def test_funding_missing_blocks(self):
        evidence = dict(EVIDENCE)
        evidence.pop("funding_usdt")
        r = evaluate_costs(side="SHORT", evidence=evidence, proofs=PROOFS)
        self.assertEqual(r["status"], "NET_PROOF_MISSING")

    def test_unverified_fees_block(self):
        proofs = dict(PROOFS)
        proofs["fee_schedule"] = False
        self.assertEqual(evaluate_costs(side="SHORT", evidence=EVIDENCE,
                                        proofs=proofs)["status"], "NET_PROOF_MISSING")

    def test_nonfinite_blocks(self):
        evidence = dict(EVIDENCE)
        evidence["quantity"] = "NaN"
        self.assertEqual(evaluate_costs(side="SHORT", evidence=evidence,
                                        proofs=PROOFS)["status"], "NET_PROOF_MISSING")


if __name__ == "__main__":
    unittest.main()
