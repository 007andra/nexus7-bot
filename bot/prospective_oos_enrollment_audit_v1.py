"""PROSPECTIVE_OOS_ENROLLMENT_AUDIT_V1.

Read-only integrity audit for the immutable prospective OOS cohort.

This audit verifies:
- cohort metadata is frozen and non-resettable;
- enrolled candidates are after the immutable cutoff;
- table candidate_id matches payload candidate_id;
- no duplicate payload candidate ids are observed;
- every OOS outcome belongs to an enrolled candidate;
- outcome horizon matches its row key;
- observation_start is not before the candidate's next 15m boundary;
- observed outcomes are mature for their declared horizon;
- malformed rows are surfaced fail-closed.

No row is added, deleted, rewritten, promoted, or credited to LIVE/M2/M3.
"""
from __future__ import annotations

from collections import Counter
import json
import math
import time

from bot import prospective_oos_cohort_v1 as cohort

AUTHORITY = {
    "authority": "OBSERVABILITY_ONLY",
    "research_only": True,
    "read_only": True,
    "cohort_mutation_authorized": False,
    "candidate_generation_unchanged": True,
    "thresholds_unchanged": True,
    "risk_epoch_traversal_credit": False,
    "canonical_pipeline_credit": False,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _ceil_15m(epoch):
    return math.ceil(float(epoch) / 900.0) * 900.0


async def snapshot(db, *, now_epoch=None):
    now = float(time.time() if now_epoch is None else now_epoch)
    baseline = await cohort.ensure_cohort(db)
    start = float(baseline["started_epoch"])

    candidate_rows = await db._fetchall(
        "SELECT candidate_id,captured_epoch,payload "
        "FROM hard_gate_shadow_candidates_v1 "
        "WHERE population=? AND captured_epoch>=? ORDER BY captured_epoch,candidate_id",
        (cohort.POPULATION, start),
    )

    candidates = {}
    payload_ids = []
    malformed_candidates = 0
    id_mismatches = 0
    before_cutoff = 0
    authority_violations = 0
    missing_counterfactual = 0

    for row in candidate_rows or []:
        cid = str(row["candidate_id"] if hasattr(row, "keys") else row[0])
        captured = _finite(row["captured_epoch"] if hasattr(row, "keys") else row[1])
        raw = row["payload"] if hasattr(row, "keys") else row[2]
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            malformed_candidates += 1
            continue

        payload_cid = str(payload.get("candidate_id") or "")
        payload_ids.append(payload_cid)
        if payload_cid != cid:
            id_mismatches += 1
        if captured is None or captured < start:
            before_cutoff += 1
        if payload.get("population") != cohort.POPULATION:
            authority_violations += 1
        if payload.get("shadow_only") is not True or payload.get("live_eligible") is not False:
            authority_violations += 1
        if not isinstance(payload.get("counterfactual_nexus_v1"), dict):
            missing_counterfactual += 1
        candidates[cid] = {
            "captured_epoch": captured,
            "payload": payload,
        }

    payload_counts = Counter(x for x in payload_ids if x)
    duplicate_payload_ids = sum(1 for n in payload_counts.values() if n > 1)

    distributions = {}
    for dim in ("symbol", "side", "regime", "setup"):
        counts = Counter(
            str(item["payload"].get(dim) or "UNKNOWN")
            for item in candidates.values()
        )
        distributions[dim] = dict(sorted(counts.items()))
    allowed_n = sum(
        1 for item in candidates.values()
        if (item["payload"].get("counterfactual_nexus_v1") or {}).get(
            "execution_allowed"
        ) is True
    )
    rejected_n = len(candidates) - allowed_n

    outcome_rows = await db._fetchall(
        "SELECT o.candidate_id,o.horizon,o.payload,c.captured_epoch "
        "FROM hard_gate_shadow_outcomes_v1 o "
        "LEFT JOIN hard_gate_shadow_candidates_v1 c "
        "ON c.candidate_id=o.candidate_id "
        "WHERE o.population=? AND (c.captured_epoch>=? OR c.candidate_id IS NULL) "
        "ORDER BY o.candidate_id,o.horizon",
        (cohort.POPULATION, start),
    )

    malformed_outcomes = 0
    orphan_outcomes = 0
    horizon_mismatches = 0
    early_observation_start = 0
    immature_observed = 0
    invalid_observation_start = 0
    oos_outcomes = 0

    for row in outcome_rows or []:
        cid = str(row["candidate_id"] if hasattr(row, "keys") else row[0])
        horizon = int(row["horizon"] if hasattr(row, "keys") else row[1])
        raw = row["payload"] if hasattr(row, "keys") else row[2]
        candidate = candidates.get(cid)
        if candidate is None:
            orphan_outcomes += 1
            continue
        oos_outcomes += 1
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            malformed_outcomes += 1
            continue

        payload_horizon = payload.get("horizon")
        try:
            payload_horizon = int(payload_horizon)
        except (TypeError, ValueError):
            payload_horizon = None
        if payload_horizon != horizon:
            horizon_mismatches += 1

        captured = candidate["captured_epoch"]
        obs_start = _finite(payload.get("observation_start"))
        if captured is None or obs_start is None:
            invalid_observation_start += 1
            continue

        expected_start = _ceil_15m(captured)
        if obs_start + 1e-9 < expected_start:
            early_observation_start += 1

        if payload.get("outcome") == "OBSERVED":
            maturity = obs_start + horizon * 60.0
            if now + 1e-9 < maturity:
                immature_observed += 1

    metadata_ok = (
        baseline.get("cohort_id") == cohort.COHORT_ID
        and baseline.get("hypothesis_frozen") is True
        and baseline.get("reset_allowed") is False
        and _finite(baseline.get("discovery_cutoff_epoch")) == start
        and baseline.get("hypothesis") == cohort.FROZEN_HYPOTHESIS
    )

    violations = {
        "metadata": 0 if metadata_ok else 1,
        "malformed_candidates": malformed_candidates,
        "candidate_id_mismatches": id_mismatches,
        "duplicate_payload_ids": duplicate_payload_ids,
        "before_cutoff": before_cutoff,
        "authority_violations": authority_violations,
        "missing_counterfactual": missing_counterfactual,
        "malformed_outcomes": malformed_outcomes,
        "orphan_outcomes": orphan_outcomes,
        "horizon_mismatches": horizon_mismatches,
        "early_observation_start": early_observation_start,
        "immature_observed": immature_observed,
        "invalid_observation_start": invalid_observation_start,
    }
    total_violations = sum(violations.values())
    status = "PASS" if total_violations == 0 else "FAIL_CLOSED"

    captured_values = [
        c["captured_epoch"] for c in candidates.values()
        if c["captured_epoch"] is not None
    ]
    return {
        **AUTHORITY,
        "status": status,
        "cohort_id": baseline.get("cohort_id"),
        "started_epoch": start,
        "metadata_frozen": metadata_ok,
        "enrolled_candidates": len(candidates),
        "oos_outcomes": oos_outcomes,
        "first_capture_epoch": min(captured_values) if captured_values else None,
        "last_capture_epoch": max(captured_values) if captured_values else None,
        "allowed_candidates": allowed_n,
        "rejected_candidates": rejected_n,
        "distributions": distributions,
        "violations": violations,
        "total_violations": total_violations,
        "integrity_pass": total_violations == 0,
        "interpretation_guard": (
            "PASS_MEANS_COHORT_INTEGRITY_ONLY_NOT_EDGE_OR_LIVE_PERMISSION"
        ),
    }


def _fmt(value):
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "NA"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value).replace(" ", "_")


def format_log(row: dict) -> str:
    v = row.get("violations") or {}
    return (
        "[PROSPECTIVE_OOS_ENROLLMENT_AUDIT_V1] "
        f"status={_fmt(row.get('status'))} "
        f"cohort_id={_fmt(row.get('cohort_id'))} "
        f"enrolled={_fmt(row.get('enrolled_candidates'))} "
        f"oos_outcomes={_fmt(row.get('oos_outcomes'))} "
        f"allowed_candidates={_fmt(row.get('allowed_candidates'))} "
        f"rejected_candidates={_fmt(row.get('rejected_candidates'))} "
        f"metadata_frozen={_fmt(row.get('metadata_frozen'))} "
        f"total_violations={_fmt(row.get('total_violations'))} "
        f"candidate_id_mismatches={_fmt(v.get('candidate_id_mismatches'))} "
        f"duplicate_payload_ids={_fmt(v.get('duplicate_payload_ids'))} "
        f"before_cutoff={_fmt(v.get('before_cutoff'))} "
        f"authority_violations={_fmt(v.get('authority_violations'))} "
        f"missing_counterfactual={_fmt(v.get('missing_counterfactual'))} "
        f"horizon_mismatches={_fmt(v.get('horizon_mismatches'))} "
        f"early_observation_start={_fmt(v.get('early_observation_start'))} "
        f"immature_observed={_fmt(v.get('immature_observed'))} "
        f"integrity_pass={_fmt(row.get('integrity_pass'))} "
        "cohort_mutation_authorized=false candidate_generation_unchanged=true "
        "thresholds_unchanged=true promotion_allowed=false live_allowed=false "
        "decision_effect=NONE execution_effect=NONE"
    )


def format_distribution(row: dict) -> str:
    parts = []
    for dim in ("symbol", "side", "regime", "setup"):
        counts = (row.get("distributions") or {}).get(dim) or {}
        body = ",".join(f"{k}:{v}" for k, v in counts.items()) or "NONE"
        parts.append(f"{dim}={body}")
    return (
        "[PROSPECTIVE_OOS_ENROLLMENT_DISTRIBUTION_V1] "
        + " | ".join(parts)
        + " forced_diversity=false candidate_generation_unchanged=true "
        "promotion_allowed=false live_allowed=false decision_effect=NONE "
        "execution_effect=NONE"
    )
