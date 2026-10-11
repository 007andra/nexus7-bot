"""Research-only candidate JSON projection for prospective OOS.

Never used by production snapshots. The source payload remains authoritative.
Any future SQL projection must pass exact report-equivalence tests first.
"""
import json

ROOT_FIELDS = (
    "counterfactual_nexus_v1", "symbol", "side", "setup", "regime",
    "captured_epoch", "score", "entry", "stop", "cost_snapshot",
)
OBS_FIELDS = (
    "cohort", "risk_epoch_traversal_credit", "candidate_id",
    "execution_allowed", "status", "reason", "reason_category",
    "symbol", "side", "setup", "regime", "score_snapshot",
    "confidence", "risk_reward", "expected_value", "setup_quality",
    "required_equity_at_min_qty",
)
SCORE_FIELDS = ("fusion_confidence", "rr_net", "ev_pct")
COST_FIELDS = ("spread_bps", "taker_fee", "entry_slippage", "exit_slippage")


def project_candidate(raw):
    """Preserve presence/absence and original values; never fabricate defaults."""
    if not isinstance(raw, dict):
        raise TypeError("candidate must be a dict")
    out = {key: raw[key] for key in ROOT_FIELDS if key in raw}
    obs = out.get("counterfactual_nexus_v1")
    if isinstance(obs, dict):
        out["counterfactual_nexus_v1"] = {
            key: obs[key] for key in OBS_FIELDS if key in obs
        }
        score = out["counterfactual_nexus_v1"].get("score_snapshot")
        if isinstance(score, dict):
            out["counterfactual_nexus_v1"]["score_snapshot"] = {
                key: score[key] for key in SCORE_FIELDS if key in score
            }
    cost = out.get("cost_snapshot")
    if isinstance(cost, dict):
        out["cost_snapshot"] = {
            key: cost[key] for key in COST_FIELDS if key in cost
        }
    return out


def byte_comparison(raws):
    """Compact UTF-8 serialization benchmark; no DB writes or SQL changes."""
    original = sum(len(json.dumps(x, ensure_ascii=False).encode("utf-8")) for x in raws)
    projected = sum(
        len(json.dumps(project_candidate(x), ensure_ascii=False).encode("utf-8"))
        for x in raws
    )
    return {"original_bytes": original, "projected_bytes": projected,
            "reduction_pct": round(100 * (1 - projected / original), 2) if original else 0}
