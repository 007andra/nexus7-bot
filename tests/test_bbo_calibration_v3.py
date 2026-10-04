"""BBO Calibration V3 decoupled cohort tests."""
import asyncio
import json

from bot import bbo_calibration_v3 as cal


def cost_payload(cid, *, symbol="SOLUSDT", setup="MOMENTUM", regime="TRENDING_UP",
                 side="LONG", static=29.0, live=16.0, age=100, spread=1.5, valid=True):
    return {
        "candidate_id": cid,
        "symbol": symbol,
        "setup": setup,
        "regime": regime,
        "side": side,
        "bbo_cost_only_observation": {
            "cohort": "COST_ONLY",
            "candidate_id": cid,
            "symbol": symbol,
            "setup": setup,
            "regime": regime,
            "side": side,
            "bbo_valid": valid,
            "bbo_reason": "OK" if valid else "STALE",
            "bbo_age_ms": age,
            "spread_bps": spread,
            "static_total_cost_bps": static,
            "live_spread_only_cost_bps": None if live is None else live - 4.0,
            "live_total_cost_bps": live,
            "cost_reduction_bps": None if live is None else static - live,
            "production_sha": "abc123",
            "shadow_only": True,
            "decision_effect": "NONE",
            "execution_effect": "NONE",
        },
    }


def add_decision_obs(row):
    row = dict(row)
    row["bbo_cost_observation"] = {
        "candidate_id": row["candidate_id"],
        "symbol": row["symbol"],
        "setup": row["setup"],
        "regime": row["regime"],
        "side": row["side"],
        "bbo_valid": True,
        "bbo_age_ms": 90,
        "spread_bps": 1.2,
        "rr_net_static": 1.4,
        "rr_net_live": 1.7,
        "ev_static": -0.1,
        "ev_live": 0.1,
        "would_change_decision": True,
        "costs": {
            "static_total_cost_bps": 29.0,
            "live_plus_static_impact_cost_bps": 16.0,
        },
        "shadow_only": True,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }
    return row


def test_cost_only_counts_without_any_nexus_decision():
    rows = [
        cost_payload("c1", symbol="SOLUSDT"),
        cost_payload("c2", symbol="XRPUSDT", setup="PULLBACK", regime="RANGING"),
    ]
    report = cal.build_report(rows)
    assert report["status"] == "COLLECTING"
    assert report["cost_only_unique_candidates"] == 2
    assert report["cost_only_valid_bbo"] == 2
    assert report["decision_impact_unique_candidates"] == 0
    assert report["decision_impact_valid_bbo"] == 0
    assert abs(report["cost_only_global"]["cost_reduction_bps"]["mean"] - 13.0) < 1e-12
    assert set(report["cost_only_groups"]["symbol"]) == {"SOLUSDT", "XRPUSDT"}
    assert report["promotion_allowed"] is False
    assert report["live_allowed"] is False
    assert report["decision_effect"] == report["execution_effect"] == "NONE"


def test_decision_impact_remains_separate_and_truthful():
    rows = [
        add_decision_obs(cost_payload("with-nexus")),
        cost_payload("pre-nexus-only"),
    ]
    report = cal.build_report(rows)
    assert report["cost_only_unique_candidates"] == 2
    assert report["decision_impact_unique_candidates"] == 1
    assert report["decision_impact_valid_bbo"] == 1
    assert report["decision_impact_global"]["would_change_decision"] == 1


def test_invalid_cost_only_bbo_is_visible_but_not_valid_sample():
    rows = [
        cost_payload("ok"),
        cost_payload("stale", valid=False, live=None, age=9000, spread=None),
    ]
    report = cal.build_report(rows)
    assert report["cost_only_unique_candidates"] == 2
    assert report["cost_only_valid_bbo"] == 1
    assert report["cost_only_global"]["valid_rate"] == 0.5


def test_thresholds_are_cost_only_candidate_level():
    rows50 = [cost_payload(f"c{i}") for i in range(50)]
    rows100 = [cost_payload(f"p{i}") for i in range(100)]
    assert cal.build_report(rows50)["status"] == "MIN_SAMPLE_REACHED"
    assert cal.build_report(rows100)["status"] == "PREFERRED_SAMPLE_REACHED"


def test_duplicate_candidate_not_double_counted():
    one = cost_payload("same")
    report = cal.build_report([one, dict(one)])
    assert report["cost_only_unique_candidates"] == 1
    assert report["cost_only_valid_bbo"] == 1


class DB:
    async def _fetchall(self, sql, args=()):
        assert "hard_gate_shadow_candidates_v1" in sql
        assert args == ("HARD_GATE_SHADOW",)
        return [{"payload": json.dumps(cost_payload("db1"))}]


def test_snapshot_and_summary_are_research_only():
    report = asyncio.run(cal.snapshot(DB()))
    assert report["cost_only_unique_candidates"] == 1
    line = cal.format_summary(report)
    assert "[BBO_CALIBRATION_V3]" in line
    assert "cost_only_valid=1" in line
    assert "decision_impact_valid=0" in line
    assert "promotion_allowed=false" in line
    assert "live_allowed=false" in line
    assert "decision_effect=NONE execution_effect=NONE" in line
