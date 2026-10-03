"""F-003 — stop-loss risk budget: canonical cost, open-risk reservation and the
final pre-dispatch barrier for every order that opens risk.

Order of operations (sizing lives in RiskManagerV3 / professional_risk):

    TECHNICAL STOP -> RISK BUDGET (equity x MAX_RISK_PCT) -> CONTRACTS (floor)
    -> MARGIN CEILING (available x MAX_MARGIN_PCT, only reduces)
    -> LIQUIDATION SAFETY (no stop compression) -> OPEN-RISK CAP
    -> FINAL INVARIANT (here, at the transport boundary) -> POST

Capital semantics: the risk budget is a fraction of account EQUITY; available
collateral only constrains margin. Leverage changes required margin, never the
risk budget.

INV-RISK-SIZING-001     projected loss at the stop (modeled costs included)
                        <= equity x risk_pct for every accepted entry.
INV-PREDISPATCH-RISK-001 no order that opens risk reaches the transport unless
                        its contracts, native stop and the freshest entry price
                        keep the projected loss within the authorized budget.
INV-OPEN-RISK-001       sum of risk reserved by open BGX positions + the new
                        entry <= equity x MAX_OPEN_RISK_PCT. Each open position
                        reserves its INITIAL budget; nothing is released when a
                        stop is trailed (simple, conservative, deterministic).

Funding is not part of the sizing cost model (no reliable forward model).
"""
from __future__ import annotations

import contextvars
import math
from dataclasses import dataclass, replace
from decimal import Decimal

from bot.logger import log

RISK_PCT_HARD_CEILING = 0.05       # structural: a mis-set env can never exceed it
OPEN_RISK_HARD_CEILING = 0.10
ENTRY_ENDPOINTS = ("/api/v1/orders", "/api/v1/st-orders")

MIN_CONTRACT_EXCEEDS_RISK_BUDGET = "MIN_CONTRACT_EXCEEDS_RISK_BUDGET"
MIN_CONTRACT_EXCEEDS_MARGIN_CAP = "MIN_CONTRACT_EXCEEDS_MARGIN_CAP"
TECHNICAL_STOP_OUTSIDE_LIQUIDATION_SAFE_ZONE = "TECHNICAL_STOP_OUTSIDE_LIQUIDATION_SAFE_ZONE"


class RiskBudgetRefused(RuntimeError):
    def __init__(self, reason, detail=""):
        self.reason = str(reason)
        super().__init__(f"{reason}{': ' + detail if detail else ''}")


def _finite_positive(value, name):
    if isinstance(value, bool):
        raise RiskBudgetRefused("INVALID_INPUT", f"{name}=bool")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise RiskBudgetRefused("INVALID_INPUT", f"{name}={value!r}") from None
    if not math.isfinite(number) or number <= 0:
        raise RiskBudgetRefused("INVALID_INPUT", f"{name}={value!r}")
    return number


def validate_risk_pct(value, *, ceiling=RISK_PCT_HARD_CEILING, name="risk_pct"):
    """0 < pct <= ceiling, never normalized silently."""
    pct = _finite_positive(value, name)
    if pct > ceiling:
        raise RiskBudgetRefused("RISK_PCT_ABOVE_HARD_CEILING", f"{name}={pct} ceiling={ceiling}")
    return pct


def configured_risk_limits():
    from bot.config import cfg
    open_pct = validate_risk_pct(cfg.MAX_OPEN_RISK_PCT, ceiling=OPEN_RISK_HARD_CEILING,
                                 name="MAX_OPEN_RISK_PCT")
    margin_pct = _finite_positive(cfg.MAX_MARGIN_PCT, "MAX_MARGIN_PCT")
    if margin_pct > 1:
        raise RiskBudgetRefused("INVALID_INPUT", f"MAX_MARGIN_PCT={margin_pct}")
    return open_pct, margin_pct


def cost_fraction(symbol):
    """Canonical per-symbol round-trip cost (2 taker fees + 2 adverse slips).

    Majors (BTC/ETH/SOL) use the base slippage, alts 2x base — the same model
    used by research and geometry, so an alt is never costed as a major.
    """
    from bot.kucoin_execution_model import estimated_round_trip_cost_pct
    value = estimated_round_trip_cost_pct(symbol) / 100.0
    if not math.isfinite(value) or value < 0:
        raise RiskBudgetRefused("INVALID_COST_MODEL", f"{symbol}={value!r}")
    return value


def _d(value):
    return Decimal(str(value))


def projected_loss(contracts, multiplier, entry, stop, cost):
    """contracts x multiplier x (|entry - stop| + entry x cost), in USDT (exact)."""
    return _d(contracts) * _d(multiplier) * (abs(_d(entry) - _d(stop)) + _d(entry) * _d(cost))


def assert_projected_loss_within_budget(*, symbol, contracts, multiplier, entry, stop,
                                        direction, cost_fraction, equity, risk_pct,
                                        stage="PREDISPATCH", leverage=None):
    """Raise RiskBudgetRefused unless the loss at the stop fits equity x risk_pct."""
    contracts_f = _finite_positive(contracts, "contracts")
    if contracts_f != int(contracts_f):
        raise RiskBudgetRefused("INVALID_INPUT", f"contracts={contracts} not integral")
    mult = _finite_positive(multiplier, "multiplier")
    entry_f = _finite_positive(entry, "entry")
    stop_f = _finite_positive(stop, "stop")
    equity_f = _finite_positive(equity, "equity")
    pct = validate_risk_pct(risk_pct)
    if isinstance(cost_fraction, bool) or not math.isfinite(float(cost_fraction)) \
            or float(cost_fraction) < 0:
        raise RiskBudgetRefused("INVALID_INPUT", f"cost_fraction={cost_fraction!r}")
    if direction == "LONG":
        valid_side = stop_f < entry_f
    elif direction == "SHORT":
        valid_side = stop_f > entry_f
    else:
        raise RiskBudgetRefused("INVALID_INPUT", f"direction={direction!r}")
    if not valid_side:
        raise RiskBudgetRefused("STOP_ON_INVALID_SIDE", f"entry={entry_f} stop={stop_f}")
    loss = projected_loss(int(contracts_f), mult, entry_f, stop_f, cost_fraction)
    budget = _d(equity_f) * _d(pct)
    notional = _d(int(contracts_f)) * _d(mult) * _d(entry_f)
    metrics = {
        "symbol": symbol, "equity": equity_f, "risk_pct": pct, "risk_budget": float(budget),
        "entry": entry_f, "stop": stop_f, "stop_pct": abs(entry_f - stop_f) / entry_f,
        "cost_fraction": float(cost_fraction), "contracts": int(contracts_f),
        "notional": float(notional),
        "margin": float(notional / _d(leverage)) if leverage else float("nan"),
        "projected_loss": float(loss), "projected_loss_pct": float(loss / _d(equity_f)),
        "leverage": leverage,
    }
    ok = loss <= budget * (Decimal(1) + Decimal("1e-9"))
    (log.info if ok else log.critical)(
        "[PREDISPATCH_RISK_INVARIANT] stage=%s symbol=%s result=%s equity=%.6f risk_pct=%.4f "
        "risk_budget=%.6f entry=%.10g stop=%.10g stop_pct=%.5f cost_fraction=%.5f "
        "contracts=%s notional=%.6f projected_loss=%.6f projected_loss_pct=%.5f leverage=%s",
        stage, symbol, "PASS" if ok else "BLOCK", equity_f, pct, metrics["risk_budget"],
        entry_f, stop_f, metrics["stop_pct"], float(cost_fraction), int(contracts_f),
        metrics["notional"], metrics["projected_loss"], metrics["projected_loss_pct"], leverage,
    )
    if not ok:
        raise RiskBudgetRefused("PROJECTED_LOSS_EXCEEDS_RISK_BUDGET",
                                f"{float(loss):.8f}>{float(budget):.8f}")
    return metrics


# ── Open-risk reservation (INV-OPEN-RISK-001) ────────────────────────────────
def position_reserved_risk(position, *, equity, risk_pct, symbol=""):
    """Initial risk reserved by an open BGX position.

    Stamped at entry (= the full per-trade budget authorized then). A position
    without a stamp (restart/adoption) reserves the larger of today's per-trade
    budget and its loss to the initial (else current) stop: never less.
    """
    stamped = getattr(position, "_risk_reserved_usdt", None)
    if isinstance(stamped, (int, float)) and not isinstance(stamped, bool) \
            and math.isfinite(stamped) and stamped > 0:
        return float(stamped)
    try:
        qty = _finite_positive(getattr(position, "qty", None), "qty")
        entry = _finite_positive(getattr(position, "entry", None), "entry")
        stop = getattr(position, "initial_sl", None) or getattr(position, "sl", None)
        stop = _finite_positive(stop, "stop")
        cost = cost_fraction(symbol or getattr(position, "symbol", ""))
        computed = qty * (abs(entry - stop) + entry * cost)
    except RiskBudgetRefused as exc:
        raise RiskBudgetRefused("OPEN_RISK_UNRESOLVED", f"{symbol}:{exc}") from None
    return max(float(equity) * float(risk_pct), computed)


def reserved_open_risk(engine, *, equity, risk_pct):
    external = set(getattr(engine, "_external_position_symbols", set()) or set())
    total, detail = 0.0, {}
    for symbol, position in list((getattr(engine, "positions", {}) or {}).items()):
        if symbol in external:
            continue   # EXTERNAL: no BGX budget invented; collateral/slots govern it
        reserved = position_reserved_risk(position, equity=equity, risk_pct=risk_pct,
                                          symbol=symbol)
        detail[symbol] = reserved
        total += reserved
    return total, detail


def assert_open_risk_within_cap(engine, *, symbol, proposed, equity, risk_pct, open_pct):
    reserved, detail = reserved_open_risk(engine, equity=equity, risk_pct=risk_pct)
    cap = float(equity) * float(open_pct)
    total = reserved + float(proposed)
    if total > cap * (1 + 1e-9):
        log.critical(
            "[OPEN_RISK_CAP_REJECTED] symbol=%s equity=%.6f reserved=%.6f proposed=%.6f "
            "total=%.6f cap=%.6f aggregate_reserved_pct=%.5f open_positions=%s",
            symbol, equity, reserved, proposed, total, cap, total / float(equity),
            ",".join(sorted(detail)) or "NONE",
        )
        raise RiskBudgetRefused("OPEN_RISK_CAP_EXCEEDED", f"{total:.6f}>{cap:.6f}")
    return reserved, total


# ── Authorization carried from sizing to the transport boundary ──────────────
@dataclass(frozen=True)
class RiskAuthorization:
    symbol: str
    side: str            # "buy" | "sell"
    direction: str       # "LONG" | "SHORT"
    contracts: int
    multiplier: float
    entry: float
    stop: float
    cost_fraction: float
    equity: float
    risk_pct: float
    risk_budget: float
    projected_loss: float
    reserved_before: float
    leverage: float


_AUTHORIZATION = contextvars.ContextVar("nexus_f003_risk_authorization", default=None)


def authorize(auth):
    return _AUTHORIZATION.set(auth)


def current_authorization():
    return _AUTHORIZATION.get()


def reset_authorization(token):
    _AUTHORIZATION.reset(token)


def update_entry_price(price):
    """Re-anchor the authorized entry to the freshest executable price."""
    auth = _AUTHORIZATION.get()
    if auth is not None:
        _AUTHORIZATION.set(replace(auth, entry=float(price)))


def _native_stop(endpoint, body):
    if endpoint != "/api/v1/st-orders":
        return None
    key = "triggerStopDownPrice" if str(body.get("side", "")).lower() == "buy" else "triggerStopUpPrice"
    return body.get(key)


def assert_transport_dispatch(client, endpoint, body):
    """INV-PREDISPATCH-RISK-001 — last check before session.post for new risk."""
    if endpoint not in ENTRY_ENDPOINTS or body.get("reduceOnly") is True \
            or body.get("closeOrder") is True:
        return None
    symbol_kc = str(body.get("symbol", ""))
    try:
        from bot.kucoin import to_standard
        symbol = to_standard(symbol_kc)
    except Exception:
        symbol = symbol_kc
    auth = _AUTHORIZATION.get()

    def refuse(reason, detail=""):
        log.critical(
            "[PREDISPATCH_RISK_INVARIANT] stage=TRANSPORT symbol=%s result=BLOCK reason=%s "
            "detail=%s endpoint=%s exchange_dispatch=NONE", symbol, reason, detail or "-", endpoint)
        raise RiskBudgetRefused(reason, detail)

    if auth is None:
        refuse("NO_RISK_AUTHORIZATION")
    if auth.symbol != symbol or str(body.get("side", "")).lower() != auth.side:
        refuse("RISK_AUTHORIZATION_MISMATCH", f"auth={auth.symbol}/{auth.side}")
    try:
        contracts = int(body.get("size"))
    except (TypeError, ValueError):
        refuse("INVALID_INPUT", f"size={body.get('size')!r}")
    if contracts <= 0 or contracts > auth.contracts:
        refuse("CONTRACTS_ABOVE_AUTHORIZATION", f"{contracts}>{auth.contracts}")
    stop = _native_stop(endpoint, body)
    if stop in (None, "", 0):
        refuse("NATIVE_STOP_MISSING")
    instruments = getattr(client, "_instruments", None) or {}
    multiplier = (instruments.get(symbol) or {}).get("multiplier", auth.multiplier)
    if float(multiplier) != float(auth.multiplier):
        refuse("MULTIPLIER_MISMATCH", f"{multiplier}!={auth.multiplier}")
    try:
        assert_projected_loss_within_budget(
            symbol=symbol, contracts=contracts, multiplier=multiplier, entry=auth.entry,
            stop=stop, direction=auth.direction, cost_fraction=auth.cost_fraction,
            equity=auth.equity, risk_pct=auth.risk_pct, stage="TRANSPORT",
            leverage=auth.leverage)
    except RiskBudgetRefused as exc:
        refuse(exc.reason, str(exc))
    return True
