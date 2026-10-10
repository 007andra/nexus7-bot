"""Research-only per-candidate 60/240 replay to cost calculation bridge.

Never turns missing execution data into a profitable simulated fill.
"""
from __future__ import annotations

from bot.binance_usdm_oos_horizon_replay import replay_horizon
from bot.binance_usdm_oos_net_costs import evaluate_costs


def evaluate_candidate(*, symbol: str, candidate_id: str, side: str,
                       decision_epoch_ms: int, entry: str, stop: str,
                       target: str, quantity: str, capital_usdt: str,
                       costs: dict, proofs: dict, transport) -> dict:
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
            entry_fill_verified = proofs.get("entry_fill_price") is True
            if not entry_fill_verified:
                result["missing"].append("ENTRY_FILL_UNVERIFIED")
            if not exit_fill_verified:
                result["missing"].append("EXIT_FILL_UNVERIFIED")
            if entry_fill_verified and exit_fill_verified:
                evidence = {
                    "entry_price": entry,
                    "exit_price": path["path"]["exit_price"],
                    "quantity": quantity,
                    "capital_usdt": capital_usdt,
                    **costs,
                }
                result["net"] = evaluate_costs(
                    side=side, evidence=evidence, proofs=proofs)
                result["status"] = result["net"]["status"]
                result["missing"] = result["net"].get("missing", [])
        results[horizon] = result
    return {"candidate_id": candidate_id, "symbol": symbol,
            "horizons": results, "research_only": True,
            "promotion_allowed": False, "live_allowed": False,
            "decision_effect": "NONE", "execution_effect": "NONE"}
