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
            "algo_orders": "/fapi/v1/algoOpenOrders",
        }
        for name, endpoint in endpoints.items():
            result = await client._get(endpoint, auth=True)
            if not isinstance(result, list):
                return {"status": "PROOF_MISSING", "reason": name.upper() + "_READ_FAILED"}
            snapshots[name] = result
            timestamps[name] = datetime.now(timezone.utc).isoformat()
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
