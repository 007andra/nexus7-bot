"""V4: purely read-only preregistered SHADOW ablation, phase A.

This is a *membership and gross-outcome audit*, not an executable net backtest.
It must never promote the challenger. The only authorizing artifact is issue #602
(created 2026-10-08T22:16:36Z). No entry/exit/risk/SQL write effect.
"""
from __future__ import annotations

from collections import Counter
import json
import math
import os
import time

FLAG = "NEXUS_V4_PROSPECTIVE_ABLATION_SHADOW"
ISSUE_ID = 602
CUTOFF_EPOCH = 1791497796.0  # GitHub issue immutable created_at, UTC
COHORT_ID = "V4_SHORT_DOWN_MOMENTUM_ABLATION_20261008"
POPULATION = "HARD_GATE_SHADOW"
TARGET_APPROVED = 100
PER_SYMBOL_CAP = 15
MIN_SYMBOLS = 8
MIN_EXCLUDED = 20
MIN_RETAINED = 30
HORIZONS = (60, 240)
TARGET_SEGMENT = ("SHORT", "TRENDING_DOWN", "MOMENTUM")
# These are fixed labels only, never user/SQL text. Descriptive, not veto rules.
FUNNEL_SEGMENTS = ("SHORT_DOWN_MOMENTUM", "SHORT_DOWN_BOS_BREAK", "OTHER")
FRONTIER_CLASSES = (
    "PULLBACK", "FUNNEL", "CAPITAL", "MIN_ORDER", "NEXUS",
    "NEXUS_RR", "NEXUS_EV", "NEXUS_SCORE", "NEXUS_DATA",
    "NEXUS_OTHER", "SHADOW_APPROVED", "UNKNOWN",
)
AUTHORITY = {
    "research_only": True,
    "read_only_evaluation": True,
    "shadow_only": True,
    "prospective_only": True,
    "hypothesis_frozen": True,
    "sample_replacement_allowed": False,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "thresholds_unchanged": True,
    "risk_unchanged": True,
    "historical_hwm_preserved": True,
}


def enabled():
    # Default OFF: evaluation must not add DB reads to the scanner unless opted in.
    return os.environ.get(FLAG, "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _mean(values):
    values = tuple(values)
    return sum(values) / len(values) if values else None


def _funnel_segment(raw):
    """Fixed grouping for research diagnostics; cannot alter a decision."""
    side = str(raw.get("side") or "").upper()
    regime = str(raw.get("regime") or "").upper()
    setup = str(raw.get("setup") or "").upper()
    if side == "SHORT" and regime == "TRENDING_DOWN":
        if setup == "MOMENTUM":
            return "SHORT_DOWN_MOMENTUM"
        if setup == "BOS_BREAK":
            return "SHORT_DOWN_BOS_BREAK"
    return "OTHER"


def _frontier_class(raw):
    """Only code-owned names reach the log; unknown inputs are grouped."""
    value = raw.get("frontier_stage")
    return value if type(value) is str and value in FRONTIER_CLASSES else "UNKNOWN"


def _aggregate_frontier(counter):
    """Stable, allowlisted aggregate; no symbol, ID, free-text or raw SQL."""
    return ",".join(f"{key}:{counter[key]}" for key in FRONTIER_CLASSES
                    if counter.get(key, 0)) or "NONE"


def _aggregate_segment(counter):
    return ",".join(f"{key}:{counter[key]}" for key in FUNNEL_SEGMENTS
                    if counter.get(key, 0)) or "NONE"


def _candidate(raw):
    """Only canonical, future, immutable shadow decisions qualify.

    The counterfactual_nexus_v1 subrecord is intentionally never consulted.
    """
    if not isinstance(raw, dict):
        return None
    captured = _finite(raw.get("captured_epoch"))
    if captured is None or captured <= CUTOFF_EPOCH:
        return None
    if (raw.get("population") != POPULATION or
            raw.get("shadow_only") is not True or
            raw.get("live_eligible") is not False or
            raw.get("decision_effect") != "NONE" or
            raw.get("execution_effect") != "NONE" or
            raw.get("nexus_called") is not True or
            type(raw.get("nexus_allowed")) is not bool):
        return None
    cid = str(raw.get("candidate_id") or "")
    sym = str(raw.get("symbol") or "").upper()
    side = str(raw.get("side") or "").upper()
    setup = str(raw.get("setup") or "").upper()
    regime = str(raw.get("regime") or "").upper()
    parts = cid.split(":")
    if (not sym or not side or not setup or not regime or
            len(parts) != 5 or parts[:4] !=
            [POPULATION, sym, side, setup] or not parts[4] or
            raw.get("_db_candidate_id", cid) != cid):
        return None
    return {
        "candidate_id": cid,
        "captured_epoch": captured,
        "symbol": sym,
        "side": side,
        "regime": regime,
        "setup": setup,
        "champion_approved": raw["nexus_allowed"],
        # Diagnostic label only; never used in enrollment, eligibility or veto.
        "frontier_stage_diagnostic": _frontier_class(raw),
    }


def _outcome(raw, *, candidate, horizon, now_epoch):
    """Maturity/provenance guard for the existing GROSS path only.

    A valid gross bar observation is NOT a net executable protected exit.
    """
    if not isinstance(raw, dict) or raw.get("outcome") != "OBSERVED":
        return None
    if raw.get("candidate_id") != candidate["candidate_id"]:
        return None
    if type(raw.get("horizon")) is not int or raw["horizon"] != horizon:
        return None
    if raw.get("return_basis") != "hypothetical_entry_gross":
        return None
    start = _finite(raw.get("observation_start"))
    ret = _finite(raw.get("future_return"))
    mfe = _finite(raw.get("MFE"))
    mae = _finite(raw.get("MAE"))
    if None in (start, ret, mfe, mae):
        return None
    expected = math.ceil(candidate["captured_epoch"] / 900) * 900
    if (abs(start - expected) > 1e-4 or
            start + 60 * horizon > float(now_epoch)):
        return None
    return {"gross_return": ret, "MFE": mfe, "MAE": mae}


def evaluate(payloads, outcomes60=(), outcomes240=(), *, now_epoch):
    """Deterministic retrospective reconstruction of the *future-only* cohort.

    Does NOT persist a membership freeze; a future separate durable enrollment
    ledger is needed before claiming irreversible final evaluation. Fail closed.
    """
    candidates = []
    invalid_future = 0
    noncanonical_future = 0
    pre_nexus_segments, pre_nexus_frontier = Counter(), Counter()
    rejected_segments, rejected_frontier = Counter(), Counter()
    approved_segments = Counter()
    for raw in payloads:
        if not isinstance(raw, dict):
            continue
        captured = _finite(raw.get("captured_epoch"))
        if captured is None or captured <= CUTOFF_EPOCH:
            continue
        # Most shadow candidates never reach the canonical NEXUS evaluator.
        # This is normal, not malformed evidence or an audit failure.
        if raw.get("nexus_called") is not True:
            noncanonical_future += 1
            # Existing noncanonical count is record-level, not unique identities.
            # These counts remain descriptive and cannot create new approvals.
            if raw.get("population") == POPULATION and raw.get("shadow_only") is True:
                pre_nexus_segments[_funnel_segment(raw)] += 1
                pre_nexus_frontier[_frontier_class(raw)] += 1
            continue
        row = _candidate(raw)
        if row is None:
            invalid_future += 1
        else:
            candidates.append(row)

    candidates.sort(key=lambda x: (x["captured_epoch"], x["candidate_id"]))
    seen, approved, rejected = set(), [], 0
    counts = Counter()
    cap_skipped = 0
    duplicate_ids = 0
    for row in candidates:
        cid = row["candidate_id"]
        if cid in seen:
            duplicate_ids += 1
            continue
        seen.add(cid)
        if not row["champion_approved"]:
            rejected += 1
            rejected_segments[_funnel_segment(row)] += 1
            rejected_frontier[row["frontier_stage_diagnostic"]] += 1
            continue
        approved_segments[_funnel_segment(row)] += 1
        if counts[row["symbol"]] >= PER_SYMBOL_CAP:
            cap_skipped += 1
            continue
        if len(approved) < TARGET_APPROVED:
            approved.append(row)
            counts[row["symbol"]] += 1

    observations = {}
    for horizon, items in ((60, outcomes60), (240, outcomes240)):
        obs = {}
        dup = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("candidate_id") or "")
            if not cid:
                continue
            if cid in obs:
                dup += 1
            else:
                obs[cid] = item
        observations[horizon] = (obs, dup)

    excluded = [r for r in approved if
                (r["side"], r["regime"], r["setup"]) == TARGET_SEGMENT]
    retained = [r for r in approved if r not in excluded]
    pairs = {}
    for horizon in HORIZONS:
        obs, _ = observations[horizon]
        valid = []
        excluded_valid = []
        retained_valid = []
        for row in approved:
            result = _outcome(obs.get(row["candidate_id"]),
                              candidate=row, horizon=horizon,
                              now_epoch=now_epoch)
            if result is None:
                continue
            valid.append((row, result))
            if row in excluded:
                excluded_valid.append(result["gross_return"])
            else:
                retained_valid.append(result["gross_return"])
        pairs[horizon] = {
            "observed_gross": len(valid),
            "pending_or_unproven": len(approved) - len(valid),
            "excluded_observed_gross": len(excluded_valid),
            "retained_observed_gross": len(retained_valid),
            "excluded_avg_gross": _mean(excluded_valid),
            "retained_avg_gross": _mean(retained_valid),
        }

    blockers = []
    if invalid_future or duplicate_ids or any(p[1] for p in observations.values()):
        blockers.append("PROVENANCE_INTEGRITY")
    if len(approved) < TARGET_APPROVED:
        blockers.append("TARGET_APPROVED")
    if len(counts) < MIN_SYMBOLS:
        blockers.append("SYMBOL_DIVERSITY")
    if len(excluded) < MIN_EXCLUDED:
        blockers.append("MIN_EXCLUDED")
    if len(retained) < MIN_RETAINED:
        blockers.append("MIN_RETAINED")
    for horizon in HORIZONS:
        if pairs[horizon]["observed_gross"] < len(approved):
            blockers.append(f"OUTCOMES_{horizon}_NOT_PROVEN")
    # Phase A makes no net calculations, no stop-first assumptions, and no CI.
    blockers.append("EXECUTABLE_NET_PROOF_NOT_IMPLEMENTED")
    if "PROVENANCE_INTEGRITY" in blockers:
        status = "AUDIT_FAIL_CLOSED"
    elif "TARGET_APPROVED" in blockers:
        status = "COLLECTING_FUTURE_APPROVALS"
    elif any(b.startswith("OUTCOMES_") for b in blockers):
        status = "OUTCOMES_PENDING"
    elif any(b in blockers for b in ("SYMBOL_DIVERSITY", "MIN_EXCLUDED", "MIN_RETAINED")):
        status = "INSUFFICIENT_SAMPLE"
    else:
        status = "NET_EXECUTION_PROOF_REQUIRED"

    return {
        **AUTHORITY,
        "cohort_id": COHORT_ID,
        "issue_id": ISSUE_ID,
        "cutoff_epoch": CUTOFF_EPOCH,
        "status": status,
        "blockers": tuple(blockers),
        "future_canonical_candidates": len(candidates),
        "future_canonical_rejected": rejected,
        "eligible_approved": len(approved),
        "excluded_challenger_only": len(excluded),
        "retained_challenger": len(retained),
        "per_symbol_cap_skipped": cap_skipped,
        "invalid_future_records": invalid_future,
        "noncanonical_future_excluded": noncanonical_future,
        # Diagnosis of the 0/100 enrollment bottleneck; not market-policy stats.
        "pre_nexus_funnel_segments": dict(pre_nexus_segments),
        "pre_nexus_frontier": dict(pre_nexus_frontier),
        "canonical_rejected_funnel_segments": dict(rejected_segments),
        "canonical_rejected_frontier": dict(rejected_frontier),
        "canonical_approved_funnel_segments": dict(approved_segments),
        "duplicate_candidate_ids": duplicate_ids,
        "distinct_symbols": len(counts),
        "max_symbol_share": max(counts.values()) / len(approved) if approved else 0.0,
        "observed_60m": pairs[60]["observed_gross"],
        "observed_240m": pairs[240]["observed_gross"],
        "pending_60m": pairs[60]["pending_or_unproven"],
        "pending_240m": pairs[240]["pending_or_unproven"],
        "excluded_gross_avg_60m": pairs[60]["excluded_avg_gross"],
        "excluded_gross_avg_240m": pairs[240]["excluded_avg_gross"],
        "retained_gross_avg_60m": pairs[60]["retained_avg_gross"],
        "retained_gross_avg_240m": pairs[240]["retained_avg_gross"],
        "gross_only": True,
        "net_proven": False,
        "stop_tp_execution_proven": False,
        "durable_member_freeze_proven": False,
        "comparison_basis": "NOT_POLICY_EQUIVALENT_GROSS_RESEARCH_ONLY",
    }


def _selected_approved_ids(payloads):
    """Bound outcome SQL to the first 100 future canonical approvals only."""
    valid = sorted(
        (r for raw in payloads if (r := _candidate(raw)) is not None
         and r["champion_approved"]),
        key=lambda row: (row["captured_epoch"], row["candidate_id"]),
    )
    selected, seen, counts = [], set(), Counter()
    for row in valid:
        cid, symbol = row["candidate_id"], row["symbol"]
        if cid in seen:
            continue
        seen.add(cid)
        if counts[symbol] >= PER_SYMBOL_CAP:
            continue
        if len(selected) == TARGET_APPROVED:
            break
        selected.append(cid)
        counts[symbol] += 1
    return selected


async def snapshot(db, *, now_epoch=None):
    """Read-only SELECTs, no DDL, persisted enrollment or order actions."""
    # SQLite stores double precision; PostgreSQL REAL can round epoch seconds
    # by ~64s. Buffer query by 512s, then apply exact JSON cutoff in _candidate.
    rows = await db._fetchall(
        "SELECT candidate_id,payload FROM hard_gate_shadow_candidates_v1 "
        "WHERE population=? AND captured_epoch>=? "
        "ORDER BY captured_epoch,candidate_id",
        (POPULATION, CUTOFF_EPOCH - 512.0),
    )
    payloads = []
    for item in rows or ():
        try:
            cid = item["candidate_id"] if hasattr(item, "keys") else item[0]
            obj = json.loads(item["payload"] if hasattr(item, "keys") else item[1])
            if isinstance(obj, dict):
                obj["_db_candidate_id"] = cid
                payloads.append(obj)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    # Outcome query is limited to the *canonical post-cutoff* candidates and
    # does not access another research cohort. Always parameterized.
    ids = _selected_approved_ids(payloads)
    out60, out240 = [], []
    if ids:
        placeholders = ",".join("?" for _ in ids)
        outcome_rows = await db._fetchall(
            "SELECT candidate_id,horizon,payload FROM hard_gate_shadow_outcomes_v1 "
            "WHERE population=? AND horizon IN (60,240) "
            f"AND candidate_id IN ({placeholders})",
            (POPULATION, *ids),
        )
        for item in outcome_rows or ():
            try:
                cid = item["candidate_id"] if hasattr(item, "keys") else item[0]
                horizon = item["horizon"] if hasattr(item, "keys") else item[1]
                obj = json.loads(item["payload"] if hasattr(item, "keys") else item[2])
                if not isinstance(obj, dict) or type(horizon) is not int:
                    continue
                obj.setdefault("candidate_id", cid)
                (out60 if horizon == 60 else out240).append(obj)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    return evaluate(payloads, out60, out240,
                    now_epoch=time.time() if now_epoch is None else now_epoch)


def _fmt(value):
    return "NA" if value is None else f"{value:.6f}"


def format_log(report):
    return (
        "[V4_PROSPECTIVE_ABLATION_PHASE_A] "
        f"cohort_id={report['cohort_id']} cutoff_epoch={int(report['cutoff_epoch'])} "
        f"status={report['status']} approved={report['eligible_approved']}/{TARGET_APPROVED} "
        f"rejected_descriptive={report['future_canonical_rejected']} "
        f"excluded={report['excluded_challenger_only']} retained={report['retained_challenger']} "
        f"symbols={report['distinct_symbols']} concentration={_fmt(report['max_symbol_share'])} "
        f"cap_skipped={report['per_symbol_cap_skipped']} "
        f"noncanonical_excluded={report['noncanonical_future_excluded']} "
        f"funnel_pre={_aggregate_segment(report['pre_nexus_funnel_segments'])} "
        f"funnel_pre_stage={_aggregate_frontier(report['pre_nexus_frontier'])} "
        f"funnel_rejected={_aggregate_segment(report['canonical_rejected_funnel_segments'])} "
        f"funnel_rejected_stage={_aggregate_frontier(report['canonical_rejected_frontier'])} "
        f"funnel_approved={_aggregate_segment(report['canonical_approved_funnel_segments'])} "
        f"invalid={report['invalid_future_records']} "
        f"observed60={report['observed_60m']} observed240={report['observed_240m']} "
        f"excluded_gross60={_fmt(report['excluded_gross_avg_60m'])} "
        f"retained_gross60={_fmt(report['retained_gross_avg_60m'])} "
        f"excluded_gross240={_fmt(report['excluded_gross_avg_240m'])} "
        f"retained_gross240={_fmt(report['retained_gross_avg_240m'])} "
        f"blockers={','.join(report['blockers'])} gross_only=true net_proven=false "
        "durable_member_freeze_proven=false research_only=true "
        "promotion_allowed=false live_allowed=false "
        "decision_effect=NONE execution_effect=NONE"
    )
