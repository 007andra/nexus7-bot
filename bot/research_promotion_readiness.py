"""Fail-closed research-readiness aggregation for NEXUS challengers.

This module does not promote strategies, mutate LIVE configuration, alter
thresholds, or authorize orders. It only checks whether the existing research
evidence is present, internally coherent and strong enough for an operator to
review a challenger.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping, Sequence


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GREEN_DRIFT = {"STABLE"}


@dataclass(frozen=True)
class ReadinessResult:
    ready_for_operator_review: bool
    blockers: tuple[str, ...]
    checks: Mapping[str, bool]
    execution_effect: str = "NONE"
    promotion_authority: bool = False

    def as_dict(self) -> dict:
        return {
            "ready_for_operator_review": self.ready_for_operator_review,
            "blockers": self.blockers,
            "checks": dict(self.checks),
            "execution_effect": self.execution_effect,
            "promotion_authority": self.promotion_authority,
        }


def _strict_true(value: object) -> bool:
    return value is True


def _manifest_valid(bundle: Mapping[str, object]) -> bool:
    digest = str(bundle.get("manifest_hash", "") or "").lower()
    dataset_digest = str(
        bundle.get("dataset_fingerprint", "") or ""
    ).lower()
    manifest = bundle.get("manifest")
    return bool(
        _SHA256_RE.fullmatch(digest)
        and _SHA256_RE.fullmatch(dataset_digest)
        and isinstance(manifest, Mapping)
        and manifest.get("artifacts")
    )


def _primary_green(bundle: Mapping[str, object]) -> bool:
    primary = bundle.get("primary")
    if not isinstance(primary, Mapping):
        return False
    blockers = primary.get("blockers") or ()
    return bool(
        primary.get("status") == "AI_EDGE_PROVEN"
        and not blockers
        and _strict_true(primary.get("context_parity_complete"))
    )


def _robustness_green(bundle: Mapping[str, object]) -> bool:
    root = bundle.get("robustness")
    if not isinstance(root, Mapping):
        return False
    robustness = root.get("robustness")
    if not isinstance(robustness, Mapping):
        return False
    summary = robustness.get("summary")
    if not isinstance(summary, Mapping):
        return False
    return bool(
        int(summary.get("temporal_folds_evaluated", 0) or 0) >= 4
        and _strict_true(summary.get("stable_positive_point_estimate"))
        and int(summary.get("leave_one_symbol_out_evaluated", 0) or 0) >= 2
    )


def _calibration_green(bundle: Mapping[str, object]) -> bool:
    calibration = bundle.get("calibration")
    if not isinstance(calibration, Mapping):
        return False
    methods = calibration.get("methods")
    if not isinstance(methods, Mapping):
        return False
    successful = [
        item for item in methods.values()
        if isinstance(item, Mapping) and item.get("status") == "OK"
    ]
    return bool(
        _strict_true(calibration.get("evidence_complete"))
        and int(calibration.get("fold_count", 0) or 0) >= 4
        and successful
        and calibration.get("fit_scope") == "TRAIN_ONLY"
        and calibration.get("evaluation_scope") == "OOS_ONLY"
        and calibration.get("purge_basis") == "ACTUAL_LABEL_END_TIMESTAMP"
        and int(calibration.get("embargo_rows", 0) or 0) >= 1
        and int(calibration.get("missing_label_end", 0) or 0) == 0
        and calibration.get("live_probability_effect") == "NONE"
    )


def _methodology_green(bundle: Mapping[str, object]) -> bool:
    methodology = bundle.get("methodology")
    if not isinstance(methodology, Mapping):
        return False
    required_true = (
        "archive_checksums_verified",
        "shared_candidate_population",
        "closed_candles_only",
        "historical_clock_frozen",
        "metrics_label_shift_normalized",
        "fees_included",
        "slippage_included",
        "funding_included",
    )
    if not all(_strict_true(methodology.get(name)) for name in required_true):
        return False
    return bool(
        methodology.get("venue") == "BINANCE_USDM"
        and methodology.get("source") == "data.binance.vision"
        and methodology.get("evidence_claim") == "NEXUS_SELECTION_EDGE"
        and methodology.get("execution_pnl_claim") is False
        and methodology.get("instrument_rule_parity")
        == "NOT_MODELED_BY_ALPHA_REPLAY"
        and methodology.get("oi_delta_semantics") == "PREVIOUS_NEXUS_CANDIDATE"
        and methodology.get("same_bar_ambiguity") == "STOP_FIRST"
        and methodology.get("authenticated_api") is False
        and methodology.get("exchange_mutations") is False
        and methodology.get("runtime_policy_mutations") is False
        and methodology.get("promotion_authority") is False
    )


def _universe_green(
    bundle: Mapping[str, object],
    required_symbols: Sequence[str],
) -> bool:
    methodology = bundle.get("methodology")
    if not isinstance(methodology, Mapping):
        return False
    required = {
        str(symbol).upper().strip()
        for symbol in required_symbols
        if str(symbol).strip()
    }
    panel_raw = methodology.get("symbol_universe")
    if not required or not isinstance(panel_raw, (list, tuple)):
        return False
    panel = sorted({
        str(symbol).upper().strip()
        for symbol in panel_raw
        if str(symbol).strip()
    })
    if not panel:
        return False
    raw = json.dumps(panel, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return bool(
        methodology.get("universe_selection") == "PREDECLARED_FIXED_PANEL"
        and methodology.get("claim_scope") == "SYMBOL_PANEL_ONLY"
        and methodology.get("symbol_universe_hash") == digest
        and required.issubset(set(panel))
    )


def _drift_green(drift: Mapping[str, object] | None) -> bool:
    if not isinstance(drift, Mapping):
        return False
    return bool(
        str(drift.get("status", "")) in _GREEN_DRIFT
        and int(drift.get("baseline_rows", 0) or 0) >= 10
        and int(drift.get("current_rows", 0) or 0) >= 10
        and drift.get("execution_effect") == "NONE"
    )


def _sensitivity_present(sensitivity: Mapping[str, object] | None) -> bool:
    """Require stress evidence without inventing a new optimization threshold."""
    if not isinstance(sensitivity, Mapping):
        return False
    parameter = sensitivity.get("parameter")
    execution_cost = sensitivity.get("execution_cost")
    if not isinstance(parameter, Mapping) or not isinstance(execution_cost, Mapping):
        return False
    return bool(
        int(parameter.get("parameter_sets", 0) or 0) >= 2
        and parameter.get("promotion_effect") == "NONE"
        and execution_cost.get("points")
        and execution_cost.get("promotion_effect") == "NONE"
    )


def evaluate_readiness(
    bundle: Mapping[str, object],
    *,
    ci_green: bool,
    shadow_drift: Mapping[str, object] | None,
    sensitivity: Mapping[str, object] | None,
    required_symbols: Sequence[str],
) -> ReadinessResult:
    """Aggregate research evidence into one operator-review readiness result.

    A true readiness result still grants no promotion. The separate
    champion/challenger registry continues to require explicit operator approval.
    """
    checks = {
        "ci_green": ci_green is True,
        "manifest_valid": _manifest_valid(bundle),
        "methodology_green": _methodology_green(bundle),
        "universe_green": _universe_green(bundle, required_symbols),
        "primary_edge_green": _primary_green(bundle),
        "robustness_green": _robustness_green(bundle),
        "calibration_green": _calibration_green(bundle),
        "shadow_drift_green": _drift_green(shadow_drift),
        "sensitivity_present": _sensitivity_present(sensitivity),
    }
    blockers = tuple(
        name.upper()
        for name, passed in checks.items()
        if not passed
    )
    return ReadinessResult(
        ready_for_operator_review=not blockers,
        blockers=blockers,
        checks=checks,
    )


def evidence_gate_flags(result: ReadinessResult) -> dict:
    """Map readiness to challenger evidence without setting operator approval."""
    return {
        "ci_green": bool(result.checks.get("ci_green")),
        "oos_green": bool(
            result.checks.get("manifest_valid")
            and result.checks.get("methodology_green")
            and result.checks.get("universe_green")
            and result.checks.get("primary_edge_green")
            and result.checks.get("robustness_green")
            and result.checks.get("calibration_green")
            and result.checks.get("sensitivity_present")
        ),
        "shadow_green": bool(result.checks.get("shadow_drift_green")),
        "operator_approved": False,
    }
