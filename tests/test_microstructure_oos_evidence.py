import unittest

from bot.microstructure_oos_evidence import evaluate_microstructure_ranking


def _row(candidate_id, ts, *, micro, depth, r, ev=1.0, setup=80.0):
    return {
        "candidate_id": candidate_id,
        "timestamp": ts,
        "direction": "LONG",
        "nexus_expected_value_pct": ev,
        "nexus_rr_net": 2.0,
        "nexus_setup_quality": setup,
        "nexus_regime_compat": 80.0,
        "nexus_confidence": 70.0,
        "round_trip_cost": 0.001,
        "r_multiple": r,
        "depth_notional_1pct": depth,
        "shadow_microstructure": {
            "available": True,
            "directional_alignment": micro,
            "taker_pressure": micro,
            "execution_effect": "NONE",
            "score_effect": "NONE",
            "promotion_authority": False,
        },
    }


def _reports():
    btc = []
    eth = []
    for index in range(4):
        ts = 1_700_000_000_000 + index * 900_000
        # Base rank prefers ETH slightly, but microstructure/depth favors BTC.
        btc.append(_row(
            f"btc-{index}", ts,
            micro=0.9, depth=10_000_000.0, r=1.5,
            ev=1.0, setup=80.0,
        ))
        eth.append(_row(
            f"eth-{index}", ts,
            micro=-0.9, depth=1_000_000.0, r=-0.5,
            ev=1.2, setup=82.0,
        ))
    return [
        {"symbol": "BTCUSDT", "candidate_diagnostics": btc},
        {"symbol": "ETHUSDT", "candidate_diagnostics": eth},
    ]


class MicrostructureOOSEvidenceTests(unittest.TestCase):
    def test_paired_ranking_is_same_population_and_detects_incremental_uplift(self):
        result = evaluate_microstructure_ranking(
            _reports(), bootstrap_samples=500, seed=7, temporal_folds=4
        )
        self.assertEqual(result["status"], "EVIDENCE_AVAILABLE")
        self.assertEqual(result["cross_sections"], 4)
        self.assertEqual(result["ranked_candidates"], 8)
        self.assertEqual(result["depth_complete_batches"], 4)
        self.assertEqual(result["top_pick_changed_batches"], 4)
        self.assertGreater(result["incremental_top_pick_uplift_r"], 0)
        self.assertGreater(result["top_pick_uplift_ci95"]["low"], 0)
        self.assertEqual(result["temporal_folds_evaluated"], 4)
        self.assertEqual(result["positive_uplift_folds"], 4)
        self.assertFalse(result["outcome_used_in_rank"])
        self.assertEqual(result["execution_effect"], "NONE")
        self.assertEqual(result["score_effect"], "NONE")
        self.assertFalse(result["promotion_authority"])
        self.assertTrue(all(
            batch["base_top_candidate_id"].startswith("eth-")
            and batch["enriched_top_candidate_id"].startswith("btc-")
            for batch in result["batches"]
        ))

    def test_realized_outcomes_do_not_change_pretrade_candidate_ids(self):
        reports = _reports()
        first = evaluate_microstructure_ranking(
            reports, bootstrap_samples=100, seed=3
        )
        for report in reports:
            for row in report["candidate_diagnostics"]:
                row["r_multiple"] *= -3
        second = evaluate_microstructure_ranking(
            reports, bootstrap_samples=100, seed=3
        )
        self.assertEqual(
            [
                (row["base_top_candidate_id"], row["enriched_top_candidate_id"])
                for row in first["batches"]
            ],
            [
                (row["base_top_candidate_id"], row["enriched_top_candidate_id"])
                for row in second["batches"]
            ],
        )
        self.assertNotEqual(
            first["incremental_top_pick_uplift_r"],
            second["incremental_top_pick_uplift_r"],
        )

    def test_unavailable_microstructure_is_excluded_not_imputed(self):
        reports = _reports()
        for report in reports:
            for row in report["candidate_diagnostics"]:
                row["shadow_microstructure"]["available"] = False
        result = evaluate_microstructure_ranking(reports)
        self.assertEqual(
            result["status"], "INSUFFICIENT_CROSS_SECTIONAL_SAMPLE"
        )
        self.assertEqual(result["microstructure_available_candidates"], 0)
        self.assertEqual(result["cross_sections"], 0)

    def test_incomplete_depth_does_not_apply_cross_sectional_liquidity(self):
        reports = _reports()
        for row in reports[1]["candidate_diagnostics"]:
            row["depth_notional_1pct"] = None
        result = evaluate_microstructure_ranking(
            reports, bootstrap_samples=100
        )
        self.assertEqual(result["depth_complete_batches"], 0)
        self.assertEqual(result["cross_sections"], 4)

    def test_duplicate_candidate_id_fails_closed(self):
        reports = _reports()
        reports[1]["candidate_diagnostics"][0]["candidate_id"] = "btc-0"
        with self.assertRaisesRegex(ValueError, "duplicate candidate_id"):
            evaluate_microstructure_ranking(reports)

    def test_any_execution_or_score_authority_in_microstructure_fails(self):
        reports = _reports()
        reports[0]["candidate_diagnostics"][0]["shadow_microstructure"][
            "execution_effect"
        ] = "ALLOW"
        with self.assertRaisesRegex(ValueError, "cannot affect execution"):
            evaluate_microstructure_ranking(reports)

        reports = _reports()
        reports[0]["candidate_diagnostics"][0]["shadow_microstructure"][
            "score_effect"
        ] = "LIVE"
        with self.assertRaisesRegex(ValueError, "cannot affect NEXUS score"):
            evaluate_microstructure_ranking(reports)


if __name__ == "__main__":
    unittest.main()
