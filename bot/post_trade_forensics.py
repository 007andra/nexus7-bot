"""Passive post-trade forensic telemetry and durable entry lineage.

This module measures trade-path quality without changing signals, sizing,
thresholds, protection, exchange routing, or close decisions. Entry context is
persisted only after a confirmed local position exists and is keyed by the
exchange opening orderId, preventing same-symbol trades from sharing lineage.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime


def _num(value, default=0.0):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _net_breakeven(entry: float, direction: str, fee_rate: float) -> float:
    entry = _num(entry)
    fee_rate = max(0.0, _num(fee_rate))
    if entry <= 0 or fee_rate >= 1:
        return 0.0
    if str(direction).upper() == "LONG":
        return entry * (1.0 + fee_rate) / (1.0 - fee_rate)
    return entry * (1.0 - fee_rate) / (1.0 + fee_rate)


def _minutes(opened_at) -> float:
    if not isinstance(opened_at, datetime):
        return 0.0
    try:
        return max(0.0, (datetime.utcnow() - opened_at).total_seconds() / 60.0)
    except Exception:
        return 0.0


def _roe_pct(pnl: float, entry: float, qty: float, leverage: float) -> float:
    notional = abs(_num(entry) * _num(qty))
    lev = max(1.0, _num(leverage, 1.0))
    margin = notional / lev
    return (_num(pnl) / margin * 100.0) if margin > 0 else 0.0


def _decision_snapshot(decision):
    if decision is None:
        return {}
    if isinstance(decision, dict):
        snapshot = dict(decision)
        allowed = snapshot.get("execution_allowed")
        # Lineage records the authorization outcome, not LONG/SHORT/WAIT.
        # The directional NEXUS decision remains available in the source
        # decision object; execution lineage must match the gate that opened
        # the exchange order.
        if type(allowed) is bool:
            snapshot["decision"] = "APPROVE" if allowed else "REJECT"
        return snapshot
    allowed = getattr(decision, "execution_allowed", None)
    return {
        "decision": "APPROVE" if allowed is True else "REJECT",
        "execution_allowed": allowed,
        "setup_quality": getattr(decision, "setup_quality", None),
        "confidence": getattr(decision, "confidence", None),
    }


def _lineage(sig, nexus, order_record):
    nexus = _decision_snapshot(nexus)
    order_record = order_record if isinstance(order_record, dict) else {}
    return {
        "version": 2,
        "symbol": str(getattr(sig, "symbol", "")),
        "direction": str(getattr(sig, "direction", "")),
        "entry": _num(getattr(sig, "entry", 0.0)),
        "score": _num(getattr(sig, "score", 0.0)),
        "regime": str(getattr(sig, "regime", "UNKNOWN") or "UNKNOWN"),
        "entry_type": str(getattr(sig, "entry_type", "UNKNOWN") or "UNKNOWN"),
        "nexus": str(nexus.get("decision", nexus.get("action", "UNKNOWN"))),
        "nexus_setup_quality": nexus.get("setup_quality"),
        "nexus_confidence": nexus.get("confidence"),
        "order_id": str(order_record.get("order_id") or ""),
        "client_oid": str(order_record.get("client_oid") or ""),
        "order_created_at_ms": int(_num(order_record.get("created_at", 0.0)) * 1000),
        "captured_at_ms": int(time.time() * 1000),
    }


def _opening_order_for_position(engine, symbol, opened_after_s):
    registry = getattr(engine, "orders", None)
    if registry is None or not hasattr(registry, "snapshot"):
        return None
    try:
        rows = registry.snapshot()
    except Exception:
        return None
    candidates = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol", "")) != str(symbol):
            continue
        if str(row.get("state", "")) != "FILLED":
            continue
        if not row.get("order_id") or not str(row.get("client_oid", "")).startswith("bgx7-"):
            continue
        created = _num(row.get("created_at", 0.0))
        if created + 2.0 < opened_after_s:
            continue
        candidates.append(row)
    if not candidates:
        return None
    return max(candidates, key=lambda row: _num(row.get("created_at", 0.0)))


def _lineage_key(order_id):
    from bot.durable_daily_stop import state_key
    token = hashlib.sha256(str(order_id).encode()).hexdigest()[:32]
    return state_key("trade_lineage_v2") + ":" + token


async def _persist_lineage(payload, log):
    order_id = str((payload or {}).get("order_id") or "")
    if not order_id:
        return False
    try:
        from bot import database as db
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if await db.save_key_value(_lineage_key(order_id), encoded, strict=True) is not True:
            raise db.PersistenceError("lineage persistence unconfirmed")
        log.info(
            "[TRADE_LINEAGE] symbol=%s orderId=%s durable=true nexus=%s regime=%s entry_type=%s execution_effect=NONE",
            payload.get("symbol", "NA"), order_id, payload.get("nexus", "UNKNOWN"),
            payload.get("regime", "UNKNOWN"), payload.get("entry_type", "UNKNOWN"),
        )
        return True
    except Exception as exc:
        log.warning(
            "[TRADE_LINEAGE] symbol=%s orderId=%s durable=false error=%s execution_effect=NONE",
            (payload or {}).get("symbol", "NA"), order_id or "NONE", type(exc).__name__,
        )
        return False


def install(TradingEngine, Position, cfg, fee_rate, log) -> None:
    if getattr(TradingEngine, "_post_trade_forensics_installed", False):
        return

    original_nexus_validate = getattr(TradingEngine, "_nexus_validate", None)
    if original_nexus_validate is not None:
        async def _nexus_validate_with_lineage(self, sig, *args, **kwargs):
            decision = await original_nexus_validate(self, sig, *args, **kwargs)
            cache = getattr(self, "_post_trade_nexus_lineage", None)
            if not isinstance(cache, dict):
                cache = {}
                self._post_trade_nexus_lineage = cache
            cache[str(getattr(sig, "symbol", ""))] = _decision_snapshot(decision)
            return decision
        TradingEngine._nexus_validate = _nexus_validate_with_lineage

    original_open = getattr(TradingEngine, "_open", None)
    if original_open is not None:
        async def _open_with_lineage(self, sig, *args, **kwargs):
            symbol = str(getattr(sig, "symbol", ""))
            started = time.time()
            result = await original_open(self, sig, *args, **kwargs)
            # original_open performs the mandatory NEXUS validation. Read the
            # wrapper cache only afterwards so this entry cannot inherit a
            # previous same-symbol decision.
            nexus = dict((getattr(self, "_post_trade_nexus_lineage", {}) or {}).get(symbol, {}) or {})
            pos = (getattr(self, "positions", {}) or {}).get(symbol)
            if pos is not None:
                order_record = _opening_order_for_position(self, symbol, started)
                if order_record is not None:
                    payload = _lineage(sig, nexus, order_record)
                    pos._forensic_lineage = payload
                    await _persist_lineage(payload, log)
                    if not getattr(self, "paper_trade", True):
                        # Q-01B: durable exit geometry for this exact trade lineage.
                        try:
                            from bot import trade_lifecycle
                            await trade_lifecycle.open_trade(pos, pos.qty, "entry_confirmed")
                        except Exception as exc:
                            log.error("[TRADE_LINEAGE_OPEN_FAILED] symbol=%s error=%s",
                                      symbol, type(exc).__name__)
                        try:
                            from bot import exit_geometry_durability
                            await exit_geometry_durability.persist(pos, "entry_confirmed")
                        except Exception as exc:
                            log.error("[EXIT_GEOMETRY_PERSIST_FAILED] symbol=%s stage=entry error=%s",
                                      symbol, type(exc).__name__)
                else:
                    log.warning(
                        "[TRADE_LINEAGE] symbol=%s durable=false reason=NO_CONFIRMED_OPENING_ORDER execution_effect=NONE", symbol,
                    )
            return result
        TradingEngine._open = _open_with_lineage

    original_update = Position.update_pnl

    def _update_pnl_forensics(self, current_price):
        result = original_update(self, current_price)
        pnl = _num(getattr(self, "pnl", 0.0))
        price = _num(getattr(self, "current_price", current_price))
        entry = _num(getattr(self, "entry", price), price)
        if not hasattr(self, "_forensic_mfe_pnl"):
            self._forensic_mfe_pnl = max(0.0, pnl)
            self._forensic_mae_pnl = min(0.0, pnl)
            self._forensic_best_price = price if pnl > 0 else entry
            self._forensic_worst_price = price if pnl < 0 else entry
        else:
            if pnl > _num(self._forensic_mfe_pnl, 0.0):
                self._forensic_mfe_pnl = pnl
                self._forensic_best_price = price
            if pnl < _num(self._forensic_mae_pnl, 0.0):
                self._forensic_mae_pnl = pnl
                self._forensic_worst_price = price
        return result

    Position.update_pnl = _update_pnl_forensics
    original_sync = TradingEngine._sync_positions

    async def _sync_positions_forensics(self, *args, **kwargs):
        before = dict(getattr(self, "positions", {}) or {})
        stats = getattr(self, "stats", None)
        trades_before = len(getattr(stats, "trades", []) or []) if stats is not None else 0
        result = await original_sync(self, *args, **kwargs)
        after = set((getattr(self, "positions", {}) or {}).keys())
        removed = [sym for sym in before if sym not in after]
        pending = getattr(self, "_pending_lineage_terminal", None)
        if not isinstance(pending, dict):
            pending = {}
            self._pending_lineage_terminal = pending
        if not getattr(self, "paper_trade", True):
            for sym in removed:
                pending[sym] = before[sym]
        if pending:
            # NOVO-02: a lineage becomes terminal only from an authoritative
            # snapshot (raises when unconfirmed) proving the symbol flat/flipped;
            # unconfirmed reads are retried on the next sync, never assumed.
            try:
                from bot import trade_lifecycle
                rows = await self.client.get_positions()
                await trade_lifecycle.terminalize_positions(dict(pending), rows, "exchange_flat_sync")
                pending.clear()
            except Exception as exc:
                log.error("[TRADE_LINEAGE_TERMINAL_DEFERRED] symbols=%s error=%s",
                          ",".join(sorted(pending)), type(exc).__name__)
        new_trades = list((getattr(stats, "trades", []) or [])[trades_before:]) if stats is not None else []

        for sym in removed:
            pos = before[sym]
            trade = next((t for t in reversed(new_trades) if getattr(t, "symbol", None) == sym), None)
            if trade is None:
                continue
            entry = _num(getattr(trade, "entry", getattr(pos, "entry", 0.0)))
            exit_price = _num(getattr(trade, "exit_price", getattr(pos, "current_price", 0.0)))
            qty = _num(getattr(trade, "qty", getattr(pos, "qty", 0.0)))
            pnl_gross = _num(getattr(trade, "pnl_gross", 0.0))
            pnl_net = _num(getattr(trade, "pnl", pnl_gross))
            fees = _num(getattr(trade, "total_fees", 0.0))
            mfe = _num(getattr(pos, "_forensic_mfe_pnl", getattr(pos, "peak_pnl", 0.0)))
            mae = _num(getattr(pos, "_forensic_mae_pnl", min(0.0, getattr(pos, "pnl", 0.0))))
            best_price = _num(getattr(pos, "_forensic_best_price", entry))
            worst_price = _num(getattr(pos, "_forensic_worst_price", entry))
            leverage = _num(getattr(cfg, "LEVERAGE", 1.0), 1.0)
            direction = str(getattr(pos, "direction", "")).upper()
            be_price = _net_breakeven(entry, direction, fee_rate)
            be_reached = best_price >= be_price if direction == "LONG" else best_price <= be_price if direction == "SHORT" else False
            sl = _num(getattr(pos, "sl", 0.0))
            tp = _num(getattr(pos, "tp", 0.0))
            tol = max(abs(entry) * 0.0005, 1e-12)
            inferred_exit = "SL_NEAR" if sl > 0 and abs(exit_price - sl) <= tol else "TP_NEAR" if tp > 0 and abs(exit_price - tp) <= tol else "EXCHANGE_CLOSE_OTHER"
            lineage = getattr(pos, "_forensic_lineage", None)
            if not isinstance(lineage, dict):
                lineage = {
                    "score": _num(getattr(pos, "score", 0.0)),
                    "nexus": "UNKNOWN",
                    "regime": str(getattr(pos, "regime", "UNKNOWN") or "UNKNOWN"),
                    "entry_type": str(getattr(pos, "entry_type", "UNKNOWN") or "UNKNOWN"),
                    "order_id": "",
                }
            opening_order_id = str(lineage.get("order_id") or "")
            if opening_order_id:
                # Accounting-only metadata: the durable daily-PnL ledger can later
                # bind KuCoin's confirmed close to this exact opening lineage.
                trade.opening_order_id = opening_order_id
            capture = (pnl_net / mfe * 100.0) if mfe > 0 else 0.0
            log.warning(
                "[POST_TRADE_FORENSICS] symbol=%s side=%s entry=%.8f exit=%.8f qty=%.8f "
                "accounting_source=ESTIMATED_LOCAL_MARK_AND_FEE_RATE fills_confirmed=false "
                "opening_order_id=%s gross_pnl=%.6f net_pnl=%.6f fees=%.6f duration_min=%.1f "
                "mfe_pnl=%.6f mae_pnl=%.6f mfe_roe_pct=%.2f mae_roe_pct=%.2f "
                "best_price=%.8f worst_price=%.8f net_breakeven=%.8f breakeven_reached=%s "
                "profit_capture_pct=%.2f score=%.1f nexus=%s regime=%s entry_type=%s "
                "exit_inferred=%s sl=%.8f tp=%.8f slippage_bps=NA funding=NA "
                "decision_effect=NONE execution_effect=NONE",
                sym, direction, entry, exit_price, qty, opening_order_id,
                pnl_gross, pnl_net, fees, _minutes(getattr(pos, "opened_at", None)),
                mfe, mae, _roe_pct(mfe, entry, qty, leverage), _roe_pct(mae, entry, qty, leverage),
                best_price, worst_price, be_price, str(bool(be_reached)).lower(), capture,
                _num(lineage.get("score", 0.0)), lineage.get("nexus", "UNKNOWN"),
                lineage.get("regime", "UNKNOWN"), lineage.get("entry_type", "UNKNOWN"),
                inferred_exit, sl, tp,
            )
        return result

    TradingEngine._sync_positions = _sync_positions_forensics
    TradingEngine._post_trade_forensics_installed = True
    log.info(
        "[POST_TRADE_FORENSICS] installed=true telemetry_only=true lineage_durable=true "
        "lineage_key=opening_order_id nexus_source=final_validation thresholds_unchanged=true "
        "leverage_unchanged=true execution_effect=NONE"
    )
