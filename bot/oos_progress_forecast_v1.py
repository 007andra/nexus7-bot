"""OOS_PROGRESS_FORECAST_V1.

Non-authoritative collection-pace forecast for the immutable prospective OOS
cohort. Estimates are derived only from observed enrollment/outcome cadence and
are never used to alter candidate generation, scan frequency, thresholds,
risk, promotion, or LIVE authority.

If evidence is too sparse, the forecast deliberately returns ETA_UNAVAILABLE.
"""
from __future__ import annotations

import math
import time

AUTHORITY = {
    "authority": "OBSERVABILITY_ONLY",
    "research_only": True,
    "forecast_only": True,
    "non_authoritative": True,
    "collection_acceleration_authorized": False,
    "candidate_generation_unchanged": True,
    "scan_frequency_unchanged": True,
    "thresholds_unchanged": True,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
}

TARGET_CANDIDATES = 50
TARGET_60M = 30
TARGET_240M = 30
MIN_CANDIDATES_FOR_RATE = 3
MIN_RATE_SPAN_S = 900.0
MIN_OUTCOMES_FOR_RATE = 3


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _eta_hours(remaining, rate_per_hour):
    if remaining <= 0:
        return 0.0
    if rate_per_hour is None or rate_per_hour <= 0.0:
        return None
    return float(remaining) / float(rate_per_hour)


def evaluate(oos: dict, audit: dict, *, now_epoch=None) -> dict:
    now = float(time.time() if now_epoch is None else now_epoch)
    enrolled = _int(oos.get("enrolled_candidates"))
    observed60 = _int(oos.get("observed_60m"))
    observed240 = _int(oos.get("observed_240m"))
    start = _finite(oos.get("started_epoch"))
    first = _finite(audit.get("first_capture_epoch"))
    last = _finite(audit.get("last_capture_epoch"))

    candidate_rate = None
    candidate_span = None
    if (
        enrolled >= MIN_CANDIDATES_FOR_RATE
        and first is not None and last is not None and last >= first
    ):
        candidate_span = last - first
        if candidate_span >= MIN_RATE_SPAN_S:
            # n-1 inter-arrival intervals across first..last.
            candidate_rate = (enrolled - 1) * 3600.0 / candidate_span

    cohort_elapsed = None if start is None else max(0.0, now - start)

    rate60 = None
    effective60 = None
    if cohort_elapsed is not None:
        effective60 = max(0.0, cohort_elapsed - 3600.0)
        if observed60 >= MIN_OUTCOMES_FOR_RATE and effective60 >= MIN_RATE_SPAN_S:
            rate60 = observed60 * 3600.0 / effective60

    rate240 = None
    effective240 = None
    if cohort_elapsed is not None:
        effective240 = max(0.0, cohort_elapsed - 14400.0)
        if observed240 >= MIN_OUTCOMES_FOR_RATE and effective240 >= MIN_RATE_SPAN_S:
            rate240 = observed240 * 3600.0 / effective240

    rem_candidates = max(0, TARGET_CANDIDATES - enrolled)
    rem60 = max(0, TARGET_60M - observed60)
    rem240 = max(0, TARGET_240M - observed240)

    eta_candidates = _eta_hours(rem_candidates, candidate_rate)
    eta60 = _eta_hours(rem60, rate60)
    eta240 = _eta_hours(rem240, rate240)

    individual_etas = (eta_candidates, eta60, eta240)
    complete_eta = (
        max(individual_etas)
        if all(value is not None for value in individual_etas)
        else None
    )

    reasons = []
    if candidate_rate is None and rem_candidates > 0:
        reasons.append("CANDIDATE_RATE_INSUFFICIENT")
    if rate60 is None and rem60 > 0:
        reasons.append("OUTCOME_60M_RATE_INSUFFICIENT")
    if rate240 is None and rem240 > 0:
        reasons.append("OUTCOME_240M_RATE_INSUFFICIENT")
    if audit.get("integrity_pass") is not True:
        reasons.append("COHORT_INTEGRITY_NOT_PASS")

    if complete_eta is not None and audit.get("integrity_pass") is True:
        status = "PACE_ESTIMATE_AVAILABLE"
    else:
        status = "ETA_UNAVAILABLE"

    return {
        **AUTHORITY,
        "status": status,
        "cohort_id": oos.get("cohort_id"),
        "started_epoch": start,
        "now_epoch": now,
        "cohort_elapsed_hours": (
            cohort_elapsed / 3600.0 if cohort_elapsed is not None else None
        ),
        "enrolled_candidates": enrolled,
        "observed_60m": observed60,
        "observed_240m": observed240,
        "candidate_rate_per_hour": candidate_rate,
        "outcome_60m_rate_per_hour": rate60,
        "outcome_240m_rate_per_hour": rate240,
        "candidate_rate_span_hours": (
            candidate_span / 3600.0 if candidate_span is not None else None
        ),
        "effective_60m_window_hours": (
            effective60 / 3600.0 if effective60 is not None else None
        ),
        "effective_240m_window_hours": (
            effective240 / 3600.0 if effective240 is not None else None
        ),
        "remaining_candidates": rem_candidates,
        "remaining_60m": rem60,
        "remaining_240m": rem240,
        "eta_candidates_hours": eta_candidates,
        "eta_60m_hours": eta60,
        "eta_240m_hours": eta240,
        "eta_sample_complete_hours": complete_eta,
        "eta_unavailable_reasons": tuple(reasons),
        "integrity_pass": bool(audit.get("integrity_pass")),
        "interpretation_guard": (
            "FORECAST_IS_DESCRIPTIVE_ONLY_DO_NOT_ACCELERATE_OR_CHANGE_SELECTION"
        ),
    }


def _fmt(value):
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "NA"
    if isinstance(value, (tuple, list)):
        return ",".join(map(str, value)) or "NONE"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value).replace(" ", "_")


def format_log(row: dict) -> str:
    keys = (
        "status", "cohort_id", "cohort_elapsed_hours",
        "enrolled_candidates", "observed_60m", "observed_240m",
        "candidate_rate_per_hour", "outcome_60m_rate_per_hour",
        "outcome_240m_rate_per_hour", "remaining_candidates",
        "remaining_60m", "remaining_240m", "eta_candidates_hours",
        "eta_60m_hours", "eta_240m_hours", "eta_sample_complete_hours",
        "eta_unavailable_reasons", "integrity_pass",
        "collection_acceleration_authorized", "candidate_generation_unchanged",
        "scan_frequency_unchanged", "thresholds_unchanged",
        "promotion_allowed", "live_allowed", "decision_effect", "execution_effect",
    )
    return "[OOS_PROGRESS_FORECAST_V1] " + " ".join(
        f"{key}={_fmt(row.get(key))}" for key in keys
    )
