"""Export the SAME frozen prospective OOS cohort, under PostgreSQL READ ONLY.

Run from repository root using python -m research.oos_rca_v1.pg_matched_cohort_export.
This tool does not import the trading engine or initialize/alter database schema.
The output contains no credentials, balance/HWM data, or LIVE authority.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from bot import prospective_oos_cohort_v1 as cohort
from bot import prospective_oos_maturation_review_v1 as review

FIELDS = (
    "candidate_id", "symbol", "side", "regime", "setup", "captured_epoch",
    "cohort_decision", "horizon", "verified", "outcome_state",
    "missing_reason", "future_return_gross", "MFE", "MAE",
    "observation_start", "return_basis",
)


def _obj(raw):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


def _finite(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def build_export(metadata, candidates, outcomes, *, as_of):
    """Exact payload cutoff/identity, no outcome imputation or retroactive replacement."""
    metadata = _obj(metadata)
    if (not metadata or metadata.get("cohort_id") != cohort.COHORT_ID
            or metadata.get("hypothesis_frozen") is not True
            or metadata.get("reset_allowed") is not False):
        raise ValueError("COHORT_METADATA_INVALID")
    started = _finite(metadata.get("started_epoch"))
    if started is None or started != _finite(metadata.get("discovery_cutoff_epoch")):
        raise ValueError("FROZEN_CUTOFF_MISMATCH")
    result = {}
    exclusions = Counter()
    for raw in candidates:
        data = _obj(raw["payload"])
        if data is None:
            exclusions["MALFORMED_CANDIDATE"] += 1
            continue
        data["_review_table_candidate_id"] = raw["candidate_id"]
        item, reason = review._candidate(data, started_epoch=started)
        if item is None:
            exclusions[reason] += 1
            continue
        if item["captured_epoch"] > as_of:
            exclusions["FUTURE_CANDIDATE"] += 1
            continue
        if item["candidate_id"] in result:
            raise ValueError("DUPLICATE_CANDIDATE_ID")
        cf = data["counterfactual_nexus_v1"]
        item["cohort_decision"] = (
            "INDETERMINATE_ERROR" if cf.get("status") == "ERROR" else
            "COUNTERFACTUAL_APPROVED" if cf.get("execution_allowed") is True else
            "COUNTERFACTUAL_REJECTED" if cf.get("execution_allowed") is False else
            "INDETERMINATE"
        )
        result[item["candidate_id"]] = item
    by_horizon = {}
    orphaned = 0
    for raw in outcomes:
        cid, horizon = str(raw["candidate_id"]), raw["horizon"]
        if cid not in result or horizon not in (60, 240):
            orphaned += 1
            continue
        key = (cid, horizon)
        if key in by_horizon:
            raise ValueError("DUPLICATE_OUTCOME")
        by_horizon[key] = raw["payload"]
    rows = []
    for cid, item in sorted(result.items(), key=lambda pair: (pair[1]["captured_epoch"], pair[0])):
        for horizon in (60, 240):
            raw = _obj(by_horizon.get((cid, horizon)))
            if raw is None:
                state, missing, proof = "MISSING", "NO_VALID_OUTCOME_ROW", None
            else:
                state = str(raw.get("outcome") or "UNKNOWN")
                proof, error = review._observed_outcome(
                    raw, candidate_id=cid, horizon=horizon,
                    captured_epoch=item["captured_epoch"], now_epoch=as_of)
                if proof is not None and raw.get("return_basis") != "hypothetical_entry_gross":
                    proof, error = None, "RETURN_BASIS_NOT_GROSS"
                missing = (None if proof is not None else error or state)
            captured_start = math.ceil(item["captured_epoch"] / 900) * 900
            if raw is None and as_of < captured_start + horizon * 60:
                missing = "NOT_MATURED"
            rows.append({
                "candidate_id": cid, "symbol": item["symbol"],
                "side": item["side"], "regime": item["regime"],
                "setup": item["setup"], "captured_epoch": item["captured_epoch"],
                "cohort_decision": item["cohort_decision"], "horizon": horizon,
                "verified": proof is not None, "outcome_state": state,
                "missing_reason": missing,
                "future_return_gross": proof["future_return"] if proof else None,
                "MFE": proof["MFE"] if proof else None,
                "MAE": proof["MAE"] if proof else None,
                "observation_start": proof["observation_start"] if proof else None,
                "return_basis": "hypothetical_entry_gross" if proof else None,
            })
    summary = {
        "cohort_id": cohort.COHORT_ID,
        "as_of_epoch": as_of, "cohort_cutoff_epoch": started,
        "included_candidates": len(result), "excluded": dict(exclusions),
        "orphan_outcome_rows": orphaned, "groups": {},
        "statistical_authority": "DESCRIPTIVE_RESEARCH_ONLY",
        "promotion_allowed": False, "live_allowed": False,
        "comparison_basis": "SAME_FROZEN_PROSPECTIVE_OOS_COHORT",
        "return_basis": "HYPOTHETICAL_GROSS_NOT_EXECUTED_OR_COST_ADJUSTED",
    }
    for horizon in (60, 240):
        group = {}
        for decision in ("COUNTERFACTUAL_APPROVED", "COUNTERFACTUAL_REJECTED",
                         "INDETERMINATE", "INDETERMINATE_ERROR"):
            members = [r for r in rows if r["horizon"] == horizon and r["cohort_decision"] == decision]
            values = [r["future_return_gross"] for r in members if r["verified"]]
            group[decision] = {
                "enrolled": len(members), "verified": len(values),
                "avg_return_gross": sum(values) / len(values) if values else None,
                "positive_rate": sum(x > 0 for x in values) / len(values) if values else None,
                "missing": dict(Counter(r["missing_reason"] for r in members if r["missing_reason"])),
            }
        summary["groups"][str(horizon)] = group
    digest = json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False)
    summary["rows_sha256"] = hashlib.sha256(digest.encode()).hexdigest()
    return rows, summary


async def export_readonly(dsn, as_of):
    """Server-enforced repeatable-read, READ ONLY transaction; SELECT statements only."""
    import asyncpg
    if not dsn or not dsn.startswith(("postgres://", "postgresql://")):
        raise ValueError("PostgreSQL DSN missing/invalid")
    conn = await asyncpg.connect(dsn, command_timeout=20, statement_cache_size=0)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            meta = await conn.fetchrow(
                "SELECT payload FROM prospective_oos_cohort_v1 WHERE cohort_id=$1",
                cohort.COHORT_ID)
            if meta is None:
                raise ValueError("FROZEN_COHORT_NOT_FOUND")
            # Read all population rows; exact cutoff is stored in JSON (DB REAL may round).
            candidates = await conn.fetch(
                "SELECT candidate_id,payload FROM hard_gate_shadow_candidates_v1 "
                "WHERE population=$1 ORDER BY captured_epoch,candidate_id LIMIT 20001",
                cohort.POPULATION)
            if len(candidates) > 20000:
                raise ValueError("CANDIDATES_TRUNCATED_FAIL_CLOSED")
            outcomes = await conn.fetch(
                "SELECT o.candidate_id,o.horizon,o.payload "
                "FROM hard_gate_shadow_outcomes_v1 o "
                "JOIN hard_gate_shadow_candidates_v1 c ON c.candidate_id=o.candidate_id "
                "WHERE o.population=$1 AND c.population=$1 AND o.horizon IN (60,240) "
                "ORDER BY o.candidate_id,o.horizon LIMIT 40001",
                cohort.POPULATION)
            if len(outcomes) > 40000:
                raise ValueError("OUTCOMES_TRUNCATED_FAIL_CLOSED")
            return build_export(meta["payload"], candidates, outcomes, as_of=as_of)
    finally:
        await conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--as-of", required=True, help="Frozen UTC ISO timestamp, e.g. 2026-10-08T17:00:00Z")
    ap.add_argument("--out-prefix", type=Path, required=True)
    args = ap.parse_args()
    try:
        when = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError("UTC offset required")
    except ValueError as exc:
        ap.error(str(exc))
    rows, stats = asyncio.run(export_readonly(os.environ.get("DATABASE_URL", ""), when.timestamp()))
    args.out_prefix.parent.mkdir(parents=True, exist_ok=True)
    with args.out_prefix.with_suffix(".csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    args.out_prefix.with_suffix(".json").write_text(
        json.dumps(stats, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print("Research-only evidence export, candidates:", stats["included_candidates"],
          "rows:", len(rows), "SHA256:", stats["rows_sha256"])


if __name__ == "__main__":
    main()
