import inspect

from bot import binance_margin_parity as parity


def test_avax_live_rejection_case_current_price_still_has_local_headroom():
    result = parity.audit_market_order_margin(
        available=6.0944,
        qty=25,
        reference_price=11.441,
        leverage=50,
        taker_fee_rate=0.0005,
        market_take_bound=None,
    )
    assert round(result.current_required, 6) == 5.863513
    assert result.current_headroom > 0.23
    assert 0.039 < result.exhaustion_move_fraction < 0.040


def test_avax_case_four_percent_price_scenario_exhausts_collateral():
    result = parity.audit_market_order_margin(
        available=6.0944,
        qty=25,
        reference_price=11.441,
        leverage=50,
        taker_fee_rate=0.0005,
        market_take_bound=0.04,
    )
    assert result.bound_required > result.available
    assert result.bound_headroom < 0


def test_five_percent_market_bound_scenario_explains_minus_2019_mechanically():
    result = parity.audit_market_order_margin(
        available=6.0944,
        qty=25,
        reference_price=11.441,
        leverage=50,
        taker_fee_rate=0.0005,
        market_take_bound=0.05,
    )
    assert round(result.bound_required, 6) == 6.156688
    assert result.bound_headroom < -0.06


def test_diagnostic_module_has_no_execution_side_effects():
    source = inspect.getsource(parity)
    for forbidden in (
        "place_order(",
        "set_leverage(",
        "cancel_order(",
        "LIVE_OPERATOR_MARGIN_FRACTION",
        "MAX_RISK_PCT",
    ):
        assert forbidden not in source
