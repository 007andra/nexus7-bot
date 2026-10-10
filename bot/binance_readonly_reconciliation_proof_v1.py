"""Pure read-only Binance USD-M reconciliation proof.

This module performs no exchange or database I/O. Callers must obtain fresh,
authenticated snapshots independently and supply their source timestamps.
Never feed it credentials or unredacted account identifiers.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def _amount(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid monetary amount") from exc
    if not amount.is_finite():
        raise ValueError("nonfinite monetary amount")
    return amount


def compare_readonly_snapshots(
    *,
    balance_rows: list[dict],
    position_rows: list[dict],
    order_rows: list[dict],
    algo_order_rows: list[dict],
    internal: dict,
    sources: dict,
    tolerance_usdt: str = "0.0001",
) -> dict:
    """Fail closed on absent, stale or mismatched independently sourced data.

    sources requires non-empty capture timestamps for balance, positions,
    orders, algo_orders and internal. Never infer a zero count from a missing
    response. No sensitive order/account identifiers appear in the report.
    """
    required = ("balance", "positions", "orders", "algo_orders", "internal")
    if any(not isinstance(sources.get(k), str) or not sources[k].strip() for k in required):
        return {"status": "PROOF_MISSING", "reason": "SOURCE_TIMESTAMP_MISSING"}
    if not all(isinstance(x, list) for x in (balance_rows, position_rows, order_rows, algo_order_rows)):
        return {"status": "PROOF_MISSING", "reason": "EXCHANGE_SNAPSHOT_MISSING"}
    if not isinstance(internal, dict):
        return {"status": "PROOF_MISSING", "reason": "INTERNAL_SNAPSHOT_MISSING"}
    try:
        tol = _amount(tolerance_usdt)
        if tol < 0:
            raise ValueError("negative tolerance")
        usdt = [row for row in balance_rows if isinstance(row, dict) and row.get("asset") == "USDT"]
        if len(usdt) != 1:
            return {"status": "PROOF_MISSING", "reason": "USDT_BALANCE_AMBIGUOUS"}
        wallet = _amount(usdt[0]["balance"])
        available = _amount(usdt[0]["availableBalance"])
        unrealized = _amount(usdt[0]["crossUnPnl"])
        exchange_equity = wallet + unrealized
        internal_equity = _amount(internal["equity_usdt"])
        active = [p for p in position_rows if _amount(p["positionAmt"]) != 0]
        discrepancies = []
        if abs(exchange_equity - internal_equity) > tol:
            discrepancies.append("EQUITY_MISMATCH")
        if len(active) != int(internal["active_positions"]):
            discrepancies.append("POSITION_COUNT_MISMATCH")
        if len(order_rows) != int(internal["open_orders"]):
            discrepancies.append("OPEN_ORDER_COUNT_MISMATCH")
        if len(algo_order_rows) != int(internal["algo_orders"]):
            discrepancies.append("ALGO_ORDER_COUNT_MISMATCH")
        return {
            "status": "RECONCILIATION_FAIL" if discrepancies else "BASIC_SNAPSHOT_MATCH",
            "scope": "BASIC_COUNTS_AND_EQUITY_ONLY_NOT_FULL_RECONCILIATION",
            "exchange_equity_usdt": str(exchange_equity),
            "internal_equity_usdt": str(internal_equity),
            "available_usdt": str(available),
            "active_positions": len(active),
            "open_orders": len(order_rows),
            "algo_orders": len(algo_order_rows),
            "discrepancies": discrepancies,
            "execution_effect": "NONE",
            "live_allowed": False,
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"status": "PROOF_MISSING", "reason": "MALFORMED_SNAPSHOT"}
