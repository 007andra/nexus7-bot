"""Restore exact opening-order lineage on positions recovered after restart.

This module is accounting-only.  The authoritative ownership decision remains
``restart_ownership_recovery`` / ``pilot_external_position_guard``.  After that
existing guard has proved a position belongs to BGX, this wrapper copies the
already-proven opening ``order_id`` into ``Position._forensic_lineage`` so a
later close can be reconciled to KuCoin by exact opening-order lineage.

No order, position size, leverage, stop, target or entry authorization is
changed here.  Missing or conflicting evidence is never invented/overwritten.
"""
from __future__ import annotations


def _restore_position_lineage(engine, symbol: str, log) -> bool:
    recovered = set(getattr(engine, "_recovered_position_symbols", set()) or set())
    if symbol not in recovered:
        return False

    proofs = getattr(engine, "_restart_ownership_proofs", {}) or {}
    proof = proofs.get(symbol) if isinstance(proofs, dict) else None
    position = (getattr(engine, "positions", {}) or {}).get(symbol)
    if position is None or proof is None or not bool(getattr(proof, "recovered", False)):
        log.critical(
            "[RESTART_LINEAGE_RECOVERY] symbol=%s result=NOT_RESTORED "
            "reason=missing_recovered_position_or_proof "
            "execution_effect=ACCOUNTING_METADATA_ONLY",
            symbol,
        )
        return False

    opening_order_id = str(getattr(proof, "order_id", "") or "").strip()
    if not opening_order_id:
        log.critical(
            "[RESTART_LINEAGE_RECOVERY] symbol=%s result=NOT_RESTORED "
            "reason=missing_exact_opening_order_id invented=false "
            "execution_effect=ACCOUNTING_METADATA_ONLY",
            symbol,
        )
        return False

    raw_lineage = getattr(position, "_forensic_lineage", None)
    lineage = dict(raw_lineage) if isinstance(raw_lineage, dict) else {}
    existing = str(lineage.get("opening_order_id", "") or "").strip()
    if existing and existing != opening_order_id:
        log.critical(
            "[RESTART_LINEAGE_RECOVERY] symbol=%s result=NOT_RESTORED "
            "reason=opening_order_id_conflict invented=false overwritten=false "
            "execution_effect=ACCOUNTING_METADATA_ONLY",
            symbol,
        )
        return False

    lineage["opening_order_id"] = opening_order_id
    position._forensic_lineage = lineage
    log.warning(
        "[RESTART_LINEAGE_RECOVERY] symbol=%s result=RESTORED "
        "opening_order_id_present=true evidence=exact_durable_exchange_proof "
        "invented=false execution_effect=ACCOUNTING_METADATA_ONLY",
        symbol,
    )
    return True


async def restore_exchange_geometry(engine, symbol: str, log) -> bool:
    """Q-01 (Binance port): a recovered position's local SL/TP come from the
    protection ACTIVE on the exchange, never from the startup ATR/liquidation
    estimate. Without a provable stop AND target, R-based exits and trailing
    stay disabled for this position (``_geometry_unproven``); exchange
    protection is untouched."""
    from bot.timeout_adoption import active_protection, most_protective, read_orders
    position = (getattr(engine, "positions", {}) or {}).get(symbol)
    if position is None:
        return False
    position._geometry_unproven = True
    orders = await read_orders(engine.client, symbol)
    if orders is None:
        log.critical("[RESTART_GEOMETRY] symbol=%s result=UNPROVEN reason=stop_orders_unreadable "
                     "r_exits=DISABLED trailing=DISABLED protection=UNCHANGED", symbol)
        return False
    stops, tps = active_protection(orders, position.direction)
    sl = most_protective(stops, position.direction)
    tp = (min(tps) if position.direction == "LONG" else max(tps)) if tps else None
    if sl is None or tp is None:
        log.critical("[RESTART_GEOMETRY] symbol=%s result=UNPROVEN active_sl=%s active_tp=%s "
                     "startup_estimate_used=false r_exits=DISABLED trailing=DISABLED", symbol, sl, tp)
        if sl is not None:
            position.sl = position.trailing_sl = sl
        return False
    position.sl = position.trailing_sl = sl
    position.tp = tp
    position._geometry_unproven = False
    log.warning("[RESTART_GEOMETRY] symbol=%s result=RESTORED source=EXCHANGE_ACTIVE_PROTECTION "
                "sl=%s tp=%s startup_estimate_used=false", symbol, sl, tp)
    return True


def install(TradingEngine, log) -> None:
    if getattr(TradingEngine, "_restart_opening_order_lineage_patched", False):
        return

    original_load = getattr(TradingEngine, "_load_existing_positions", None)
    if original_load is None:
        raise RuntimeError("TradingEngine._load_existing_positions unavailable")

    async def _load_existing_with_lineage(self, *args, **kwargs):
        result = await original_load(self, *args, **kwargs)
        recovered = set(getattr(self, "_recovered_position_symbols", set()) or set())
        for symbol in sorted(recovered):
            _restore_position_lineage(self, symbol, log)
            if not getattr(self, "paper_trade", True):
                await restore_exchange_geometry(self, symbol, log)
        return result

    TradingEngine._load_existing_positions = _load_existing_with_lineage
    TradingEngine._restart_opening_order_lineage_patched = True
    log.warning(
        "[RESTART_LINEAGE_RECOVERY] installed=true source=exact_restart_ownership_proof "
        "opening_order_id_rehydrated=true missing_or_conflicting_evidence_invented=false "
        "leverage_unchanged=true sizing_unchanged=true close_logic_unchanged=true "
        "execution_effect=ACCOUNTING_METADATA_ONLY"
    )
