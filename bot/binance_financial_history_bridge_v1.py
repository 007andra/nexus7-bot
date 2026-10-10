"""Offline bridge from read-only collector outputs to aggregate comparison.

Collector shape-valid responses are NOT exchange completeness evidence.
An independent reviewer must attest bounded source coverage, all relevant
symbols, and internal ledger completeness before aggregate comparison.
"""
from __future__ import annotations

from bot.binance_financial_event_reconciliation_v1 import reconcile_financial_events


def reconcile_collected_history(*, fills_by_symbol, income_result, internal,
                                coverage, required_symbols):
    missing = {"status": "PROOF_MISSING", "reason": "HISTORY_COVERAGE_NOT_PROVEN",
               "execution_effect": "NONE", "live_allowed": False}
    if not isinstance(fills_by_symbol, dict) or not isinstance(income_result, dict):
        return missing
    if not isinstance(required_symbols, list) or not required_symbols or (
            len(set(required_symbols)) != len(required_symbols)):
        return missing
    if set(fills_by_symbol) != set(required_symbols):
        return missing
    if not isinstance(coverage, dict) or coverage.get("symbol_universe_verified") is not True:
        return missing
    if coverage.get("historical_endpoint_limits_reviewed") is not True:
        return missing
    if coverage.get("source_completeness_independently_verified") is not True:
        return missing
    if income_result.get("status") != "INCOME_WINDOW_SHAPE_VALID":
        return missing
    income = income_result.get("records")
    if not isinstance(income, list):
        return missing
    trades = []
    for symbol in required_symbols:
        result = fills_by_symbol.get(symbol)
        if not isinstance(result, dict) or result.get("status") != "FILLS_WINDOW_SHAPE_VALID":
            return missing
        records = result.get("records")
        if not isinstance(records, list) or any(
                not isinstance(row, dict) or row.get("symbol") != symbol
                for row in records):
            return missing
        trades.extend(records)
    if any(coverage.get(k) is not True for k in (
            "trades_complete", "income_complete", "internal_complete")):
        return missing
    return reconcile_financial_events(
        trades=trades, income=income, internal=internal, coverage=coverage)
