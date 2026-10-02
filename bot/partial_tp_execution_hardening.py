"""LIVE partial-TP execution hardening.

The legacy engine treated an accepted reduce-only order as executed, attempted to
move the stop immediately, and then mutated local quantity/SL even when KuCoin
had not confirmed the fill or the stop move.  In addition, KuCoin order payloads
contained ``closeOrder=true`` together with an explicit size, which is ambiguous
for a partial reduction and was observed in production immediately before the
entire LTC position disappeared.

This overlay makes the partial path evidence-driven:
- sized reduce-only orders never carry ``closeOrder``; ``reduceOnly`` remains;
- a partial order must be REST-confirmed filled before local state changes;
- exchange position size is re-read after the fill and becomes quantity truth;
- if the position is already flat, no bogus break-even stop is attempted;
- a failed break-even move never changes local SL and blocks new entries via the
  existing unprotected-symbol gate until protection is reconciled.
"""
from __future__ import annotations

import asyncio


def install(TradingEngine, KuCoinClient, taker_fee: float, log) -> None:
    if getattr(TradingEngine, "_partial_tp_execution_hardening_installed", False):
        return

    # KuCoin's current Add Order contract models sized reductions with `size`
    # plus `reduceOnly`.  Do not combine an explicit size with `closeOrder`.
    original_post = KuCoinClient._post

    async def _post_sized_reduce_only(self, endpoint, body=None, *args, **kwargs):
        if (
            endpoint == "/api/v1/orders"
            and isinstance(body, dict)
            and body.get("reduceOnly") is True
            and body.get("closeOrder") is True
            and str(body.get("size", "")).strip() not in ("", "0", "0.0")
        ):
            body = dict(body)
            body.pop("closeOrder", None)
            log.info(
                "[REDUCE_ONLY_SIZE_HARDENING] explicit_size=true reduceOnly=true "
                "closeOrder_removed=true execution_semantics=sized_reduction"
            )
        return await original_post(self, endpoint, body, *args, **kwargs)

    KuCoinClient._post = _post_sized_reduce_only

    async def _manage_partial_tp_hardened(self):
        if getattr(self, "paper_trade", True) is False:
            from bot.durable_partial_exit import check
            return await check(self)
        for sym, pos in list(self.positions.items()):
            try:
                if pos.tp1_hit:
                    continue
                cur = pos.current_price
                if not cur or cur <= 0:
                    continue

                from bot.exit_geometry import initial_risk_per_unit
                risk_dist = initial_risk_per_unit(pos)   # Q-01: initial 1R
                if risk_dist is None:
                    continue
                funding_cost = pos.entry * 0.0001 * 3
                tp1_price = (
                    pos.entry + risk_dist + funding_cost
                    if pos.direction == "LONG"
                    else pos.entry - risk_dist - funding_cost
                )
                hit = (
                    (pos.direction == "LONG" and cur >= tp1_price)
                    or (pos.direction == "SHORT" and cur <= tp1_price)
                )
                if not hit:
                    continue

                raw_partial = pos.qty_original * 0.5
                qty_step = 0.001
                if self.instruments:
                    inst = self.instruments.get(sym, {})
                    qty_step = float(inst.get("lotSizeFilter", {}).get("qtyStep", 0.001))
                if qty_step > 0:
                    decimals = len(str(qty_step).rstrip("0").split(".")[-1])
                    partial_qty = round(raw_partial - (raw_partial % qty_step), decimals)
                else:
                    partial_qty = round(raw_partial, 4)
                if partial_qty <= 0 or partial_qty > pos.qty:
                    continue

                close_side = "Sell" if pos.direction == "LONG" else "Buy"
                result = await self.client.place_order(
                    symbol=sym,
                    side=close_side,
                    qty=partial_qty,
                    sl=0,
                    tp=0,
                    instruments=self.instruments,
                    reduce_only=True,
                )
                order_id = result.get("orderId") if isinstance(result, dict) else ""
                if not order_id:
                    log.error(
                        f"[PARTIAL_TP_FAIL_CLOSED] symbol={sym} stage=SUBMIT "
                        "reason=no_order_id local_state_unchanged=true"
                    )
                    continue

                fill = await self.client.wait_for_fill(order_id, timeout_s=8.0)
                if not fill.get("filled"):
                    log.error(
                        f"[PARTIAL_TP_FAIL_CLOSED] symbol={sym} orderId={order_id} "
                        f"stage=FILL_CONFIRM timed_out={fill.get('timed_out', False)} "
                        "local_state_unchanged=true"
                    )
                    # Read-only reconciliation may discover a late fill; do not
                    # submit another partial in this cycle.
                    try:
                        await self._reconcile_exchange_positions(only_symbol=sym)
                    except Exception as exc:
                        log.warning(f"[PARTIAL_TP_RECONCILE] symbol={sym} error={exc}")
                    continue

                # The exchange is exposure authority after a confirmed reduction.
                all_pos = await self.client.get_positions()
                exchange_pos = next(
                    (
                        p for p in all_pos
                        if p.get("symbol") == sym and float(p.get("size", 0) or 0) > 0
                    ),
                    None,
                )

                if exchange_pos is None:
                    # A native TP/another exchange event may have flattened the
                    # remainder concurrently.  _sync_positions will account for
                    # the close; do not manufacture a BE stop or ghost quantity.
                    pos.tp1_hit = True
                    log.info(
                        f"[PARTIAL_TP_FLAT_AFTER_FILL] symbol={sym} orderId={order_id} "
                        "remaining_exchange_position=false be_move_skipped=true "
                        "local_close_deferred_to_sync=true"
                    )
                    continue

                remaining_qty = self._contracts_to_base_qty(
                    sym, float(exchange_pos.get("size", 0) or 0)
                )
                if remaining_qty <= 0:
                    log.critical(
                        f"[PARTIAL_TP_FAIL_CLOSED] symbol={sym} stage=REMAINING_QTY "
                        "reason=non_positive_exchange_qty"
                    )
                    self._unprotected_symbols.add(sym)
                    continue

                pos.tp1_hit = True
                pos.qty = remaining_qty
                # Q-01C: break-even is a floor; a better (trailed) stop is kept.
                from bot.stop_monotonic import (
                    EQUAL_AFTER_ROUNDING, WORSE, current_stop, decide_stop, log_decision,
                )
                current_sl = current_stop(pos)
                be_decision = decide_stop(
                    pos.direction, current_sl, pos.entry,
                    tick_size=(getattr(self, "instruments", None) or {}).get(sym, {}).get("tickSize"),
                    market_price=cur)
                log_decision(sym, pos.direction, current_sl, pos.entry, be_decision,
                             "paper_partial_break_even")
                keep_better = be_decision.reason in (WORSE, EQUAL_AFTER_ROUNDING)
                be_ok = (await self.client.set_sl(sym, pos.entry)
                         if be_decision.replace else keep_better)

                # The partial fill itself is authoritative, so quantity and the
                # one-shot marker advance even if the BE protection update fails.
                # This prevents a second 50% close on the next scan.
                pos.tp1_hit = True
                pos.qty = remaining_qty

                pnl_partial = risk_dist * partial_qty
                fee_p = partial_qty * cur * taker_fee * 2
                pnl_net = pnl_partial - fee_p

                if not be_ok:
                    self._unprotected_symbols.add(sym)
                    log.critical(
                        f"[PARTIAL_TP_PROTECTION_FAIL] symbol={sym} orderId={order_id} "
                        f"remaining_qty={remaining_qty:.8f} requested_be={pos.entry:.8f} "
                        "local_sl_unchanged=true entries_blocked=true"
                    )
                    continue

                self._unprotected_symbols.discard(sym)
                if be_decision.replace:
                    pos.sl = pos.entry
                    pos.trailing_sl = pos.entry
                log.info(
                    f"[PARTIAL_TP_CONFIRMED] symbol={sym} orderId={order_id} "
                    f"closed_qty={partial_qty:.8f} remaining_qty={remaining_qty:.8f} "
                    f"be={pos.entry:.8f} pnl_estimate={pnl_net:+.6f} "
                    "fill_confirmed=true protection_confirmed=true"
                )

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(f"[PARTIAL_TP_FAIL_CLOSED] symbol={sym} exception={exc}")

    TradingEngine._manage_partial_tp = _manage_partial_tp_hardened
    TradingEngine._partial_tp_execution_hardening_installed = True
    log.info(
        "[PARTIAL_TP_EXECUTION_HARDENING] installed=true fill_confirmation=REST "
        "exchange_qty_authoritative=true be_fail_closed=true sized_reduce_only=true"
    )
