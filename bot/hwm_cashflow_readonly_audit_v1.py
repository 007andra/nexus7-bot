"""#601 offline-safe redacted, single-SELECT PostgreSQL ledger/HWM provenance check.

Only operator-invoked via an existing authorized private Railway process.
No bot runtime imports, migration, DB init/DDL, exchange API, or key mutations.
Never emits raw ledger, SQL binds, identifiers, credentials or evidence refs.
"""
from __future__ import annotations

import asyncio
import json
import math
import os

LOG_TAG = "HWM_CASHFLOW_READONLY_AUDIT_V1"
LEDGER_KEY = "risk:external_cash_flows:binance:ledger:v1"
CURSOR_KEY = "risk:external_capital_flow:binance:seen_transfers:v1"
WALLET_KEY = "risk:external_cash_flows:binance:wallet_baseline:v1"
SELECT_SQL = "SELECT key,value FROM key_value WHERE key=ANY($1::text[])"
SETTINGS = {
    "default_transaction_read_only": "on",
    "statement_timeout": "700",
    "application_name": "nexus7-hwm-provenance-readonly-v1",
}
ALLOWED_REASONS = {
    "bootstrap", "new_equity_high", "incident_repair",
    "external_capital_flow_rebase", "external_position_performance_rebase",
}


def _number(value, *, positive=False):
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(f) or (positive and f <= 0):
        return None
    return f


def _json_dict(raw):
    try:
        obj = json.loads(raw) if isinstance(raw, str) else None
    except (TypeError, ValueError):
        obj = None
    return obj if isinstance(obj, dict) else None


def review_values(values, *, peak_key, provenance_key):
    """Aggregate only; no raw IDs, wallet values or evidence_ref ever leave."""
    errors = []
    raw_ledger = _json_dict(values.get(LEDGER_KEY))
    peak = _number(values.get(peak_key), positive=True)
    provenance = _json_dict(values.get(provenance_key))
    cursor = _json_dict(values.get(CURSOR_KEY))
    baseline = _json_dict(values.get(WALLET_KEY))
    if peak is None:
        errors.append("HWM_MISSING_OR_INVALID")
    if raw_ledger is None or raw_ledger.get("version") != 1:
        errors.append("LEDGER_MISSING_OR_INVALID")
    if provenance is None or provenance.get("version") != 1:
        errors.append("PROVENANCE_MISSING_OR_INVALID")
    if cursor is None or cursor.get("version") != 1:
        errors.append("CURSOR_MISSING_OR_INVALID")
    if baseline is None or baseline.get("version") != 1:
        errors.append("WALLET_BASELINE_MISSING_OR_INVALID")

    records = raw_ledger.get("applied") if raw_ledger else None
    pending = raw_ledger.get("pending") if raw_ledger else None
    if not isinstance(records, list) or not isinstance(pending, list):
        errors.append("LEDGER_COLLECTIONS_INVALID")
        records, pending = [], []
    reconciled_flow_ids = set()
    reconciliations = set()
    total_net = 0.0
    flow_n = 0
    for record in records:
        if not isinstance(record, dict):
            errors.append("RECONCILIATION_RECORD_MALFORMED")
            continue
        rec_id = record.get("reconciliation_id")
        ids = record.get("identities")
        if not isinstance(rec_id, str) or not rec_id or rec_id in reconciliations:
            errors.append("RECONCILIATION_ID_INVALID_OR_DUPLICATE")
        else:
            reconciliations.add(rec_id)
        if not isinstance(ids, list) or not ids:
            errors.append("APPLIED_FLOW_IDENTITIES_MISSING")
            continue
        if any(not isinstance(i, str) or not i for i in ids):
            errors.append("APPLIED_FLOW_IDENTITIES_INVALID")
            continue
        if len(set(ids)) != len(ids) or set(ids) & reconciled_flow_ids:
            errors.append("DUPLICATE_APPLIED_FLOW")
        reconciled_flow_ids.update(ids)
        flow_n += len(ids)
        pre = _number(record.get("pre_flow_equity"), positive=True)
        post = _number(record.get("post_flow_equity"), positive=True)
        prior = _number(record.get("previous_hwm"), positive=True)
        adjusted = _number(record.get("adjusted_hwm"), positive=True)
        net = _number(record.get("net_amount"))
        if None in (pre, post, prior, adjusted, net):
            errors.append("TWR_EVIDENCE_INCOMPLETE")
            continue
        if abs(pre + net - post) > 1e-6 * max(1.0, post):
            errors.append("FLOW_PRE_POST_MISMATCH")
        expected = max(prior, pre) * (post / pre)
        # One-way reconstruction mirrors the canonical cash_flow_ledger rule:
        # adjusted HWM >= both TWR rebased HWM and equity at reconciliation.
        equity_at = _number(record.get("equity_at_reconciliation"), positive=True)
        if equity_at is None:
            errors.append("RECONCILIATION_EQUITY_MISSING")
        else:
            expected = max(expected, equity_at)
        if adjusted is not None and abs(expected - adjusted) > 1e-6 * max(1, adjusted):
            errors.append("TWR_REBASE_MISMATCH")
        total_net += net
    for item in pending:
        if not isinstance(item, dict) or not item.get("identity"):
            errors.append("PENDING_FLOW_MALFORMED")
        elif item["identity"] in reconciled_flow_ids:
            errors.append("PENDING_ALREADY_APPLIED")
    if baseline is not None and (
            _number(baseline.get("wallet"), positive=True) is None
            or not isinstance(baseline.get("anchor_ms"), int)):
        errors.append("WALLET_BASELINE_UNVERIFIABLE")
    if provenance is not None:
        prov_peak = _number(provenance.get("new_peak"), positive=True)
        if prov_peak is None or peak is None or abs(prov_peak - peak) > 1e-6 * max(1.0, peak):
            errors.append("HWM_PROVENANCE_PEAK_MISMATCH")
        if provenance.get("reason") not in ALLOWED_REASONS:
            errors.append("HWM_PROVENANCE_REASON_UNRECOGNIZED")
        if not isinstance(provenance.get("evidence_ref"), str) or not provenance["evidence_ref"]:
            errors.append("HWM_EVIDENCE_REF_MISSING")
    if cursor is not None and (
            not isinstance(cursor.get("seen"), list)
            or any(not isinstance(s, str) for s in cursor.get("seen", []))):
        errors.append("CURSOR_IDENTITIES_MALFORMED")

    errors = sorted(set(errors))
    return {
        "tag": LOG_TAG,
        "status": "STRUCTURAL_CHECKS_PASSED_INDEPENDENT_EXCHANGE_AUDIT_PENDING"
                  if not errors else "EVIDENCE_INCONSISTENT_OR_INCOMPLETE",
        "blockers": errors or ["EXCHANGE_INCOME_CROSSCHECK_MISSING",
                               "FULL_HWM_PROVENANCE_HISTORY_NOT_IN_KEY_VALUE"],
        "applied_reconciliation_records": len(records),
        "applied_transfer_events": flow_n,
        "applied_external_net_usdt": round(total_net, 8),
        "pending_transfer_events": len(pending),
        "durable_hwm_usdt": None if peak is None else round(peak, 8),
        "latest_hwm_provenance_present": provenance is not None,
        "wallet_baseline_present": baseline is not None,
        "exchange_income_verified": False,
        "full_hwm_history_verified": False,
        "read_only": True, "mutation_effect": "NONE",
        "research_only": True, "promotion_allowed": False,
        "live_allowed": False, "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


async def collect_once(conn, *, peak_key, provenance_key):
    keys = (LEDGER_KEY, CURSOR_KEY, WALLET_KEY, peak_key, provenance_key)
    if any(not isinstance(k, str) or not k for k in keys):
        raise ValueError("INVALID_READONLY_LEDGER_KEYS")
    rows = await asyncio.wait_for(conn.fetch(SELECT_SQL, list(keys)), timeout=1.0)
    kv = {r["key"]: r["value"] for r in rows if r["key"] in keys}
    return review_values(kv, peak_key=peak_key, provenance_key=provenance_key)


async def _main():
    # No bot.database.init() call here: it would run table-init DDL.
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn.startswith(("postgresql://", "postgres://")):
        raise ValueError("POSTGRES_CONNECTION_UNAVAILABLE")
    # Key computation only; never prints the account/database fingerprint.
    from bot import hwm_namespace
    peak_key = hwm_namespace.equity_peak_key()
    provenance_key = hwm_namespace.provenance_key()
    import asyncpg
    conn = await asyncpg.connect(dsn, timeout=5.0, server_settings=SETTINGS)
    try:
        return await collect_once(conn, peak_key=peak_key,
                                  provenance_key=provenance_key)
    finally:
        await conn.close(timeout=2.0)


def main():
    try:
        result = asyncio.run(_main())
    except Exception:
        # Exception messages can contain credentials or sensitive ledger data.
        result = {"tag": LOG_TAG, "status": "UNAVAILABLE_OR_FAILED_CLOSED",
                  "read_only": True, "live_allowed": False,
                  "execution_effect": "NONE"}
        print(json.dumps(result, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
