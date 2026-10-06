"""Research-only low-capital Binance USD-M universe screen.

Reads public Binance exchange metadata and 24h tickers to rank the full
USDT-perpetual universe by the maximum stop distance tolerated by the
smallest exchange-valid order under the runtime's current risk budget.

This module never mutates cfg.SYMBOLS, engine.instruments,
engine.viable_symbols, sizing, leverage, thresholds, dispatch, or orders.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import os
from typing import Any

from bot.config import cfg
from bot import execution_cost
from bot import min_order_feasibility_matrix as feasibility


AUTHORITY = {
    "research_only": True,
    "observability_only": True,
    "shadow_only": True,
    "live_eligible": False,
    "live_candidate": False,
    "automatic_promotion": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "live_authority_unchanged": True,
}
FLAG = "LOW_CAPITAL_UNIVERSE_V1"


def enabled() -> bool:
    return os.environ.get(FLAG, "true").lower() == "true"


def _instrument_rows(exchange_info: dict[str, Any]) -> dict[str, dict[str, float | str]]:
    rows: dict[str, dict[str, float | str]] = {}
    for item in exchange_info.get("symbols", []) if isinstance(exchange_info, dict) else []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).upper()
        if (
            not symbol
            or item.get("contractType") != "PERPETUAL"
            or str(item.get("quoteAsset", "")).upper() != "USDT"
            or str(item.get("status", "")).upper() != "TRADING"
        ):
            continue
        filters = {
            str(f.get("filterType")): f
            for f in item.get("filters", [])
            if isinstance(f, dict)
        }
        price_filter = filters.get("PRICE_FILTER", {})
        lot_filter = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE", {})
        notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL", {})
        try:
            tick = float(price_filter.get("tickSize", 0) or 0)
            step = float(lot_filter.get("stepSize", 0) or 0)
            min_qty = float(lot_filter.get("minQty", 0) or 0)
            min_notional = float(
                notional_filter.get("notional", notional_filter.get("minNotional", 0)) or 0
            )
        except (TypeError, ValueError):
            continue
        if tick <= 0 or step <= 0 or min_qty <= 0:
            continue
        rows[symbol] = {
            "minQty": min_qty,
            "lotSize": min_qty,
            "qtyStep": step,
            "tickSize": tick,
            "multiplier": 1.0,
            "minBaseQty": min_qty,
            "minNotional": min_notional,
            "maxLeverage": 0.0,
            "binanceSymbol": symbol,
            "quantityUnit": "BASE_ASSET",
        }
    return rows


def _ticker_rows(raw: Any) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).upper()
        if not symbol:
            continue
        try:
            price = float(row.get("lastPrice", 0) or 0)
            quote_volume = float(row.get("quoteVolume", 0) or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        out[symbol] = {"price": price, "quote_volume_usdt": max(0.0, quote_volume)}
    return out


def build_snapshot(
    engine,
    exchange_info: dict[str, Any],
    tickers: Any,
) -> tuple[dict[str, object], ...]:
    equity = Decimal(str(getattr(getattr(engine, "risk", None), "balance", 0.0) or 0.0))
    if not equity.is_finite() or equity <= 0:
        raise ValueError("equity unavailable")
    available = feasibility.available_collateral(engine, equity)
    risk_pct = feasibility.effective_risk_pct(engine)
    fee = Decimal(str(execution_cost.fallback_taker_fee()))
    slippage = Decimal(os.environ.get("NEXUS_EXPECTED_SLIPPAGE_PCT", "0.001"))

    instruments = _instrument_rows(exchange_info)
    prices = _ticker_rows(tickers)
    configured = set(cfg.SYMBOLS)
    rows: list[dict[str, object]] = []

    for symbol, info in instruments.items():
        ticker = prices.get(symbol)
        if ticker is None:
            continue
        row = feasibility.audit_symbol(
            info=info,
            price=ticker["price"],
            equity=equity,
            available=available,
            risk_pct=risk_pct,
            leverage=cfg.LEVERAGE,
            max_margin_pct=getattr(cfg, "MAX_MARGIN_PCT", 0.80),
            fee_rate_per_side=fee,
            slippage_pct=slippage,
        )
        rows.append(
            {
                "symbol": symbol,
                "configured_live_universe": symbol in configured,
                "status": row["status"],
                "binding": row["binding"],
                "price": ticker["price"],
                "quote_volume_usdt": ticker["quote_volume_usdt"],
                "min_valid_qty": float(row["min_valid_qty"]),
                "min_order_notional": float(row["min_order_notional"]),
                "margin_at_min": float(row["margin_at_min"]),
                "margin_cap": float(row["margin_cap"]),
                "max_stop_pct": float(row["max_stop_pct"]),
                "risk_budget": float(row["risk_budget"]),
                **AUTHORITY,
            }
        )

    status_rank = {"CONDITIONAL": 0, "MARGIN_BLOCK": 1, "COST_BLOCK": 2}
    rows.sort(
        key=lambda item: (
            status_rank.get(str(item["status"]), 9),
            -float(item["max_stop_pct"]),
            -float(item["quote_volume_usdt"]),
            str(item["symbol"]),
        )
    )
    for idx, row in enumerate(rows, 1):
        row["research_rank"] = idx
    return tuple(rows)


async def collect(engine) -> tuple[dict[str, object], ...]:
    client = getattr(engine, "client", None)
    getter = getattr(client, "_get", None)
    if getter is None:
        raise RuntimeError("binance public getter unavailable")
    exchange_info, tickers = await asyncio.gather(
        asyncio.wait_for(getter("/fapi/v1/exchangeInfo"), timeout=8.0),
        asyncio.wait_for(getter("/fapi/v1/ticker/24hr"), timeout=8.0),
    )
    return build_snapshot(engine, exchange_info, tickers)


def _fmt(row: dict[str, object]) -> str:
    return (
        f"{row['symbol']}:rank={row['research_rank']}:status={row['status']}"
        f":configured={str(row['configured_live_universe']).lower()}"
        f":max_stop_pct={float(row['max_stop_pct']):.6f}"
        f":min_notional={float(row['min_order_notional']):.6f}"
        f":quote_volume={float(row['quote_volume_usdt']):.2f}"
    )


async def _run(engine, log) -> None:
    try:
        rows = await collect(engine)
        conditional = [r for r in rows if r["status"] == "CONDITIONAL"]
        outsiders = [r for r in conditional if not r["configured_live_universe"]]
        configured = [r for r in conditional if r["configured_live_universe"]]
        authority = " ".join(
            f"{k}={str(v).lower() if isinstance(v, bool) else v}"
            for k, v in AUTHORITY.items()
        )
        log.warning(
            "[LOW_CAPITAL_UNIVERSE_V1] markets=%d conditional=%d "
            "conditional_configured=%d conditional_outside_live=%d "
            "top_outside=%s %s",
            len(rows),
            len(conditional),
            len(configured),
            len(outsiders),
            "|".join(_fmt(r) for r in outsiders[:12]) or "NONE",
            authority,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning(
            "[LOW_CAPITAL_UNIVERSE_V1] status=DEFER reason=%s "
            "research_only=true observability_only=true live_allowed=false "
            "decision_effect=NONE execution_effect=NONE",
            type(exc).__name__,
        )


def schedule_if_enabled(engine, log) -> bool:
    if not enabled():
        return False
    task = getattr(engine, "_low_capital_universe_v1_task", None)
    if task is not None and not task.done():
        return False

    starter = getattr(engine, "_start_background", None)
    if not callable(starter):
        raise RuntimeError("engine background manager unavailable")

    task = starter(_run(engine, log))
    engine._low_capital_universe_v1_task = task
    log.warning(
        "[LOW_CAPITAL_UNIVERSE_V1] status=SCHEDULED "
        "scheduler=ENGINE_BACKGROUND_MANAGER "
        "research_only=true observability_only=true shadow_only=true "
        "live_allowed=false decision_effect=NONE execution_effect=NONE"
    )
    return True
