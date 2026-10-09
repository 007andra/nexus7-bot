"""HWM namespace migration preflight: STRICTLY READ-ONLY, redacted, future-only.

No DDL, key migrations, upserts, startup hooks, exchange access or changes to
risk gates. This reports which physical namespace contains HWM+provenance.
Historical completeness and account income authenticity are NOT established.

Operator-only: python -m bot.hwm_legacy_migration_preflight_ro_v1
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import os

TAG = "HWM_NAMESPACE_MIGRATION_PREFLIGHT_RO_V1"
SQL = "SELECT key,value FROM key_value WHERE key=ANY($1::text[])"
SETTINGS = {
    "default_transaction_read_only": "on",
    "statement_timeout": "700",
    "application_name": "nexus7-hwm-legacy-readonly-preflight",
}
V1_PEAK = "risk:account_equity_peak:v1"
V1_PROV = "risk:account_equity_peak:provenance:v1"
ALLOWED_REASONS = frozenset({
    "bootstrap", "new_equity_high", "incident_repair",
    "external_capital_flow_rebase", "external_position_performance_rebase",
})


def _positive(v):
    if isinstance(v, bool):
        return None
    try:
        x = Decimal(str(v))
    except (ValueError, TypeError, InvalidOperation):
        return None
    return x if x.is_finite() and x > 0 else None


def _provenance(raw, peak):
    if peak is None or not isinstance(raw, str):
        return False
    try:
        p = json.loads(raw)
        if not isinstance(p, dict) or p.get("version") != 1:
            return False
        candidate = _positive(p.get("new_peak"))
        when = datetime.fromisoformat(p["recorded_at"].replace("Z", "+00:00"))
        return bool(
            candidate is not None
            and abs(candidate - peak) <= Decimal("0.000001") * max(Decimal(1), peak)
            and when.tzinfo is not None and when.utcoffset() is not None
            and p.get("reason") in ALLOWED_REASONS
            and isinstance(p.get("evidence_ref"), str)
            and bool(p["evidence_ref"].strip())
            and p.get("execution_effect") == "NONE"
        )
    except (TypeError, KeyError, ValueError, OverflowError, AttributeError):
        return False


def physical_keys():
    from bot import hwm_namespace
    from bot import financial_namespace

    current_peak = hwm_namespace.equity_peak_key()
    current_prov = hwm_namespace.provenance_key()
    old_peak = financial_namespace.legacy_key_for(current_peak)
    old_prov = financial_namespace.legacy_key_for(current_prov)
    if not all(isinstance(k, str) and k for k in (current_peak, current_prov)):
        raise ValueError("INVALID_CANONICAL_NAMESPACE")
    if current_peak == current_prov:
        raise ValueError("COLLIDING_NAMESPACE_KEYS")
    if any(x is not None and (not isinstance(x, str) or not x)
           for x in (old_peak, old_prov)):
        raise ValueError("INVALID_LEGACY_MAPPING")
    return {
        "canonical": (current_peak, current_prov),
        "legacy": (old_peak, old_prov),
        "v1": (V1_PEAK, V1_PROV),
    }


def review_values(values, *, keys):
    """Return redacted, stable statuses; never expose key names or source rows.

    Deliberately does not recommend an executable migration if even one
    namespace is ambiguous, stale, malformed, or dual-populated.
    """
    blockers = []
    out = {}
    valid = {}
    present = {}
    for name in ("canonical", "legacy", "v1"):
        k_peak, k_prov = keys[name]
        if name == "legacy" and (not k_peak or not k_prov):
            blockers.append("LEGACY_KEY_PAIR_MAPPING_UNAVAILABLE")
            out[name] = "MAPPING_UNAVAILABLE"
            valid[name] = False
            present[name] = False
            continue
        raw_peak = values.get(k_peak)
        raw_prov = values.get(k_prov)
        peak_present = raw_peak is not None
        prov_present = raw_prov is not None
        present[name] = peak_present or prov_present
        if not present[name]:
            out[name] = "ABSENT"
            valid[name] = False
            continue
        if not peak_present or not prov_present:
            out[name] = "UNPAIRED"
            valid[name] = False
            blockers.append(name.upper() + "_UNPAIRED_HWM_PROVENANCE")
            continue
        peak = _positive(raw_peak)
        good = _provenance(raw_prov, peak)
        valid[name] = good
        if not good:
            out[name] = "INVALID_OR_DIVERGENT"
            blockers.append(name.upper() + "_INVALID_OR_DIVERGENT")
        else:
            out[name] = "PAIR_VALID"
    # A non-empty older v1 pair is independent evidence of another physical
    # version; never silently override current/legacy precedence.
    if present["v1"]:
        blockers.append("ANCIENT_V1_NAMESPACE_REVIEW_REQUIRED")
    if present["canonical"] and present["legacy"]:
        blockers.append("DUAL_CANONICAL_LEGACY_SCOPE_REVIEW_REQUIRED")
    if not present["canonical"] and not present["legacy"]:
        blockers.append("NO_HWM_PAIR_IN_CANONICAL_OR_LEGACY")
    if "UNCONFIGURED" in keys["canonical"][0]:
        blockers.append("ACTIVE_ACCOUNT_NAMESPACE_UNCONFIGURED")
    if keys["canonical"][0] in keys["legacy"] or keys["canonical"][1] in keys["legacy"]:
        blockers.append("PHYSICAL_SCOPE_MAPPING_COLLISION")
    if keys["legacy"][0] is not None and keys["legacy"][0] == keys["legacy"][1]:
        blockers.append("LEGACY_MAPPING_COLLISION")
    # Show only the validated numeric HWM from the physical source the
    # application would read. No raw key, old/new evidence_ref or identity.
    source = "NONE"
    peak_usdt = None
    if valid["canonical"]:
        source = "CANONICAL"
        peak_usdt = float(_positive(values.get(keys["canonical"][0])))
    elif not present["canonical"] and valid["legacy"]:
        source = "LEGACY"
        peak_usdt = float(_positive(values.get(keys["legacy"][0])))
    status = (
        "CONSISTENT_LEGACY_MANUAL_MIGRATION_REQUIRED"
        if not blockers and source == "LEGACY"
        else "CANONICAL_PAIR_PRESENT_REVIEW_ANCHOR_REQUIRED"
        if not blockers and source == "CANONICAL"
        else "MIGRATION_PREFLIGHT_BLOCKED"
    )
    return {
        "tag": TAG,
        "status": status,
        "scope_state": out,
        "effective_source": source,
        "effective_peak_usdt": (
            None if peak_usdt is None else round(peak_usdt, 8)
        ),
        "blockers": sorted(set(blockers)),
        "schema_installed": False,
        "anchor_created": False,
        "backup_available_verified": False,
        "historic_provenance_complete": False,
        "migration_authorized": False,
        "write_effect": "NONE",
        "risk_gate_effect": "NONE",
        "live_allowed": False,
        "decision_effect": "NONE",
        "read_only": True,
    }


async def collect_once(conn, *, keys):
    wanted = sorted({v for pair in keys.values() for v in pair if v})
    if not wanted or len(wanted) > 6:
        raise ValueError("UNSAFE_PHYSICAL_KEY_SCOPE")
    rows = await asyncio.wait_for(conn.fetch(SQL, wanted), timeout=1.0)
    values = {row["key"]: row["value"] for row in rows if row["key"] in wanted}
    return review_values(values, keys=keys)


async def _run():
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn.startswith(("postgres://", "postgresql://")):
        raise ValueError("POSTGRES_CONNECTION_UNAVAILABLE")
    keys = physical_keys()
    import asyncpg
    conn = await asyncpg.connect(dsn, timeout=5, server_settings=SETTINGS)
    try:
        return await collect_once(conn, keys=keys)
    finally:
        await conn.close(timeout=2)


def main():
    try:
        result = asyncio.run(_run())
    except Exception:
        # No raw error messages; they can expose credentials, account
        # fingerprints, private DSNs or key material.
        result = {
            "tag": TAG, "status": "UNAVAILABLE_OR_FAILED_CLOSED",
            "read_only": True, "migration_authorized": False,
            "live_allowed": False, "write_effect": "NONE",
        }
        print(json.dumps(result, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] != "MIGRATION_PREFLIGHT_BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
