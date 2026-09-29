import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import binance_accounting_evidence as accounting
from bot import drawdown_persistence as ddp
from bot.professional_risk import CapitalState
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.risk import RiskManager


OPEN_ORDER_ID = "40869448353"
CLOSE_ORDER_ID = "50000000001"


def _entry_registry():
    return {
        "order_id": OPEN_ORDER_ID,
        "client_oid": "bgx7-043d8306e3231d420e0a5b46248570",
        "symbol": "AVAXUSDT",
        "side": "Buy",
        "state": "FILLED",
        "reduce_only": False,
        "exposure_intent": "INCREASE",
        "previous_position_qty": 0.0,
    }


def _lineage():
    return {
        "version": 2,
        "symbol": "AVAXUSDT",
        "direction": "LONG",
        "order_id": OPEN_ORDER_ID,
        "client_oid": "bgx7-043d8306e3231d420e0a5b46248570",
        "order_created_at_ms": 1790713642000,
        "captured_at_ms": 1790713645000,
    }


def _trades():
    return [
        {
            "symbol": "AVAXUSDT", "id": 1001, "orderId": OPEN_ORDER_ID,
            "side": "BUY", "price": "11.433", "qty": "25",
            "realizedPnl": "0", "commission": "0.1429125",
            "commissionAsset": "USDT", "time": 1790713644000,
            "positionSide": "BOTH",
        },
        {
            "symbol": "AVAXUSDT", "id": 1002, "orderId": CLOSE_ORDER_ID,
            "side": "SELL", "price": "11.515", "qty": "25",
            "realizedPnl": "2.05", "commission": "0.1439375",
            "commissionAsset": "USDT", "time": 1790716710000,
            "positionSide": "BOTH",
        },
    ]


def _orders():
    return [
        {
            "symbol": "AVAXUSDT", "orderId": OPEN_ORDER_ID,
            "clientOrderId": "bgx7-043d8306e3231d420e0a5b46248570",
            "side": "BUY", "positionSide": "BOTH", "status": "FILLED",
        },
        {
            "symbol": "AVAXUSDT", "orderId": CLOSE_ORDER_ID,
            "clientOrderId": "generated-close-id",
            "side": "SELL", "positionSide": "BOTH", "status": "FILLED",
            "reduceOnly": True,
        },
    ]


def test_close_fill_exists_but_missing_algo_actual_order_link_keeps_cycle_unattributed():
    algo = [{
        "symbol": "AVAXUSDT", "algoId": "9001",
        "clientAlgoId": "bgx7-protection", "side": "SELL",
        "positionSide": "BOTH", "actualOrderId": "",
        "algoStatus": "EXPIRED", "closePosition": True,
    }]
    cycles = accounting.reconstruct_bgx_lifecycles(
        _trades(), _orders(), algo, [_entry_registry()],
        {OPEN_ORDER_ID: _lineage()},
    )
    assert cycles == []


def test_same_close_becomes_attributable_when_actual_order_id_is_present():
    algo = [{
        "symbol": "AVAXUSDT", "algoId": "9001",
        "clientAlgoId": "bgx7-protection", "side": "SELL",
        "positionSide": "BOTH", "actualOrderId": CLOSE_ORDER_ID,
        "algoStatus": "FINISHED", "closePosition": True,
    }]
    cycles = accounting.reconstruct_bgx_lifecycles(
        _trades(), [_orders()[0]], algo, [_entry_registry()],
        {OPEN_ORDER_ID: _lineage()},
    )
    assert len(cycles) == 1
    assert cycles[0]["receipt"]["close_identity"] == ["BGX_ALGO_CLOSE_ORDER"]
    assert cycles[0]["row"]["closePrice"] == "11.515"


def test_captured_hwm_to_flat_equity_is_15_83_percent_drawdown():
    peak = 8.8015
    equity = 7.4078
    assert round((peak - equity) / peak * 100, 2) == 15.83


def test_durable_hwm_contract_promotes_unrealized_account_equity_highs():
    async def scenario():
        legacy = RiskManager()
        legacy.init(6.0944)
        risk = ProfessionalRiskAdapter(legacy)
        risk.update_capital(CapitalState(6.0944, 6.0944))
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="6.7008")), \
             patch("bot.drawdown_persistence.save_key_values_atomic", AsyncMock(return_value=True)):
            peak = await ddp.restore_update_real_account_peak(risk, 8.8015, strict=True)
        assert peak == 8.8015
        assert legacy.peak_balance == 8.8015
    asyncio.run(scenario())
