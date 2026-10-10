"""BBO Calibration V2 research-only aggregation tests."""
import asyncio
import json

from bot import bbo_calibration_v2 as cal


def payload(cid, symbol, setup, regime, side, static, live, rr_s, rr_l, ev_s, ev_l,
            age, spread, change=False, valid=True):
    return {
        "candidate_id": cid,
        "symbol": symbol,
        "setup": setup,
        "regime": regime,
        "side": side,
        "bbo_cost_observation": {
            "candidate_id": cid,
            "symbol": symbol,
            "setup": setup,
            "regime": regime,
            "side": side,
            "bbo_valid": valid,
            "bbo_age_ms": age,
            "spread_bps": spread,
            "rr_net_static": rr_s,
            "rr_net_live": rr_l,
            "ev_static": ev_s,
            "ev_live": ev_l,
            "would_change_decision": change,
            "costs": {
                "static_total_cost_bps": static,
                "live_plus_static_impact_cost_bps": live,
            },
            "shadow_only": True,
            "decision_effect": "NONE",
            "execution_effect": "NONE",
        },
    }


def test_build_report_stratifies_and_computes_deltas():
    rows = [
        payload("c1", "BTCUSDT", "BOS_BREAK", "TRENDING_UP", "LONG",
                30, 15, 1.40, 1.75, -0.10, 0.04, 100, 1.2, True),
        payload("c2", "SOLUSDT", "MOMENTUM", "TRENDING_UP", "LONG",
                28, 16, 1.50, 1.80, 0.01, 0.12, 200, 1.8, False),
        payload("c3", "BNBUSDT", "PULLBACK", "TRENDING_DOWN", "SHORT",
                29, 18, 1.60, 1.82, 0.02, 0.08, 300, 2.2, False),
    ]
    report = cal.build_report(rows)
    assert report["status"] == "COLLECTING"
    assert report["unique_candidates"] == 3
    assert report["valid_bbo_candidates"] == 3
    assert abs(report["global"]["static_cost_bps"]["mean"] - 29.0) < 1e-12
    assert abs(report["global"]["bbo_cost_bps"]["mean"] - (49 / 3)) < 1e-12
    assert abs(report["global"]["cost_reduction_bps"]["mean"] - (38 / 3)) < 1e-12
    assert report["global"]["would_change_decision"] == 1
    assert set(report["groups"]["symbol"]) == {"BTCUSDT", "SOLUSDT", "BNBUSDT"}
    assert set(report["groups"]["setup"]) == {"BOS_BREAK", "MOMENTUM", "PULLBACK"}
    assert set(report["groups"]["regime"]) == {"TRENDING_UP", "TRENDING_DOWN"}
    assert set(report["groups"]["side"]) == {"LONG", "SHORT"}
    assert report["outliers"]["delta_rr"][0]["candidate_id"] == "c1"
    assert report["promotion_allowed"] is False and report["live_allowed"] is False
    assert report["decision_effect"] == report["execution_effect"] == "NONE"


def test_duplicate_candidate_is_not_double_counted_and_invalid_bbo_stays_visible():
    one = payload("same", "BTCUSDT", "BOS_BREAK", "TRENDING_UP", "LONG",
                  30, 15, 1.4, 1.7, -0.1, 0.1, 50, 1.0)
    duplicate = dict(one)
    invalid = payload("bad", "ETHUSDT", "MOMENTUM", "RANGING", "LONG",
                      30, None, 1.4, None, -0.1, None, 9000, None, valid=False)
    report = cal.build_report([one, duplicate, invalid])
    assert report["unique_candidates"] == 2
    assert report["valid_bbo_candidates"] == 1
    assert report["global"]["valid_rate"] == 0.5


def test_sample_thresholds_are_candidate_level():
    row = payload("x", "BTCUSDT", "BOS_BREAK", "TRENDING_UP", "LONG",
                  30, 15, 1.4, 1.7, -0.1, 0.1, 50, 1.0)
    fifty = [{**row, "candidate_id": f"c{i}",
              "bbo_cost_observation": {**row["bbo_cost_observation"], "candidate_id": f"c{i}"}}
             for i in range(50)]
    hundred = [{**row, "candidate_id": f"p{i}",
                "bbo_cost_observation": {**row["bbo_cost_observation"], "candidate_id": f"p{i}"}}
               for i in range(100)]
    assert cal.build_report(fifty)["status"] == "MIN_SAMPLE_REACHED"
    assert cal.build_report(hundred)["status"] == "PREFERRED_SAMPLE_REACHED"


class DB:
    async def _fetchall(self, sql, args=()):
        assert "hard_gate_shadow_candidates_v1" in sql
        assert args == ("HARD_GATE_SHADOW",)
        return [{"payload": json.dumps(payload(
            "db1", "BTCUSDT", "BOS_BREAK", "TRENDING_UP", "LONG",
            30, 15, 1.4, 1.7, -0.1, 0.1, 50, 1.0
        ))}]


def test_snapshot_reads_only_research_population():
    report = asyncio.run(cal.snapshot(DB()))
    assert report["unique_candidates"] == 1
    line = cal.format_summary(report)
    assert "[BBO_CALIBRATION_V2]" in line
    assert "promotion_allowed=false" in line
    assert "live_allowed=false" in line
    assert "decision_effect=NONE execution_effect=NONE" in line
