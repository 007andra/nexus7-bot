"""Offline-only validation of a frozen NEXUS-7 OOS PostgreSQL CSV snapshot.

Reads local CSV, writes aggregate JSON; never connects to a DB or exchange.
Outcomes are hypothetical gross price marks, NOT actual executed PnL.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import struct
from collections import Counter
from pathlib import Path

REQUIRED = {
    "cohort_id", "snapshot_as_of_epoch", "frozen_started_epoch", "candidate_id",
    "captured_epoch", "symbol", "side", "regime", "setup", "decision_state",
    "outcome_horizon_minutes", "outcome_state", "return_basis",
    "hypothetical_gross_return", "hypothetical_mfe", "hypothetical_mae",
    "observation_start_epoch", "outcome_identity_ok", "outcome_horizon_ok",
    "captured_epoch_real", "export_scope", "counterfactual_status",
    "execution_allowed_source", "outcome_payload_parse_ok",
}
COHORT = "CALIBRATION_GENERALIZATION_V1"
APPROVED = "COUNTERFACTUAL_APPROVED"
REJECTED = "COUNTERFACTUAL_REJECTED"
HORIZONS = (60, 240)


def number(value):
    try:
        if value is None or str(value).strip().upper() in ("", "NA", "NONE", "NULL"):
            return None
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError, OverflowError):
        return None


def stats(values):
    return {
        "n": len(values),
        "mean_gross_fraction": sum(values) / len(values) if values else None,
        "positive_fraction": sum(v > 0 for v in values) / len(values) if values else None,
    }


def validate(records):
    records = list(records)
    if not records:
        raise ValueError("EMPTY_EXPORT_OR_FROZEN_COHORT_NOT_FOUND")
    seen, ids = set(), {}
    snapshot, frozen, scope = None, None, None
    problems, missing = [], Counter()
    validated = {(g, h): [] for g in (APPROVED, REJECTED) for h in HORIZONS}
    observed_missing_basis = invalid_observed = 0
    for line, row in enumerate(records, 2):
        cid = (row.get("candidate_id") or "").strip()
        horizon = number(row.get("outcome_horizon_minutes"))
        decision = row.get("decision_state")
        if (row.get("cohort_id") != COHORT or not cid.startswith("HARD_GATE_SHADOW:")
                or horizon not in HORIZONS or int(horizon) != horizon):
            problems.append(f"row {line}: INVALID_COHORT_IDENTITY_OR_HORIZON")
            continue
        horizon = int(horizon)
        key = (cid, horizon)
        if key in seen:
            problems.append(f"row {line}: DUPLICATE_CANDIDATE_HORIZON")
        seen.add(key)
        now = number(row.get("snapshot_as_of_epoch"))
        started = number(row.get("frozen_started_epoch"))
        captured = number(row.get("captured_epoch"))
        stored_real = number(row.get("captured_epoch_real"))
        row_scope = row.get("export_scope")
        if row_scope not in ("JSON_PRECISE", "REAL_COARSE"):
            problems.append(f"row {line}: UNKNOWN_EXPORT_SCOPE")
        if scope is None:
            scope = row_scope
        elif scope != row_scope:
            problems.append(f"row {line}: MIXED_CUTOFF_SCOPES")
        if None in (now, started, captured, stored_real):
            problems.append(f"row {line}: MISSING_CAPTURE_OR_SNAPSHOT")
            continue
        if snapshot is None:
            snapshot, frozen = now, started
        if abs(now - snapshot) > 0.01 or abs(started - frozen) > 0.01:
            problems.append(f"row {line}: NON_ATOMIC_SNAPSHOT")
        # Strict maturation review checks precise JSON epoch. Old OOS snapshot
        # queries the PostgreSQL REAL column using a float32 cutoff.
        if row_scope == "JSON_PRECISE":
            in_scope = captured + 1e-9 >= started
        elif row_scope == "REAL_COARSE":
            try:
                started_float4 = struct.unpack("!f", struct.pack("!f", started))[0]
            except (OverflowError, struct.error):
                started_float4 = None
            in_scope = started_float4 is not None and stored_real >= started_float4
        else:
            in_scope = False
        if not in_scope or captured > now + 1e-6:
            problems.append(f"row {line}: CANDIDATE_OUTSIDE_SELECTED_CUTOFF_OR_SNAPSHOT")
        if decision not in (APPROVED, REJECTED):
            problems.append(f"row {line}: INVALID_COUNTERFACTUAL_DECISION")
        # Both production snapshots use (execution_allowed is True): missing,
        # null or false values mean rejected, not indeterminate.
        should_approve = str(row.get("execution_allowed_source")).lower() == "true"
        if decision != (APPROVED if should_approve else REJECTED):
            problems.append(f"row {line}: COUNTERFACTUAL_DECISION_MISMATCH")
        attributes = tuple(row.get(k) for k in (
            "symbol", "side", "regime", "setup", "captured_epoch", "decision_state"
        ))
        if cid in ids and ids[cid] != attributes:
            problems.append(f"row {line}: CONFLICTING_CANDIDATE_METADATA")
        else:
            ids[cid] = attributes
        if str(row.get("outcome_identity_ok")).lower() not in ("t", "true"):
            problems.append(f"row {line}: OUTCOME_CANDIDATE_ID_MISMATCH")
        if str(row.get("outcome_horizon_ok")).lower() not in ("t", "true"):
            problems.append(f"row {line}: OUTCOME_HORIZON_MISMATCH")
        if str(row.get("outcome_payload_parse_ok")).lower() not in ("t", "true"):
            problems.append(f"row {line}: MALFORMED_OUTCOME_PAYLOAD")
        state = row.get("outcome_state")
        if state != "OBSERVED":
            missing[(str(decision), horizon, state or "MISSING")] += 1
            continue
        observed_at = number(row.get("observation_start_epoch"))
        gain = number(row.get("hypothetical_gross_return"))
        mfe = number(row.get("hypothetical_mfe"))
        mae = number(row.get("hypothetical_mae"))
        # Old COHORT_V1 _outcome_map accepts observed finite return/MFE/MAE
        # (without maturity proof) and excludes status=ERROR from _matched.
        # MATURATION_REVIEW_V1 requires verified horizon maturity and permits
        # any _candidate with execution_allowed is not True as rejected.
        mature = (
            True if row_scope == "REAL_COARSE" else
            observed_at is not None
            and observed_at + horizon * 60 <= now + 1e-6
            and observed_at + 1e-6 >= math.ceil(captured / 900) * 900
        )
        if None in (gain, mfe, mae, observed_at) or not mature:
            invalid_observed += 1
            missing[(str(decision), horizon, "OBSERVED_INVALID_OR_NOT_MATURE")] += 1
            continue
        # The current research source may not explicitly persist return_basis.
        # Preserve the production-validated gross mark but flag this limitation.
        if row.get("return_basis") != "hypothetical_entry_gross":
            observed_missing_basis += 1
        if row_scope == "REAL_COARSE" and row.get("counterfactual_status") == "ERROR":
            missing[(str(decision), horizon, "LEGACY_EXCLUDES_STATUS_ERROR")] += 1
            continue
        if decision in (APPROVED, REJECTED):
            validated[(decision, horizon)].append(gain)
    for cid in ids:
        if (cid, 60) not in seen or (cid, 240) not in seen:
            problems.append("CANDIDATE_WITHOUT_BOTH_HORIZON_ROWS")
    decisions = Counter(entry[-1] for entry in ids.values())
    samples = {
        str(h): {group: stats(validated[(group, h)]) for group in (APPROVED, REJECTED)}
        for h in HORIZONS
    }
    for h in HORIZONS:
        approved, rejected = samples[str(h)][APPROVED], samples[str(h)][REJECTED]
        samples[str(h)]["descriptive_gross_lift_fraction"] = (
            approved["mean_gross_fraction"] - rejected["mean_gross_fraction"]
            if approved["mean_gross_fraction"] is not None
            and rejected["mean_gross_fraction"] is not None else None
        )
    return {
        "cohort_id": COHORT, "export_scope": scope,
        "reference_report": ("PROSPECTIVE_OOS_COHORT_V1" if scope == "REAL_COARSE"
                             else "PROSPECTIVE_OOS_MATURATION_REVIEW_V1"),
        "as_of_epoch": snapshot,
        "frozen_started_epoch": frozen, "unique_candidates": len(ids),
        "total_rows": len(records), "decision_counts": dict(sorted(decisions.items())),
        "outcomes_matching_production_validator": samples,
        "nonobserved_or_invalid_reasons": {
            "%s|%s|%s" % k: v for k, v in sorted(missing.items())
        },
        "observed_missing_or_different_return_basis": observed_missing_basis,
        "invalid_observed_outcome_rows": invalid_observed,
        "integrity_problems": problems[:50],
        "integrity_problem_count": len(problems),
        "evidence_scope": "HYPOTHETICAL_GROSS_ONLY; NOT REALIZED_NET; NOT LIVE AUTHORIZATION",
        "passed_schema_and_identity": not problems,
        "promotion_allowed": False, "live_allowed": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    raw = args.csv_file.read_bytes()
    with args.csv_file.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not REQUIRED.issubset(reader.fieldnames):
            parser.error("Expected CSV columns missing; incomplete evidence")
        report = validate(reader)
    report["input_file_sha256"] = hashlib.sha256(raw).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print("Candidates:", report["unique_candidates"],
          "Approved:", report["decision_counts"].get(APPROVED, 0),
          "Rejected:", report["decision_counts"].get(REJECTED, 0),
          "Integrity:", "PASS" if report["passed_schema_and_identity"] else "FAIL",
          "240m approved observed:",
          report["outcomes_matching_production_validator"]["240"][APPROVED]["n"])
    if not report["passed_schema_and_identity"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
