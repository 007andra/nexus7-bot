import inspect

from bot import operator_runtime_policy as policy


def test_operator_margin_fraction_remains_fifty_percent():
    assert policy.MARGIN_FRACTION == 0.50


def test_operator_policy_uses_configured_leverage_without_mutating_it():
    source = inspect.getsource(policy)
    assert "target_margin = operator_target_margin(available, leverage, margin_fraction)" in source
    assert "target_notional = target_margin * leverage" in source
    assert "leverage = float(cfg.LEVERAGE)" in source
    assert "cfg.LEVERAGE =" not in source


def test_drawdown_policy_is_fail_closed_by_default_with_explicit_override():
    source = inspect.getsource(policy._install_drawdown_advisory)
    protected = inspect.getsource(policy._protect_drawdown_update)
    assert "override=false entries_blocked=true" in source
    assert "override=true entries_blocked=false" in source
    assert "legacy_pause_preserved=true active_restored=false override=false" in protected
    assert "legacy_pause_neutralized=true active_restored=true override=true" in protected


def test_margin_policy_returns_operator_target_quantity_not_stop_risk_telemetry():
    source = inspect.getsource(policy._install_margin_sizing)
    assert "stop_risk_qty_advisory" in source
    assert "authority=operator_margin_policy" in source
    assert "return target_qty" in source
