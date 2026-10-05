"""Regression guard for prospective OOS research timeout isolation."""
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "bot" / "hard_gate_shadow_scan.py").read_text(encoding="utf-8")


def test_oos_research_bundle_is_decoupled_from_upstream_research_reports():
    fallback = "if prospective_oos_report is not None and ("
    legacy_gate = (
        "if epoch_row is not None and validation_report is not None "
        "and review_report is not None:"
    )
    assert fallback in SOURCE
    assert legacy_gate in SOURCE
    assert SOURCE.index(fallback) < SOURCE.index(legacy_gate)
    segment = SOURCE[SOURCE.index(fallback):SOURCE.index(legacy_gate)]
    assert "PROSPECTIVE_OOS_RESEARCH_ISOLATION_V1" in segment
    assert "prospective_oos_enrollment_audit_v1.snapshot" in segment
    assert "segregated_pilot_ledger_v1.snapshot" in segment
    assert "prospective_oos_first_approval_review_v1.snapshot" in segment
    assert "prospective_oos_maturation_review_v1.snapshot" in segment


def test_timeout_isolation_cannot_grant_live_or_mutate_trading_authority():
    start = SOURCE.index("if prospective_oos_report is not None and (")
    end = SOURCE.index(
        "if epoch_row is not None and validation_report is not None "
        "and review_report is not None:"
    )
    segment = SOURCE[start:end]
    required = (
        '"research_only": True',
        '"shadow_only": True',
        '"thresholds_unchanged": True',
        '"risk_unchanged": True',
        '"sizing_unchanged": True',
        '"leverage_unchanged": True',
        '"historical_hwm_preserved": True',
        '"lifetime_drawdown_preserved": True',
        '"current_hard_gate_unchanged": True',
        '"automatic_promotion": False',
        '"promotion_allowed": False',
        '"live_allowed": False',
        '"decision_effect": "NONE"',
        '"execution_effect": "NONE"',
    )
    for marker in required:
        assert marker in segment
    forbidden = (
        ".place_order(",
        "submission_committed",
        "execute_order(",
        "dispatch_order(",
        "MAX_DRAWDOWN =",
        "LEVERAGE =",
    )
    for marker in forbidden:
        assert marker not in segment
