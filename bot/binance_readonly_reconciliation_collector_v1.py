"""Opt-in, read-only Binance USDM reconciliation evidence collector.

No CLI invocation is made automatically by the trading engine. An operator
must explicitly run collect_reconciliation(client, internal, ...). The client
is expected to be an already-authenticated BinanceClient with read-only API
permissions. Never pass API keys, secrets, or account identifiers to reports.
"""
from __future__ import annotations

from datetime import datetime, timezone

from bot.binance_readonly_reconciliation_proof_v1 import compare_readonly_snapshots


async def collect_reconciliation(*, client, internal: dict, internal_captured_at: str) -> dict:
    """Capture independent exchange reads, failing closed on any API error.

    Deliberately no writes, no database mutation, no risk configuration
    mutation and no order submission. A matching snapshot is NOT a full
    financial reconciliation or LIVE authorization.
    """
    try:
        if not internal_captured_at:
            return {"status": "PROOF_MISSING", "reason": "INTERNAL_TIMESTAMP_MISSING"}
        # Raw authenticated endpoint reads avoid adapters which normalize
        # malformed responses into empty lists.
        snapshots = {}
        timestamps = {"internal": internal_captured_at}
        endpoints = {
            "balance": "/fapi/v3/balance",
            "positions": "/fapi/v3/positionRisk",
            "orders": "/fapi/v1/openOrders",
            "algo_orders": "/fapi/v1/openAlgoOrders",
        }
        for name, endpoint in endpoints.items():
            result = await client._get(endpoint, auth=True)
            if name == "algo_orders" and isinstance(result, dict):
                result = result.get("orders")
            if not isinstance(result, list):
                return {"status": "PROOF_MISSING", "reason": name.upper() + "_READ_FAILED"}
            snapshots[name] = result
            timestamps[name] = datetime.now(timezone.utc).isoformat()
        def parsed_time(value):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("timestamp lacks timezone")
            return dt.astimezone(timezone.utc)

        stamps = [parsed_time(timestamps[k]) for k in
                  ("internal", "balance", "positions", "orders", "algo_orders")]
        if (max(stamps) - min(stamps)).total_seconds() > 30:
            return {"status": "PROOF_MISSING", "reason": "SNAPSHOT_TIME_SKEW",
                    "execution_effect": "NONE", "live_allowed": False}
        report = compare_readonly_snapshots(
            balance_rows=snapshots["balance"],
            position_rows=snapshots["positions"],
            order_rows=snapshots["orders"],
            algo_order_rows=snapshots["algo_orders"],
            internal=internal,
            sources=timestamps,
        )
        report["scope"] = "BASIC_SNAPSHOT_ONLY_NOT_FILLS_FEES_FUNDING_OR_FLOWS"
        report["execution_effect"] = "NONE"
        report["live_allowed"] = False
        return report
    except Exception:
        # No raw exchange exception: it might include sensitive request data.
        return {"status": "PROOF_MISSING", "reason": "SIGNED_READ_EXCEPTION",
                "execution_effect": "NONE", "live_allowed": False}
