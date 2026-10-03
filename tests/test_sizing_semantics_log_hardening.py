import logging

from bot.sizing_semantics_log_hardening import SizingSemanticsFilter, normalize_record


def _record(msg, args=()):
    return logging.LogRecord("test.child", logging.WARNING, __file__, 1, msg, args, None)


def test_legacy_target_is_relabelled_to_risk_budget_without_execution_claims():
    rec = _record(
        "[PILOT_LEGACY_TARGET] preliminary_only=true target_notional=%.4f leverage=%sx",
        (9.68, 50),
    )
    assert SizingSemanticsFilter().filter(rec) is True
    assert "authoritative_policy=risk_budget" in rec.msg
    assert "sizing_authority=RISK_BUDGET_V3" in rec.msg
    assert "margin_role=CEILING" in rec.msg
    assert "execution_effect=NONE" in rec.msg
    assert "50PCT" not in rec.msg and "50pct" not in rec.msg
    assert rec.args == ()


def test_legacy_50pct_authority_claims_are_corrected_in_place():
    for legacy in ("sizing_authority=RiskManagerV3_plus_operator_50pct_margin_cap",
                   "sizing_authority=OPERATOR_50PCT_EQUITY risk_manager_role=VALIDATION_GATE",
                   "sizing_authority=OPERATOR_50PCT_EQUITY"):
        rec = _record(f"[RUNTIME_CONTRACT] status=PASS {legacy} leverage_unchanged=true")
        normalize_record(rec)
        assert "sizing_authority=RISK_BUDGET_V3" in rec.msg
        assert "50pct" not in rec.msg and "50PCT" not in rec.msg
        assert "leverage_unchanged=true" in rec.msg


def test_truthful_risk_authority_wording_is_not_rewritten():
    msg = "[RUNTIME_OVERLAYS] final risk-authoritative sizing invariants active"
    rec = _record(msg)
    normalize_record(rec)
    assert rec.msg == msg


def test_unrelated_log_is_unchanged():
    msg = "[KUCOIN] private websocket connected"
    rec = _record(msg)
    normalize_record(rec)
    assert rec.msg == msg
