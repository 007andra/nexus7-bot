from types import SimpleNamespace
from unittest.mock import patch

from bot import min_order_feasibility_matrix as matrix


def _info(step="0.001", min_qty="0.001", min_notional="5"):
    return {
        "quantityUnit": "BASE_ASSET",
        "qtyStep": step,
        "minQty": min_qty,
        "minNotional": min_notional,
        "multiplier": 1.0,
    }


def test_min_notional_binding_derives_stop_envelope():
    row = matrix.audit_symbol(
        info=_info(step="0.01", min_qty="0.01", min_notional="20"),
        price=100,
        equity=8.7583,
        available=8.7583,
        risk_pct=0.005,
        leverage=50,
        max_margin_pct=1.0,
        fee_rate_per_side=0.0005,
        slippage_pct=0.001,
    )
    assert row["binding"] == "MIN_NOTIONAL_BINDING"
    assert float(row["min_valid_qty"]) == 0.2
    assert float(row["risk_budget"]) > 0
    assert float(row["max_stop_pct"]) > 0


def test_min_qty_binding_derives_stop_envelope():
    row = matrix.audit_symbol(
        info=_info(step="0.1", min_qty="0.1", min_notional="5"),
        price=175,
        equity=8.7583,
        available=8.7583,
        risk_pct=0.005,
        leverage=50,
        max_margin_pct=1.0,
        fee_rate_per_side=0.0005,
        slippage_pct=0.001,
    )
    assert row["binding"] == "MIN_QTY_BINDING"
    assert float(row["min_valid_qty"]) == 0.1


def test_cost_block_when_cost_alone_exceeds_budget():
    row = matrix.audit_symbol(
        info=_info(step="0.1", min_qty="0.1", min_notional="0"),
        price=100,
        equity=1,
        available=1,
        risk_pct=0.005,
        leverage=50,
        max_margin_pct=1.0,
        fee_rate_per_side=0.001,
        slippage_pct=0.002,
    )
    assert row["status"] == "COST_BLOCK"
    assert float(row["max_stop_pct"]) == 0.0


def test_margin_block_is_observability_only_classification():
    row = matrix.audit_symbol(
        info=_info(step="1", min_qty="1", min_notional="5"),
        price=100,
        equity=1000,
        available=0.01,
        risk_pct=0.01,
        leverage=50,
        max_margin_pct=1.0,
        fee_rate_per_side=0.0005,
        slippage_pct=0.001,
    )
    assert row["status"] == "MARGIN_BLOCK"


def test_build_matrix_uses_recovery_adjusted_risk_without_mutation():
    engine = SimpleNamespace(
        risk=SimpleNamespace(balance=8.7583, drawdown=0.6158),
        instruments={"XUSDT": _info(step="1", min_qty="1", min_notional="5")},
        _pilot_available_balance=8.7583,
        _effective_risk_pct=lambda: 0.01,
    )
    before = dict(engine.instruments["XUSDT"])
    with patch.object(matrix.cfg, "SYMBOLS", ["XUSDT"]), \
         patch.object(matrix.cfg, "LEVERAGE", 50), \
         patch.object(matrix.cfg, "MAX_MARGIN_PCT", 1.0), \
         patch.object(matrix, "recovery_size_multiplier", return_value=0.5), \
         patch.object(matrix.execution_cost, "fallback_taker_fee", return_value=0.0005):
        rows = matrix.build_matrix(engine, {"XUSDT": 1.0})
    assert len(rows) == 1
    assert abs(float(rows[0]["risk_budget"]) - (8.7583 * 0.005)) < 1e-12
    assert engine.instruments["XUSDT"] == before
