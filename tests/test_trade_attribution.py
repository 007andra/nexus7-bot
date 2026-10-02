import pytest

from bot.trade_attribution import TradeAttributionInput, attribute_trade


def test_long_attribution_reconciles_to_net():
    result = attribute_trade(TradeAttributionInput(
        side="LONG", qty=2, decision_entry=100, actual_entry=101,
        decision_exit=110, actual_exit=109, fees=1.0, funding=-0.5,
        regime="TREND", exit_reason="TP",
    ))
    assert result["market_pnl_at_decision_prices"] == 20
    assert result["entry_execution_effect"] == -2
    assert result["exit_execution_effect"] == -2
    assert result["gross_pnl"] == 16
    assert result["net_pnl"] == 14.5
    assert result["reconciles"]


def test_short_attribution_handles_better_entry_and_exit():
    result = attribute_trade(TradeAttributionInput(
        side="SHORT", qty=1, decision_entry=100, actual_entry=101,
        decision_exit=90, actual_exit=89, fees=0.5,
    ))
    assert result["entry_execution_effect"] > 0
    assert result["exit_execution_effect"] > 0
    assert result["net_pnl"] > result["market_pnl_at_decision_prices"]


def test_invalid_side_rejected():
    with pytest.raises(ValueError):
        TradeAttributionInput("FLAT", 1, 1, 1, 1, 1)
