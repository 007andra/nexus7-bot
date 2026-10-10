"""Research-only per-candidate 60/240 replay to cost calculation bridge.

Never turns missing execution data into a profitable simulated fill.
"""
from __future__ import annotations

from bot.binance_usdm_oos_horizon_replay import replay_horizon
from bot.binance_usdm_oos_net_costs import evaluate_costs
from bot.binance_usdm_oos_source_proof import verify_trade_record


def evaluate_candidate(*, symbol: str, candidate_id: str, side: str,
                       decision_epoch_ms: int, entry: str, stop: str,
                       target: str, quantity: str, capital_usdt: str,
                       costs: dict, proofs: dict, transport,
                       source_evidence: dict | None = None) -> dict:
    results = {}
    for horizon in (60, 240):
        path = replay_horizon(
            symbol=symbol, side=side, decision_epoch_ms=decision_epoch_ms,
            horizon_minutes=horizon, entry=entry, stop=stop, target=target,
            transport=transport)
        result = {"path": path, "status": "NET_PROOF_MISSING",
                  "missing": [], "live_allowed": False,
                  "execution_effect": "NONE"}
        if path["status"] != "PATH_OBSERVED_NET_UNPROVEN":
            result["missing"].append(path.get("reason", "NO_EXECUTABLE_PATH"))
        else:
            # Prices from historical OHLC are barrier observations, not fills.
            # Explicit independent fill evidence is required before net arithmetic.
            exit_fill_verified = proofs.get("exit_fill_price") is True
            source = source_evidence if isinstance(source_evidence, dict) else {}
            verified_records = {}
            for label in ("entry", "exit"):
                item = source.get(label)
                if not isinstance(item, dict) or not isinstance(item.get("raw"), bytes):
                    continue
                verified_records[label] = verify_trade_record(
                    record=item.get("record"), raw=item["raw"],
                    expected_sha256=item.get("sha256"),
                    candidate_id=candidate_id,
                    decision_epoch_ms=decision_epoch_ms,
                    horizon_end_ms=decision_epoch_ms + horizon * 60_000)
            result["source_proof"] = verified_records
            # Observed historical trades are not evidence of our hypothetical fills.
            # A caller's self-attested boolean cannot authorize net calculation.
            result["missing"].extend(("ENTRY_FILL_UNVERIFIED", "EXIT_FILL_UNVERIFIED"))
            if proofs.get("entry_fill_price") is True or proofs.get("exit_fill_price") is True:
                result["missing"].append("FILL_FLAGS_ARE_NOT_INDEPENDENT_PROOF")
        results[horizon] = result
    return {"candidate_id": candidate_id, "symbol": symbol,
            "horizons": results, "research_only": True,
            "promotion_allowed": False, "live_allowed": False,
            "decision_effect": "NONE", "execution_effect": "NONE"}
