"""Offline adapter from collector results to deterministic manifest receipts.

Collector status alone does not prove source completeness or authenticity.
No HTTP calls and no LIVE effects.
"""
from __future__ import annotations

from bot.binance_collector_manifest_pipeline_v1 import validate_collector_receipts


def validate_collector_outputs(*, fills_by_symbol, income_result,
                               required_symbols, start_ms, end_ms):
    missing = {"status": "PROOF_MISSING", "live_allowed": False,
               "execution_effect": "NONE"}
    if (not isinstance(required_symbols, list) or not required_symbols
            or len(set(required_symbols)) != len(required_symbols)
            or "income" in required_symbols
            or not isinstance(fills_by_symbol, dict)
            or set(fills_by_symbol) != set(required_symbols)
            or not isinstance(income_result, dict)):
        return missing
    records = {}
    terminal = {}
    for symbol in required_symbols:
        result = fills_by_symbol[symbol]
        if not isinstance(result, dict) or result.get("status") not in (
                "FILLS_WINDOW_SHAPE_VALID", "FILLS_WINDOWS_SHAPE_VALID"):
            return missing
        if (result.get("start_ms") != start_ms
                or result.get("end_ms") != end_ms
                or result.get("terminal_page_verified") is not True):
            return missing
        if result.get("live_allowed") is not False or result.get("execution_effect") != "NONE":
            return missing
        records[symbol] = result.get("records")
        terminal[symbol] = True
    if (income_result.get("status") != "INCOME_WINDOW_SHAPE_VALID"
            or income_result.get("start_ms") != start_ms
            or income_result.get("end_ms") != end_ms
            or income_result.get("terminal_page_verified") is not True):
        return missing
    if income_result.get("live_allowed") is not False or income_result.get("execution_effect") != "NONE":
        return missing
    records["income"] = income_result.get("records")
    terminal["income"] = True
    return validate_collector_receipts(
        records_by_source=records, required_sources=required_symbols + ["income"],
        start_ms=start_ms, end_ms=end_ms, terminal_by_source=terminal)
