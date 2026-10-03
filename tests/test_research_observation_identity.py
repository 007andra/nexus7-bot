"""Research observation identity vs setup identity (INV-SETUP-ID-001,
INV-RESEARCH-OBS-ID-001) and LIVE isolation of research/shadow modules.

SETUP IS NOT AN OBSERVATION. OBSERVATION IS NOT A FINANCIAL INTENT.
"""
import ast
import asyncio
import os
import random
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("EXCHANGE", "binance")

from bot.candidate_trace import (  # noqa: E402
    attach_decision, bind_managed_order, build_candidate_id, ensure_candidate_id,
)
from bot.microstructure_oos_evidence import evaluate_microstructure_ranking  # noqa: E402
from bot.opportunity_ranker import (  # noqa: E402
    Opportunity, apply_cross_sectional_liquidity, evaluate_ranked_outcomes,
    rank_opportunities, rank_score,
)
from bot.research_observation_identity import build_observation_id  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
T1, T2 = 1_710_000_000_000, 1_710_000_900_000


def _sig(**overrides):
    base = dict(symbol="ETHUSDT", direction="LONG", entry=100.0, sl=99.5, tp=104.0,
                _bgx_formation_bucket=1_900_000, entry_type="PULLBACK", regime="TREND", score=80)
    base.update(overrides)
    return SimpleNamespace(**base)


SETUP = build_candidate_id(_sig())


def _opp(ts, *, depth=None, cid=SETUP, sym="ETHUSDT", side="LONG", ev=1.0):
    return Opportunity(candidate_id=cid, symbol=sym, expected_value=ev, net_rr=2.0, setup_score=70,
                       liquidity_score=50, regime_confidence=60, round_trip_cost=0.001,
                       decision_ts=ts, side=side, confidence=70, depth_notional_1pct=depth)


def _row(ts, r, *, sym="ETHUSDT", cid=SETUP, micro=0.2, ev=1.0):
    return {"candidate_id": cid, "timestamp": ts, "direction": "LONG",
            "nexus_expected_value_pct": ev, "nexus_rr_net": 2.0, "nexus_setup_quality": 70,
            "nexus_regime_compat": 60, "nexus_confidence": 70, "round_trip_cost": 0.001,
            "r_multiple": r, "nexus_market_regime": "TREND",
            "shadow_microstructure": {"available": True, "execution_effect": "NONE",
                                      "score_effect": "NONE", "directional_alignment": micro,
                                      "taker_pressure": micro}}


class ObservationIdentityTests(unittest.TestCase):
    def test_setup_id_repeats_for_equivalent_setups(self):
        self.assertEqual(build_candidate_id(_sig()), build_candidate_id(_sig()))   # INV-SETUP-ID-001

    def test_A_same_setup_two_timestamps_distinct_observations(self):
        a, b = _opp(T1), _opp(T2)
        self.assertEqual(a.candidate_id, b.candidate_id)
        self.assertNotEqual(a.observation_id, b.observation_id)

    def test_B_replay_same_event_same_observation(self):
        self.assertEqual(_opp(T1).observation_id, _opp(T1).observation_id)
        self.assertEqual(build_observation_id(SETUP, T1, "ethusdt", "long"),
                         build_observation_id(SETUP, T1, "ETHUSDT", "LONG"))

    def test_observation_id_is_causal_outcome_cannot_change_it(self):
        row_a, row_b = _row(T1, -1.0), _row(T1, 3.0)                   # same event, other future
        ids = []
        for row in (row_a, row_b):
            from bot.microstructure_oos_evidence import base_opportunity
            ids.append(base_opportunity("ETHUSDT", row).observation_id)
        self.assertEqual(ids[0], ids[1], "outcome never enters the observation identity")

    def test_C_distinct_observations_with_different_r_both_retained(self):
        a, b = _opp(T1), _opp(T2)
        report = evaluate_ranked_outcomes([a, b], {a.observation_id: -1.0, b.observation_id: 2.5})
        self.assertEqual(report["n"], 2)
        self.assertEqual(sorted([report["top_half_expectancy_r"], report["bottom_half_expectancy_r"]]),
                         [-1.0, 2.5])

    def test_ambiguous_setup_keyed_outcomes_fail_closed(self):
        a, b = _opp(T1), _opp(T2)
        with self.assertRaisesRegex(ValueError, "ambiguous setup-keyed realized_r"):
            evaluate_ranked_outcomes([a, b], {SETUP: 2.5})

    def test_D_percentile_map_does_not_overwrite(self):
        out = apply_cross_sectional_liquidity([_opp(T1, depth=1e6), _opp(T2, depth=5e6),
                                               _opp(T1, depth=3e6, cid="other", sym="BTCUSDT")])
        self.assertEqual([o.liquidity_score for o in out], [0.0, 100.0, 50.0])

    def test_E_outcome_rank_contains_both_observations(self):
        a, b, c = _opp(T1), _opp(T2), _opp(T1, cid="o", sym="BTCUSDT", ev=3.0)
        report = evaluate_ranked_outcomes(
            [a, b, c], {a.observation_id: -1.0, b.observation_id: 2.5, c.observation_id: 0.5})
        self.assertEqual(report["n"], 3)
        ranked_ids = [item.observation_id for item, _ in rank_opportunities([a, b, c])]
        self.assertEqual(len(set(ranked_ids)), 3)

    def test_F_true_duplicate_observation_fails_closed(self):
        a = _opp(T1)
        with self.assertRaisesRegex(ValueError, "duplicate research observation_id"):
            apply_cross_sectional_liquidity([a, a])
        with self.assertRaisesRegex(ValueError, "duplicate research observation_id"):
            evaluate_microstructure_ranking([
                {"symbol": "ETHUSDT", "candidate_diagnostics": [_row(T1, 1.0), _row(T1, 1.0)]}])

    def test_temporal_replay_keeps_repeated_setup_separate_in_ledger(self):
        report = evaluate_microstructure_ranking([
            {"symbol": "ETHUSDT", "candidate_diagnostics": [_row(T1, -1.0), _row(T2, 2.5)]},
            {"symbol": "BTCUSDT", "candidate_diagnostics": [_row(T1, 0.5, sym="BTCUSDT", cid="b"),
                                                            _row(T2, 0.1, sym="BTCUSDT", cid="b")]},
        ])
        self.assertEqual(report["comparable_candidates"], 4)
        self.assertEqual(report["comparable_batches"], 2)
        r_by_ts = {b["decision_ts"]: (b["base_top_r"], b["enriched_top_r"]) for b in report["batches"]}
        self.assertEqual(set(r_by_ts), {T1, T2})

    def test_rank_score_and_weights_unchanged_by_identity(self):
        a = _opp(T1)
        b = Opportunity(**{**a.__dict__, "observation_id": "obs-other"})
        self.assertEqual(rank_score(a), rank_score(b))
        self.assertAlmostEqual(rank_score(a), 0.24 * 0.2 + 0.16 * 0.5 + 0.18 * 0.7 + 0.12 * 0.5
                               + 0.12 * 0.6 + 0.08 * 0.7 - 0.16 * 0.2)


class ObservationIdentityPropertyTests(unittest.TestCase):
    def test_property_unique_events_unique_ids_and_replay_stable(self):
        rng = random.Random(466)
        events, ids = set(), {}
        for _ in range(5000):
            setup = rng.choice([SETUP, "nx7-a", "nx7-b"])          # heavy setup repetition
            sym = rng.choice(["BTCUSDT", "ETHUSDT"])
            side = rng.choice(["LONG", "SHORT"])
            ts = 1_700_000_000_000 + rng.randint(0, 400) * 900_000
            event = (setup, ts, sym, side)
            oid = build_observation_id(setup, ts, sym, side)
            if event in ids:
                self.assertEqual(ids[event], oid, "replay of the same event -> same id")
            ids[event] = oid
            events.add(event)
        self.assertEqual(len(set(ids.values())), len(events), "unique events -> unique ids")
        self.assertGreater(len(events), 2000)


class BlockBootstrapTests(unittest.TestCase):
    def test_block_bootstrap_widens_ci_for_autocorrelated_series(self):
        from bot.research_statistics import block_bootstrap_mean_ci, bootstrap_mean_ci
        series = [1.0] * 40 + [-1.0] * 40 + [1.0] * 40 + [-1.0] * 40     # strong serial dependence
        iid = bootstrap_mean_ci(series, n_bootstrap=3000, seed=7)
        block = block_bootstrap_mean_ci(series, block_length=20, n_bootstrap=3000, seed=7)
        self.assertGreater(block["high"] - block["low"], 2 * (iid["high"] - iid["low"]))
        self.assertEqual(block_bootstrap_mean_ci(series, block_length=20, n_bootstrap=3000, seed=7), block)

    def test_evidence_uses_block_bootstrap(self):
        report = evaluate_microstructure_ranking([
            {"symbol": "ETHUSDT", "candidate_diagnostics": [_row(T1 + i * 900_000, (-1.0) ** i) for i in range(8)]},
            {"symbol": "BTCUSDT", "candidate_diagnostics": [
                _row(T1 + i * 900_000, 0.2, sym="BTCUSDT", cid="b") for i in range(8)]},
        ])
        self.assertEqual(report["top_pick_uplift_ci95"]["method"], "CIRCULAR_BLOCK")
        self.assertEqual(report["bootstrap_method"], "CIRCULAR_BLOCK_OVER_TIME_ORDERED_TIMESTAMP_CLUSTERS")
        self.assertIn("effective_sample_size", report)


class CandidateMetadataSafetyTests(unittest.TestCase):
    class _Frozen:
        __slots__ = ("symbol", "direction", "entry", "sl", "tp", "entry_type", "regime",
                     "score", "_bgx_formation_bucket")

        def __init__(self, **kw):
            for k, v in kw.items():
                object.__setattr__(self, k, v)

    def test_K_setattr_failure_leaves_financial_identity_unchanged(self):
        kw = dict(symbol="ETHUSDT", direction="LONG", entry=100.0, sl=99.5, tp=104.0,
                  entry_type="PULLBACK", regime="TREND", score=80, _bgx_formation_bucket=1_900_000)
        frozen = self._Frozen(**kw)
        with self.assertRaises(AttributeError):
            frozen._bgx_setup_id = "x"                      # metadata cannot attach
        self.assertEqual(ensure_candidate_id(frozen), build_candidate_id(SimpleNamespace(**kw)))
        self.assertEqual(ensure_candidate_id(frozen), ensure_candidate_id(frozen), "stable key")
        decision = self._Frozen(**kw)
        self.assertEqual(attach_decision(decision, frozen), ensure_candidate_id(frozen))

    def test_bind_managed_order_stays_fail_closed(self):
        sig = _sig()
        with self.assertRaisesRegex(ValueError, "cannot retain candidate identity"):
            bind_managed_order(self._Frozen(), sig)
        order = SimpleNamespace(candidate_id="nx7-other")
        with self.assertRaisesRegex(ValueError, "candidate identity conflict"):
            bind_managed_order(order, sig)


def _live_closure():
    entries = {"main_hardened", "main", "sitecustomize"}

    def path_of(mod):
        p = ROOT.joinpath(*mod.split(".")).with_suffix(".py")
        if p.exists():
            return p
        p = ROOT.joinpath(*mod.split("."), "__init__.py")
        return p if p.exists() else None

    seen, stack = set(), list(entries)
    while stack:
        mod = stack.pop()
        path = path_of(mod)
        if path is None or mod in seen:
            continue
        seen.add(mod)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for name in names:
                if (name.startswith("bot") or name in entries) and name not in seen:
                    stack.append(name)
    return seen


class LiveIsolationTests(unittest.TestCase):
    FORBIDDEN = ("research_observation_identity", "opportunity_ranker", "microstructure",
                 "model_h", "market_language", "binance_oos", "research_walk_forward",
                 "research_statistics", "research_protocol", "champion_challenger")

    def test_L_research_model_h_and_microstructure_unreachable_from_live(self):
        closure = _live_closure()
        self.assertIn("bot.engine", closure)
        leaked = sorted(m for m in closure if any(k in m for k in self.FORBIDDEN))
        self.assertEqual(leaked, [])


class ShadowDriftIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_J_shadow_drift_zero_financial_mutation(self):
        from bot import nexus_shadow_drift
        from bot.config import cfg
        from bot.nexus_validation_observability import observe_nexus_validation
        guarded = ("NEXUS_MIN_SCORE", "MIN_ENTRY_SCORE", "MIN_RR_RATIO", "LEVERAGE",
                   "MAX_RISK_PCT", "MAX_DRAWDOWN", "MAX_POSITIONS", "MIN_CONFIDENCE")
        before_cfg = {k: getattr(cfg, k, None) for k in guarded}
        calls = []

        class Sentinel:
            def __getattr__(self, name):
                if name in {"place_order", "set_position_stops", "set_sl", "cancel_algo_order",
                            "cancel_all_orders", "set_leverage", "_post", "_delete", "_request"}:
                    async def forbidden(*a, **k):
                        calls.append(name)
                        raise AssertionError(f"financial mutation via {name}")
                    return forbidden
                raise AttributeError(name)

        decision = SimpleNamespace(execution_allowed=True, confidence=81.0, setup_quality=77.0,
                                   expected_value=1.2, risk_reward=2.4, decision="LONG")
        frozen = dict(decision.__dict__)
        rows = [{"ts": float(i), "symbol": "ETHUSDT", "side": "LONG", "approved": i % 2 == 0,
                 "nexus_score": 40.0 + (i % 50), "confidence": 30.0 + (i % 60),
                 "regime": "TREND" if i < 100 else "RANGE",
                 "raw": {"_signal_score": 60 + i % 30, "_cost": {"taker_fee": 0.0005 + i * 1e-6}},
                 "shadow_status": "CLOSED", "shadow_r": (-1.0) ** i} for i in range(300)]
        engine = SimpleNamespace(client=Sentinel())

        async def canonical(self, sig):
            return decision
        wrapped = observe_nexus_validation(canonical)
        with patch("bot.nexus_persistence.record_decision", AsyncMock()), \
                patch("bot.nexus_persistence.evaluate_pending", AsyncMock()), \
                patch("bot.nexus_persistence.recent_observations", AsyncMock(return_value=rows)):
            out = await wrapped(engine, _sig())
            await asyncio.sleep(0)
            drift = await nexus_shadow_drift.refresh(force=True)
        self.assertIs(out, decision)
        frozen["_bgx_candidate_id"] = build_candidate_id(_sig())
        self.assertEqual(decision.__dict__, frozen, "only lineage metadata attached")
        self.assertEqual({k: getattr(cfg, k, None) for k in guarded}, before_cfg)
        self.assertEqual(calls, [])
        self.assertEqual(drift.get("execution_effect"), "NONE")


if __name__ == "__main__":
    unittest.main()
