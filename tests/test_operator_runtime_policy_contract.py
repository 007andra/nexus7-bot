import inspect

from bot import operator_runtime_policy as policy


def test_operator_policy_has_no_margin_target():
    # F-003: sizing is the stop-loss risk budget; no percentage-of-available target.
    assert not hasattr(policy, "MARGIN_FRACTION")
    assert not hasattr(policy, "_install_margin_sizing")


def test_operator_policy_uses_configured_leverage_without_mutating_it():
    source = inspect.getsource(policy)
    assert "cfg.LEVERAGE =" not in source
    assert "sizing_authority=RISK_BUDGET_V3" in source


def test_drawdown_policy_is_fail_closed_by_default_with_explicit_override():
    source = inspect.getsource(policy._install_drawdown_advisory)
    protected = inspect.getsource(policy._protect_drawdown_update)
    assert "override=false entries_blocked=true" in source
    assert "override=true entries_blocked=false" in source
    assert "legacy_pause_preserved=true active_restored=false override=false" in protected
    assert "legacy_pause_neutralized=true active_restored=true override=true" in protected
