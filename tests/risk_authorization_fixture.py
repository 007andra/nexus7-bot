"""Test helper: the F-003 risk authorization a real ``_open`` carries to transport.

Production entries reach the transport boundary only with the authorization
created by ``final_sizing_invariants`` (INV-PREDISPATCH-RISK-001). Transport
tests that call ``place_order`` directly supply the same object explicitly,
sized so the order under test fits a 1% budget.
"""
from contextlib import contextmanager

from bot import risk_budget


@contextmanager
def risk_authorized(symbol, side, contracts, entry, stop, multiplier, *, risk_pct=0.01):
    side = str(side).lower()
    direction = "LONG" if side == "buy" else "SHORT"
    cost = risk_budget.cost_fraction(symbol)
    loss = float(risk_budget.projected_loss(contracts, multiplier, entry, stop, cost))
    equity = loss / risk_pct * 1.5
    auth = risk_budget.RiskAuthorization(
        symbol=symbol, side=side, direction=direction, contracts=int(contracts),
        multiplier=float(multiplier), entry=float(entry), stop=float(stop),
        cost_fraction=cost, equity=equity, risk_pct=risk_pct,
        risk_budget=equity * risk_pct, projected_loss=loss, reserved_before=0.0,
        leverage=10.0)
    token = risk_budget.authorize(auth)
    try:
        yield auth
    finally:
        risk_budget.reset_authorization(token)
