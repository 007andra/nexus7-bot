"""Fail-closed Binance USD-M leverage reconciliation.

This module aligns the exchange-side per-symbol leverage with cfg.LEVERAGE
during engine startup. It never changes margin mode, never creates/cancels
orders, and never weakens the final read-only leverage gate in
binance_cross_portfolio_stress.

Safety model:
- LIVE Binance only;
- execution ownership/fencing must be valid before every leverage mutation;
- the account must be globally flat with no normal or algo orders before the
  reconciliation begins;
- configured leverage must be supported by the user-specific Binance brackets;
- each mutation is single-attempt via BinanceClient.set_leverage;
- every write is followed by an authenticated readback;
- a final exposure recheck is required before the reconciliation is considered
  ready;
- any uncertainty leaves _binance_leverage_sync_ready=False. The downstream
  CROSS stress gate remains authoritative and fail-closed.
"""
from __future__ import annotations

import math
from typing import Iterable

from bot.binance import BinanceClient
from bot.config import cfg
from bot.execution_ownership import validate_execution_ownership


def _raw_client(engine):
    client = getattr(engine, "client", None)
    return getattr(client, "_client", client)


def _unique_symbols(symbols: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols or ():
        symbol = str(raw or "").upper().strip()
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


async def _validated_ownership(engine, raw_client):
    if not bool(getattr(engine, "_execution_ownership_valid", False)):
        raise RuntimeError("execution_ownership_not_ready")
    ownership = getattr(raw_client, "_execution_ownership", None)
    if ownership is None:
        ownership = getattr(getattr(engine, "client", None), "_execution_ownership", None)
    if ownership is None:
        raise RuntimeError("execution_ownership_missing")
    await validate_execution_ownership(ownership)
    return ownership


async def _account_exposure_clear(raw_client) -> bool:
    positions = await raw_client.get_positions()
    if not isinstance(positions, list):
        raise RuntimeError("positions_unconfirmed")
    if positions:
        return False

    orders = await raw_client.get_open_orders()
    if not isinstance(orders, list):
        raise RuntimeError("open_orders_unconfirmed")
    if orders:
        return False

    algo = await raw_client._get("/fapi/v1/openAlgoOrders", auth=True)
    if isinstance(algo, dict):
        algo = algo.get("orders")
    if not isinstance(algo, list):
        raise RuntimeError("open_algo_orders_unconfirmed")
    return not algo


def _config_values(config: dict, symbol: str) -> tuple[int, str]:
    if not isinstance(config, dict):
        raise RuntimeError("symbol_config_unconfirmed")
    configured_symbol = str(config.get("symbol", symbol) or "").upper()
    if configured_symbol != symbol:
        raise RuntimeError("symbol_config_identity_mismatch")
    margin_type = str(config.get("marginType", "") or "").upper()
    if margin_type not in {"CROSS", "CROSSED"}:
        raise RuntimeError("candidate_cross_margin_unconfirmed")
    leverage = config.get("leverage")
    if isinstance(leverage, bool):
        raise RuntimeError("exchange_leverage_invalid")
    try:
        leverage_int = int(leverage)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("exchange_leverage_invalid") from exc
    if leverage_int <= 0:
        raise RuntimeError("exchange_leverage_invalid")
    return leverage_int, margin_type


async def _max_supported_leverage(raw_client, symbol: str) -> int:
    payload = await raw_client.get_leverage_brackets(symbol)
    if not isinstance(payload, dict):
        raise RuntimeError("leverage_brackets_unconfirmed")
    rows = payload.get("brackets")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("leverage_brackets_unconfirmed")
    values: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("leverage_brackets_invalid")
        raw = row.get("initialLeverage")
        if isinstance(raw, bool):
            raise RuntimeError("leverage_brackets_invalid")
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("leverage_brackets_invalid") from exc
        if value <= 0:
            raise RuntimeError("leverage_brackets_invalid")
        values.append(value)
    maximum = max(values)
    if not math.isfinite(float(maximum)) or maximum <= 0:
        raise RuntimeError("leverage_brackets_invalid")
    return maximum


async def reconcile(engine, log) -> bool:
    """Align viable Binance symbols to cfg.LEVERAGE without weakening dispatch."""
    engine._binance_leverage_sync_ready = False

    if getattr(engine, "paper_trade", False):
        engine._binance_leverage_sync_ready = True
        return True

    raw_client = _raw_client(engine)
    if not isinstance(raw_client, BinanceClient):
        engine._binance_leverage_sync_ready = True
        return True

    try:
        target = int(cfg.LEVERAGE)
    except (TypeError, ValueError):
        target = 0
    if not 1 <= target <= 125:
        log.critical(
            "[BINANCE_LEVERAGE_RECONCILE] result=BLOCK reason=invalid_target "
            "configured_leverage=%s execution_effect=BLOCK_NEW_LIVE_ENTRY",
            getattr(cfg, "LEVERAGE", None),
        )
        return False

    symbols = _unique_symbols(getattr(engine, "viable_symbols", None) or cfg.SYMBOLS)
    if not symbols:
        log.critical(
            "[BINANCE_LEVERAGE_RECONCILE] result=BLOCK reason=no_symbols "
            "execution_effect=BLOCK_NEW_LIVE_ENTRY"
        )
        return False

    try:
        await _validated_ownership(engine, raw_client)
        if not await _account_exposure_clear(raw_client):
            log.critical(
                "[BINANCE_LEVERAGE_RECONCILE] result=BLOCK reason=account_exposure_present "
                "target=%dx mutation=false execution_effect=BLOCK_NEW_LIVE_ENTRY",
                target,
            )
            return False
    except Exception as exc:
        log.critical(
            "[BINANCE_LEVERAGE_RECONCILE] result=BLOCK stage=precheck reason=%s "
            "target=%dx mutation=false execution_effect=BLOCK_NEW_LIVE_ENTRY",
            type(exc).__name__,
            target,
        )
        return False

    matched = 0
    changed = 0
    failures: list[str] = []

    for symbol in symbols:
        try:
            before = await raw_client.get_symbol_config(symbol)
            current, _ = _config_values(before, symbol)
            if current == target:
                matched += 1
                continue

            maximum = await _max_supported_leverage(raw_client, symbol)
            if target > maximum:
                raise RuntimeError(
                    f"configured_leverage_exceeds_symbol_max_{maximum}"
                )

            await _validated_ownership(engine, raw_client)
            write_error = None
            try:
                await raw_client.set_leverage(symbol, target)
            except Exception as exc:  # ambiguous config write; readback decides
                write_error = type(exc).__name__

            after = await raw_client.get_symbol_config(symbol)
            confirmed, _ = _config_values(after, symbol)
            if confirmed != target:
                raise RuntimeError(
                    f"leverage_readback_mismatch_{confirmed}_expected_{target}"
                )

            await _validated_ownership(engine, raw_client)
            changed += 1
            log.warning(
                "[BINANCE_LEVERAGE_RECONCILE] symbol=%s result=CONFIRMED "
                "before=%dx after=%dx max_supported=%dx write_error=%s "
                "margin_mode=CROSS execution_effect=ACCOUNT_CONFIG_ONLY",
                symbol,
                current,
                confirmed,
                maximum,
                write_error or "NONE",
            )
        except Exception as exc:
            failures.append(symbol)
            log.critical(
                "[BINANCE_LEVERAGE_RECONCILE] symbol=%s result=BLOCK reason=%s "
                "target=%dx execution_effect=BLOCK_NEW_LIVE_ENTRY",
                symbol,
                str(exc) or type(exc).__name__,
                target,
            )

    try:
        post_clear = await _account_exposure_clear(raw_client)
        await _validated_ownership(engine, raw_client)
    except Exception as exc:
        post_clear = False
        failures.append("POSTCHECK")
        log.critical(
            "[BINANCE_LEVERAGE_RECONCILE] result=BLOCK stage=postcheck reason=%s "
            "execution_effect=BLOCK_NEW_LIVE_ENTRY",
            type(exc).__name__,
        )

    ready = bool(not failures and post_clear)
    engine._binance_leverage_sync_ready = ready
    log.warning(
        "[BINANCE_LEVERAGE_RECONCILE] result=%s target=%dx symbols=%d "
        "already_matching=%d changed=%d failures=%d exposure_clear=%s "
        "final_dispatch_gate=UNCHANGED",
        "PASS" if ready else "BLOCK",
        target,
        len(symbols),
        matched,
        changed,
        len(failures),
        post_clear,
    )
    return ready


def install(TradingEngine, log) -> None:
    """Run leverage reconciliation after core connect, before LIVE preflight."""
    if getattr(TradingEngine, "_binance_leverage_reconcile_installed", False):
        return

    original_connect = TradingEngine._connect

    async def _connect_with_leverage_reconcile(self, *args, **kwargs):
        result = await original_connect(self, *args, **kwargs)
        if getattr(self, "paper_trade", False) or not getattr(self, "connected", False):
            return result
        try:
            await reconcile(self, log)
        except Exception as exc:
            self._binance_leverage_sync_ready = False
            log.critical(
                "[BINANCE_LEVERAGE_RECONCILE] result=BLOCK stage=wrapper reason=%s "
                "execution_effect=BLOCK_NEW_LIVE_ENTRY",
                type(exc).__name__,
            )
        return result

    TradingEngine._connect = _connect_with_leverage_reconcile
    TradingEngine._binance_leverage_reconcile_installed = True
    log.warning(
        "[BINANCE_LEVERAGE_RECONCILE] installed=true target_source=cfg.LEVERAGE "
        "margin_mode_mutation=false order_mutation=false final_dispatch_gate=UNCHANGED"
    )
