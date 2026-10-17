import unittest

from bot.binance_execution_parity import ExecutionPlan, compare_plans, compare_stages
from bot.experiment_registry import ExperimentRecord, promotion_evidence, rollback_evidence
from bot.opportunity_ranker_v2 import Opportunity, rank_opportunities
from bot.portfolio_risk_v2 import beta, covariance_matrix, expected_shortfall, portfolio_snapshot
from bot.release_manifest_v2 import build_release_manifest
from bot.research_authority import ResearchAuthority, ResearchMode, shadow_authority
from bot.trade_research_metrics import execution_quality_score, margin_efficiency, mfe_mae, monte_carlo_paths


class ResearchAuthorityTests(unittest.TestCase):
    def test_shadow_is_non_authoritative(self):
        a = shadow_authority("ranker_v2")
        self.assertEqual(a.decision_effect, "NONE")
        self.assertEqual(a.execution_effect, "NONE")
        self.assertFalse(a.may_veto_live)

    def test_execution_requires_promotion_id(self):
        with self.assertRaises(ValueError):
            ResearchAuthority("x", ResearchMode.EXECUTION_ENABLED).validate()


class ExecutionParityTests(unittest.TestCase):
    def _plan(self, **overrides):
        data = dict(symbol="SOLUSDT", side="LONG", qty=1.2, entry=120.0, stop_loss=118.0,
                    take_profit=124.0, taker_fee_rate=.0005, expected_slippage_rate=.0005,
                    expected_loss_usdt=2.4, leverage=50, required_margin_usdt=2.88)
        data.update(overrides)
        return ExecutionPlan(**data)

    def test_exact_parity_passes(self):
        p = self._plan()
        self.assertTrue(compare_plans(p, p)["pass"])
        self.assertTrue(compare_stages({k:p for k in ("backtest","shadow","paper","live")})["pass"])

    def test_qty_drift_fails(self):
        self.assertFalse(compare_plans(self._plan(), self._plan(qty=1.21))["pass"])


class RankerTests(unittest.TestCase):
    def test_ranker_is_deterministic_and_non_authoritative(self):
        a = Opportunity("a","SOLUSDT","LONG",.8,.62,.9,1,2,.8,.2,1.2,.9)
        b = Opportunity("b","DOGEUSDT","LONG",.1,.52,.5,8,10,.2,.8,.4,.5)
        rows = rank_opportunities([b,a])
        self.assertEqual(rows[0]["candidate_id"], "a")
        self.assertEqual(rows[0]["execution_effect"], "NONE")


class PortfolioRiskTests(unittest.TestCase):
    def setUp(self):
        self.r = {
            "SOLUSDT": [.01,.02,-.01,.03,-.02,.01],
            "AVAXUSDT": [.009,.018,-.012,.025,-.018,.012],
        }

    def test_covariance_and_snapshot(self):
        cov = covariance_matrix(self.r)
        self.assertIn("SOLUSDT", cov)
        snap = portfolio_snapshot(self.r,{"SOLUSDT":.5,"AVAXUSDT":.5},btc_returns=[.005,.01,-.004,.012,-.008,.006],clusters={"SOLUSDT":"L1","AVAXUSDT":"L1"})
        self.assertGreaterEqual(snap["expected_shortfall_95"],0)
        self.assertAlmostEqual(snap["cluster_concentration"]["L1"],1.0)
        self.assertEqual(snap["execution_effect"],"NONE")

    def test_beta_and_es(self):
        self.assertGreater(beta([1,2,3,4],[1,2,3,4]),.99)
        self.assertGreater(expected_shortfall([-.2,-.1,.01,.02,.03],.8),0)


class TradeMetricsTests(unittest.TestCase):
    def test_excursion_and_margin_efficiency(self):
        x = mfe_mae(100,98,[99,101,103,97],"LONG")
        self.assertEqual(x.mfe_r,1.5)
        self.assertEqual(x.mae_r,-1.5)
        self.assertAlmostEqual(margin_efficiency(1.5,2,4),.75)

    def test_execution_quality(self):
        q=execution_quality_score(100,100.05,5,200)
        self.assertGreater(q["score"],90)

    def test_monte_carlo_is_deterministic(self):
        a=monte_carlo_paths([-1,1.5,.5],risk_fraction=.01,paths=100,trades_per_path=50,seed=1)
        b=monte_carlo_paths([-1,1.5,.5],risk_fraction=.01,paths=100,trades_per_path=50,seed=1)
        self.assertEqual(a,b)
        self.assertGreaterEqual(a["ruin_probability"],0)


class RegistryAndManifestTests(unittest.TestCase):
    def test_experiment_fingerprint_stable(self):
        e=ExperimentRecord("h","d","sha","train","test",{"a":1},7,{"ev":.2},"NOT_PROVEN")
        self.assertEqual(e.experiment_id(),e.experiment_id())

    def test_promotion_and_rollback(self):
        ok, blockers=promotion_evidence(expectancy_r=.2,ci95_low_r=.05,sample_n=200,concentration_share=.3,forward_shadow_consistent=True,costs_included=True)
        self.assertTrue(ok); self.assertFalse(blockers)
        self.assertTrue(rollback_evidence(live_expectancy_r=-.2,expected_expectancy_r=.2,max_allowed_gap_r=.2))

    def test_release_manifest_stable(self):
        kwargs=dict(sha="abc",symbols=["solusdt","BTCUSDT"],parameters={"lev":50},risk_policy={"r":.01},sizing_policy={"s":"x"},drawdown_policy={"mode":"recovery"},execution_chain=["signal","nexus","sizing","predispatch","submit","ack","fill","protect"],strategy_version="1",nexus_version="1",risk_version="1",execution_model_version="binance-usdm-v1")
        a=build_release_manifest(**kwargs); b=build_release_manifest(**kwargs)
        self.assertEqual(a["manifest_sha256"],b["manifest_sha256"])
        self.assertEqual(a["symbols"],["BTCUSDT","SOLUSDT"])


if __name__ == "__main__":
    unittest.main()
