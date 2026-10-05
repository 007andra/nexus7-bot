"""Regression guards for persisted prospective OOS maturation after gate clear."""
from pathlib import Path

from bot import hard_gate_shadow_scan as shadow

SOURCE = (
    Path(__file__).resolve().parents[1] / "bot" / "hard_gate_shadow_scan.py"
).read_text(encoding="utf-8")


def _eligible_row(**overrides):
    row = {
        "candidate_id": "HARD_GATE_SHADOW:FILUSDT:LONG:BOS_BREAK:1",
        "captured_epoch": 2000.0,
        "population": "HARD_GATE_SHADOW",
        "shadow_only": True,
        "live_eligible": False,
        "counterfactual_nexus_v1": {
            "candidate_id": "HARD_GATE_SHADOW:FILUSDT:LONG:BOS_BREAK:1",
            "cohort": "MIN_ORDER_BLOCKED_COUNTERFACTUAL_NEXUS",
            "risk_epoch_traversal_credit": False,
        },
    }
    row.update(overrides)
    return row


def test_gate_clear_path_runs_existing_oos_maturation_instead_of_candidate_scan():
    start = SOURCE.index('if not initial["live_entries_blocked"]:')
    end = SOURCE.index("if engine in _RUNNING:", start)
    segment = SOURCE[start:end]
    assert "_emit_existing_prospective_oos_maturation" in segment
    assert "PROSPECTIVE_OOS_GATE_CLEAR_MATURATION_V1" in segment
    assert '"live_allowed": False' in segment
    assert '"decision_effect": "NONE"' in segment
    assert '"execution_effect": "NONE"' in segment


def test_gate_clear_maturation_is_exact_existing_cohort_only():
    assert shadow._prospective_oos_candidate_for_maturation(
        _eligible_row(), started_epoch=1500.0
    )
    assert not shadow._prospective_oos_candidate_for_maturation(
        _eligible_row(captured_epoch=1000.0), started_epoch=1500.0
    )
    assert not shadow._prospective_oos_candidate_for_maturation(
        _eligible_row(live_eligible=True), started_epoch=1500.0
    )
    wrong = _eligible_row()
    wrong["counterfactual_nexus_v1"] = {
        **wrong["counterfactual_nexus_v1"],
        "cohort": "OTHER",
    }
    assert not shadow._prospective_oos_candidate_for_maturation(
        wrong, started_epoch=1500.0
    )
    credited = _eligible_row()
    credited["counterfactual_nexus_v1"] = {
        **credited["counterfactual_nexus_v1"],
        "risk_epoch_traversal_credit": True,
    }
    assert not shadow._prospective_oos_candidate_for_maturation(
        credited, started_epoch=1500.0
    )


def test_gate_clear_path_never_generates_candidates_or_reaches_live_execution():
    start = SOURCE.index("async def _emit_existing_prospective_oos_maturation")
    end = SOURCE.index("async def scan_if_enabled", start)
    segment = SOURCE[start:end]
    assert "observe_existing_prospective_oos_outcomes" in segment
    assert "existing_candidates_only" in segment
    assert "candidate_generation_unchanged" in segment
    assert "outcome.get(\"outcome\") != \"OBSERVED\"" in SOURCE
    forbidden = (
        "persist_candidate(",
        ".place_order(",
        "submission_committed",
        "execute_order(",
        "dispatch_order(",
        "MAX_DRAWDOWN =",
        "LEVERAGE =",
    )
    for marker in forbidden:
        assert marker not in segment
