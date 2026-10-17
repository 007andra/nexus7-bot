"""Fail-closed Binance USD-M CROSS stop-stress guard.

For Binance CROSS, this module avoids reusing the KuCoin isolated-liquidation
formula. It asks Binance for the user's leverage brackets, projects every local
position plus the candidate at its protective stop, and requires conservative
account-level maintenance headroom.

Maintenance is deliberately overestimated as notional * maintMarginRatio.
Binance also returns a bracket cum auxiliary value, but this guard does not
infer an undocumented formula from it. Ignoring a possible deduction is
conservative.

PAPER uses only the simulated wallet and simulated positions. LIVE additionally
requires single-asset mode, CROSS positions, exact local/exchange position
coherence, no reserved open-order margin, and fresh private account data.
No function here changes leverage, margin mode, orders, stops, or release flags.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from bot.config import cfg
from bot.binance import TAKER_FEE


MAX_STOP_STRESS_RISK_RATE = 0.90


@dataclass(frozen=True)
class StressResult:
    allowed: bool
    reason: str
    risk_rate: float = 1.0
    stressed_margin: float = 0.0
    maintenance: float = 0.0
    closing_fees: float = 0.0
    opening_fee: float = 0.0
    existing_positions: int = 0
    mode: str = "UNKNOWN"


def _positive(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _nonnegative(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _candidate_geometry(sig, qty: float):
    entry = _positive(getattr(sig, "entry", None))
    stop = _positive(getattr(sig, "sl", None))
    qty = _positive(qty)
    direction = str(getattr(sig, "direction", "")).upper()
    symbol = str(getattr(sig, "symbol", "")).upper()
    if (
        not symbol
        or entry is None
        or stop is None
        or qty is None
        or direction not in {"LONG", "SHORT"}
    ):
        return None
    if direction == "LONG" and not stop < entry:
        return None
    if direction == "SHORT" and not stop > entry:
        return None
    return {
        "symbol": symbol,
        "entry": entry,
        "stop": stop,
        "qty": qty,
        "direction": direction,
        "notional_entry": qty * entry,
        "notional_stop": qty * stop,
        "pnl_stop": (
            qty * (stop - entry)
            if direction == "LONG"
            else qty * (entry - stop)
        ),
    }


def _effective_stop(position):
    trailing = _positive(getattr(position, "trailing_sl", None))
    original = _positive(getattr(position, "sl", None))
    return trailing if trailing is not None else original


def _local_geometry(position):
    symbol = str(getattr(position, "symbol", "")).upper()
    direction = str(getattr(position, "direction", "")).upper()
    entry = _positive(getattr(position, "entry", None))
    mark = _positive(getattr(position, "current_price", None))
    qty = _positive(getattr(position, "qty", None))
    stop = _effective_stop(position)
    if (
        not symbol
        or direction not in {"LONG", "SHORT"}
        or entry is None
        or mark is None
        or qty is None
        or stop is None
    ):
        return None
    if direction == "LONG" and not 0 < stop < mark:
        return None
    if direction == "SHORT" and not stop > mark > 0:
        return None
    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "mark": mark,
        "stop": stop,
        "qty": qty,
        "notional_stop": qty * stop,
        "pnl_stop": (
            qty * (stop - entry)
            if direction == "LONG"
            else qty * (entry - stop)
        ),
    }


def select_bracket(payload: dict, notional: float) -> dict:
    """Select the authoritative returned bracket for a positive notional."""
    notional = _positive(notional)
    if notional is None or not isinstance(payload, dict):
        raise ValueError("invalid_notional_or_brackets")
    brackets = payload.get("brackets")
    if not isinstance(brackets, list) or not brackets:
        raise ValueError("missing_brackets")

    for index, bracket in enumerate(brackets):
        if not isinstance(bracket, dict):
            raise ValueError("invalid_bracket")
        floor = _nonnegative(bracket.get("notionalFloor"))
        cap = _positive(bracket.get("notionalCap"))
        mmr = _positive(bracket.get("maintMarginRatio"))
        max_leverage = _positive(bracket.get("initialLeverage"))
        if floor is None or cap is None or mmr is None or max_leverage is None:
            raise ValueError("invalid_bracket_values")
        is_last = index == len(brackets) - 1
        if floor <= notional < cap or (is_last and floor <= notional <= cap):
            return bracket
    raise ValueError("notional_outside_brackets")


async def _maintenance(client, symbol: str, notional: float, *, enforce_leverage=False):
    payload = await client.get_leverage_brackets(symbol)
    bracket = select_bracket(payload, notional)
    mmr = float(bracket["maintMarginRatio"])
    max_leverage = int(bracket["initialLeverage"])
    if enforce_leverage and int(cfg.LEVERAGE) > max_leverage:
        raise ValueError("configured_leverage_exceeds_bracket")
    # Conservative: do not subtract Binance's auxiliary cum value.
    return float(notional) * mmr, mmr, int(bracket["bracket"]), max_leverage


def _quantity_tolerance(engine, symbol: str, expected: float) -> float:
    info = (getattr(engine, "instruments", {}) or {}).get(symbol, {})
    step = _positive(info.get("qtyStep")) if isinstance(info, dict) else None
    return max((step or 0.0) * 1.01, abs(expected) * 1e-6, 1e-12)


async def _paper_existing(engine):
    positions = getattr(engine, "positions", {}) or {}
    if not isinstance(positions, dict):
        raise ValueError("invalid_local_positions")
    result = []
    for position in positions.values():
        geometry = _local_geometry(position)
        if geometry is None:
            raise ValueError("invalid_local_position_geometry")
        result.append(geometry)
    wallet = _positive(getattr(getattr(engine, "risk", None), "balance", None))
    if wallet is None:
        raise ValueError("invalid_paper_wallet")
    return wallet, result


async def _live_existing(engine):
    client = getattr(engine, "client", None)
    if client is None:
        raise ValueError("missing_exchange_client")

    account = await client.get_account_state()
    if not isinstance(account, dict):
        raise ValueError("invalid_account_state")
    if bool(account.get("multiAssetsMargin", False)):
        raise ValueError("multi_assets_margin_unsupported")
    if account.get("canTrade") is not True:
        raise ValueError("account_trading_disabled")

    wallet = _positive(account.get("crossWalletBalance"))
    if wallet is None:
        raise ValueError("invalid_cross_wallet_balance")
    order_margin = _nonnegative(account.get("orderMargin"))
    if order_margin is None:
        raise ValueError("invalid_open_order_margin")
    if order_margin > 1e-12:
        raise ValueError("additional_open_order_margin")

    local_positions = getattr(engine, "positions", {}) or {}
    if not isinstance(local_positions, dict):
        raise ValueError("invalid_local_positions")
    exchange_positions = await client.get_positions()
    if not isinstance(exchange_positions, list):
        raise ValueError("invalid_exchange_positions")
    if len(exchange_positions) != len(local_positions):
        raise ValueError("exchange_local_position_count_mismatch")

    result = []
    for row in exchange_positions:
        if not isinstance(row, dict):
            raise ValueError("invalid_exchange_position")
        symbol = str(row.get("symbol", "")).upper()
        local = local_positions.get(symbol)
        if local is None:
            raise ValueError("external_or_unmapped_position")
        if str(row.get("marginType", "")).lower() != "cross":
            raise ValueError("position_not_cross")

        direction = "LONG" if str(row.get("side", "")).upper() == "BUY" else "SHORT"
        if str(getattr(local, "direction", "")).upper() != direction:
            raise ValueError("position_side_mismatch")

        exchange_qty = _positive(row.get("size"))
        local_qty = _positive(getattr(local, "qty", None))
        if exchange_qty is None or local_qty is None:
            raise ValueError("position_quantity_invalid")
        if abs(exchange_qty - local_qty) > _quantity_tolerance(engine, symbol, local_qty):
            raise ValueError("position_quantity_mismatch")

        entry = _positive(row.get("entryPrice"))
        mark = _positive(row.get("markPrice"))
        stop = _effective_stop(local)
        if entry is None or mark is None or stop is None:
            raise ValueError("position_geometry_missing")
        if direction == "LONG" and not 0 < stop < mark:
            raise ValueError("position_stop_not_adverse")
        if direction == "SHORT" and not stop > mark > 0:
            raise ValueError("position_stop_not_adverse")

        result.append({
            "symbol": symbol,
            "direction": direction,
            "entry": entry,
            "mark": mark,
            "stop": stop,
            "qty": exchange_qty,
            "notional_stop": exchange_qty * stop,
            "pnl_stop": (
                exchange_qty * (stop - entry)
                if direction == "LONG"
                else exchange_qty * (entry - stop)
            ),
        })
    return wallet, result


async def evaluate(engine, sig, qty: float) -> StressResult:
    """Evaluate simultaneous protective-stop stress using Binance brackets."""
    candidate = _candidate_geometry(sig, qty)
    if candidate is None:
        return StressResult(False, "invalid_candidate_geometry")

    positions_local = getattr(engine, "positions", {}) or {}
    if candidate["symbol"] in positions_local:
        return StressResult(False, "same_symbol_position_conflict")

    paper = bool(getattr(engine, "paper_trade", False))
    mode = "PAPER" if paper else "LIVE"

    try:
        wallet, existing = (
            await _paper_existing(engine) if paper else await _live_existing(engine)
        )
        if not paper:
            config = await engine.client.get_symbol_config(candidate["symbol"])
            if not isinstance(config, dict) or str(config.get("marginType", "")).upper() not in {"CROSS", "CROSSED"}:
                raise ValueError("candidate_cross_margin_unconfirmed")
            if str(config.get("leverage", "")) != str(int(cfg.LEVERAGE)):
                raise ValueError("candidate_configured_leverage_unconfirmed")
    except Exception as exc:
        return StressResult(False, f"state_{str(exc) or type(exc).__name__}", mode=mode)

    client = getattr(engine, "client", None)
    if client is None:
        return StressResult(False, "missing_exchange_client", mode=mode)

    stressed_margin = wallet
    maintenance = 0.0
    closing_fees = 0.0

    try:
        for position in existing:
            stressed_margin += float(position["pnl_stop"])
            maint, _, _, _ = await _maintenance(
                client, position["symbol"], position["notional_stop"]
            )
            maintenance += maint
            closing_fees += float(position["notional_stop"]) * float(TAKER_FEE)

        candidate_maint, _, bracket, max_leverage = await _maintenance(
            client,
            candidate["symbol"],
            max(candidate["notional_entry"], candidate["notional_stop"]),
            enforce_leverage=True,
        )
    except Exception as exc:
        return StressResult(
            False,
            f"bracket_{str(exc) or type(exc).__name__}",
            stressed_margin=stressed_margin,
            maintenance=maintenance,
            closing_fees=closing_fees,
            existing_positions=len(existing),
            mode=mode,
        )

    maintenance += candidate_maint
    stressed_margin += candidate["pnl_stop"]
    closing_fees += candidate["notional_stop"] * float(TAKER_FEE)
    opening_fee = candidate["notional_entry"] * float(TAKER_FEE)
    stressed_margin -= opening_fee

    if not math.isfinite(stressed_margin) or stressed_margin <= 0:
        return StressResult(
            False, "nonpositive_stressed_margin", 1.0, stressed_margin,
            maintenance, closing_fees, opening_fee, len(existing), mode,
        )

    risk_rate = (maintenance + closing_fees) / stressed_margin
    if not math.isfinite(risk_rate) or risk_rate < 0:
        return StressResult(False, "invalid_projected_risk_rate", mode=mode)

    allowed = risk_rate < MAX_STOP_STRESS_RISK_RATE
    reason = (
        f"binance_cross_stop_stress_ok_bracket_{bracket}_maxlev_{max_leverage}"
        if allowed
        else "binance_cross_stop_stress_risk_rate_too_high"
    )
    return StressResult(
        allowed,
        reason,
        risk_rate,
        stressed_margin,
        maintenance,
        closing_fees,
        opening_fee,
        len(existing),
        mode,
    )


def final_dispatch_context_valid(engine, sig, qty) -> bool:
    """Bind the final refresh to the actual LIVE order, never a pre-sizing skip.

    Called by core _open immediately before its final refresh. No sizing or
    policy is performed here: missing/drifted context must not skip the gate.
    """
    from bot import pilot_risk_cap_hardening as risk_cap

    final_qty = _positive(risk_cap._PILOT_FINAL_QTY.get())
    actual_qty = _positive(qty)
    return (
        bool(getattr(getattr(engine, "pilot", None), "enabled", False))
        and risk_cap._PILOT_ENGINE.get() is engine
        and risk_cap._PILOT_SIGNAL.get() is sig
        and risk_cap._PILOT_SYMBOL.get() == getattr(sig, "symbol", None)
        and final_qty is not None
        and actual_qty is not None
        and final_qty == actual_qty
    )


def install(TradingEngine, log) -> None:
    """Recheck Binance CROSS stress at the final LIVE pre-dispatch boundary."""
    if getattr(TradingEngine, "_binance_cross_portfolio_stress_installed", False):
        return

    from bot import pilot_risk_cap_hardening as risk_cap

    original_refresh = TradingEngine._refresh_entry_balance

    async def _refresh_with_binance_cross_stress(self, *args, **kwargs):
        ok = await original_refresh(self, *args, **kwargs)
        if not ok:
            return ok

        if getattr(self, "paper_trade", False):
            return ok
        if not bool(getattr(getattr(self, "pilot", None), "enabled", False)):
            return ok

        sig = risk_cap._PILOT_SIGNAL.get()
        qty = risk_cap._PILOT_FINAL_QTY.get()
        if sig is None or qty is None:
            return ok

        try:
            qty = float(qty)
        except (TypeError, ValueError):
            qty = 0.0
        if not math.isfinite(qty) or qty <= 0:
            log.critical(
                "[BINANCE_CROSS_STRESS] symbol=%s result=BLOCK "
                "reason=invalid_final_qty stage=FINAL_PREDISPATCH "
                "execution_effect=BLOCK_NEW_LIVE_ENTRY",
                getattr(sig, "symbol", "?"),
            )
            return False

        result = await evaluate(self, sig, qty)
        if not result.allowed:
            log.warning(
                "[BINANCE_CROSS_STRESS] symbol=%s result=BLOCK reason=%s "
                "mode=%s risk_rate=%.4f limit=%.4f stressed_margin=%.8f "
                "maintenance=%.8f closing_fees=%.8f opening_fee=%.8f "
                "existing_positions=%d stage=FINAL_PREDISPATCH "
                "execution_effect=BLOCK_NEW_LIVE_ENTRY",
                getattr(sig, "symbol", "?"),
                result.reason,
                result.mode,
                result.risk_rate,
                MAX_STOP_STRESS_RISK_RATE,
                result.stressed_margin,
                result.maintenance,
                result.closing_fees,
                result.opening_fee,
                result.existing_positions,
            )
            return False

        log.warning(
            "[BINANCE_CROSS_STRESS] symbol=%s result=PASS reason=%s mode=%s "
            "risk_rate=%.4f limit=%.4f stressed_margin=%.8f "
            "maintenance=%.8f closing_fees=%.8f opening_fee=%.8f "
            "existing_positions=%d conservative_cum_deduction=false "
            "stage=FINAL_PREDISPATCH execution_effect=NONE",
            getattr(sig, "symbol", "?"),
            result.reason,
            result.mode,
            result.risk_rate,
            MAX_STOP_STRESS_RISK_RATE,
            result.stressed_margin,
            result.maintenance,
            result.closing_fees,
            result.opening_fee,
            result.existing_positions,
        )
        return ok

    TradingEngine._refresh_entry_balance = _refresh_with_binance_cross_stress
    TradingEngine._binance_cross_portfolio_stress_installed = True
    log.warning(
        "[BINANCE_CROSS_STRESS] installed=true model=ACCOUNT_LEVEL_STOP_STRESS "
        "brackets=BINANCE_USER_DATA maintenance=NOTIONAL_X_MMR_CONSERVATIVE "
        "cum_deduction_used=false multi_assets=BLOCK external_positions=BLOCK "
        "missing_state=BLOCK max_risk_rate=90pct execution_effect=NONE"
    )
