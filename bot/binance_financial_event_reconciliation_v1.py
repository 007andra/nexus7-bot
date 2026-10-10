"""Pure, offline financial event reconciliation for Binance USD-M.

Does not fetch exchange data, alter ledgers, or authorize LIVE. Callers must
provide independently authenticated, complete, time-bounded event populations.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


def _dec(value):
    try:
        v = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid decimal") from exc
    if not v.is_finite():
        raise ValueError("nonfinite decimal")
    return v


def reconcile_financial_events(
    *, trades, income, internal, coverage, tolerance_usdt="0.0001"
):
    """Compare independent fills/commission/funding/transfers by aggregate.

    An exact bounded window and explicit completeness attestations are required.
    Unknown income types and missing mandatory fields fail closed.
    """
    mandatory = ("window_start_ms", "window_end_ms", "trades_complete",
                 "income_complete", "internal_complete")
    if not isinstance(coverage, dict) or any(k not in coverage for k in mandatory):
        return {"status": "PROOF_MISSING", "reason": "COVERAGE_MISSING"}
    if any(coverage[k] is not True for k in
           ("trades_complete", "income_complete", "internal_complete")):
        return {"status": "PROOF_MISSING", "reason": "COVERAGE_INCOMPLETE"}
    if not isinstance(trades, list) or not isinstance(income, list) or not isinstance(internal, dict):
        return {"status": "PROOF_MISSING", "reason": "SOURCE_MISSING"}
    try:
        start, end = int(coverage["window_start_ms"]), int(coverage["window_end_ms"])
        if start <= 0 or end <= start:
            raise ValueError("invalid window")
        fees = Decimal(0)
        fill_count = 0
        for row in trades:
            if not isinstance(row, dict) or "commission" not in row or "time" not in row:
                raise ValueError("invalid fill")
            if not start <= int(row["time"]) <= end:
                raise ValueError("out-of-window fill")
            fees += _dec(row["commission"])
            fill_count += 1
        funding = Decimal(0)
        transfers = Decimal(0)
        for row in income:
            if not isinstance(row, dict) or "incomeType" not in row or "income" not in row or "time" not in row:
                raise ValueError("invalid income")
            if not start <= int(row["time"]) <= end:
                raise ValueError("out-of-window income")
            kind = row["incomeType"]
            if kind == "FUNDING_FEE":
                funding += _dec(row["income"])
            elif kind == "TRANSFER":
                transfers += _dec(row["income"])
            elif kind in ("COMMISSION", "COMMISSION_REBATE", "REALIZED_PNL", "WELCOME_BONUS"):
                # Not all of these represent external cash flows; report
                # separately in a future comprehensive accounting proof.
                pass
            else:
                return {"status": "PROOF_MISSING", "reason": "UNCLASSIFIED_INCOME_TYPE"}
        expected = {"fill_count": fill_count, "fees_usdt": fees,
                    "funding_usdt": funding, "transfers_usdt": transfers}
        if any(k not in internal for k in expected):
            raise ValueError("missing internal aggregate")
        tol = _dec(tolerance_usdt)
        if tol < 0:
            raise ValueError("negative tolerance")
        discrepancies = []
        for k, v in expected.items():
            internal_value = int(internal[k]) if k == "fill_count" else _dec(internal[k])
            if (internal_value != v if k == "fill_count" else abs(internal_value - v) > tol):
                discrepancies.append(k.upper() + "_MISMATCH")
        return {"status": "EVENTS_MATCH" if not discrepancies else "EVENTS_MISMATCH",
                "scope": "BOUNDED_AGGREGATES_ONLY_NOT_FULL_LEDGER_PROOF",
                "fill_count": fill_count, "discrepancies": discrepancies,
                "execution_effect": "NONE", "live_allowed": False}
    except (TypeError, ValueError, KeyError, OverflowError):
        return {"status": "PROOF_MISSING", "reason": "MALFORMED_FINANCIAL_EVENTS",
                "execution_effect": "NONE", "live_allowed": False}
