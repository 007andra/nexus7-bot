"""Durable LIVE partial: submission is one-shot; stop repair is retryable."""
import json
import math
from decimal import Decimal, ROUND_DOWN

from bot.confirmed_rr_exit import durable_identity, _persist
from bot.conditional_stop_protection import _instrument_info, _to_base_size
from bot.logger import log
from bot.quantity import quantity_rules, validate_base_quantity


async def check(engine):
    engine._pending_partial_symbols = set()
    for symbol, pos in list(engine.positions.items()):
        try:
            key, idem, raw = await durable_identity(symbol, pos, 'partial')
            state = json.loads(raw) if raw is not None else None
            if state is not None and (not isinstance(state, dict) or state.get('idem') != idem):
                raise ValueError('invalid partial intent')
            if state is None:
                if pos.tp1_hit:
                    continue
                entry, stop, price = map(float, (pos.entry, pos.sl, pos.current_price))
                if not all(math.isfinite(x) and x > 0 for x in (entry, stop, price)):
                    raise ValueError('invalid partial prices')
                if pos.direction not in ('LONG', 'SHORT'):
                    raise ValueError('invalid partial direction')
                profit = price-entry if pos.direction == 'LONG' else entry-price
                if abs(entry-stop) <= 0 or profit < abs(entry-stop) + entry*0.0003:
                    continue
                info = engine.instruments[symbol]
                multiplier, lot, _, _ = quantity_rules(info)
                step = multiplier * lot
                if not step.is_finite() or step <= 0:
                    raise ValueError('invalid partial step')
                qty = float((Decimal(str(pos.qty_original))*Decimal('0.5') / step).to_integral_value(rounding=ROUND_DOWN)*step)
                if not math.isfinite(qty) or qty <= 0 or qty >= float(pos.qty):
                    continue
                validate_base_quantity(qty, info, price)
                side = 'Sell' if pos.direction == 'LONG' else 'Buy'
                state = dict(idem=idem, client_oid=engine.client.build_client_oid(symbol, side, qty, idem), order_id='', qty=qty, filled=False, protected=False)
                await _persist(key, state)
                engine._pending_partial_symbols.add(symbol)
                result = await engine.client.place_order(symbol=symbol, side=side, qty=qty, sl=0, tp=0,
                    instruments=engine.instruments, reduce_only=True, idem_key=idem, single_submission=True)
                state['order_id'] = str(result.get('orderId') or '') if isinstance(result, dict) else ''
                await _persist(key, state)
            engine._pending_partial_symbols.add(symbol)
            if not state.get('filled'):
                if not state.get('order_id'):
                    recovered = await engine.client.get_order_by_client_oid(state['client_oid'])
                    state['order_id'] = str(recovered.get('orderId') or '') if isinstance(recovered, dict) else ''
                    await _persist(key, state)
                if not state['order_id']:
                    continue
                fill = await engine.client.wait_for_fill(state['order_id'], timeout_s=8.0)
                if not isinstance(fill, dict) or fill.get('filled') is not True:
                    continue
                state['filled'] = True
                # Persist the irreversible action BEFORE attempting stop repair.
                await _persist(key, state)
            rows = await engine.client.get_positions()
            if not isinstance(rows, list) or any(not isinstance(p, dict) for p in rows):
                raise ValueError('invalid exchange positions')
            matches = [p for p in rows if p.get('symbol') == symbol]
            if len(matches) > 1:
                raise ValueError('ambiguous position')
            remaining = 0.0
            if matches:
                row = matches[0]
                unit = row.get('sizeUnit')
                if unit not in ('BASE_ASSET', 'CONTRACTS'):
                    raise ValueError('unknown position units')
                raw_size = row['size']
                if isinstance(raw_size, bool) or not math.isfinite(float(raw_size)):
                    raise ValueError('invalid exchange quantity')
                raw_size = abs(float(raw_size))
                remaining = _to_base_size(raw_size, unit, _instrument_info(engine.client, symbol))
                if not math.isfinite(remaining) or (raw_size > 0 and remaining <= 0):
                    raise ValueError('invalid residual')
            pos.tp1_hit = True
            if remaining == 0:
                await engine._sync_positions()
                engine._pending_partial_symbols.discard(symbol)
                continue
            pos.qty = remaining
            if state.get('protected') is not True:
                engine._unprotected_symbols.add(symbol)
                if await engine.client.set_sl(symbol, pos.entry) is not True:
                    continue
                state['protected'] = True
                await _persist(key, state)
                pos.sl = pos.entry
                pos.trailing_sl = pos.entry
                engine._unprotected_symbols.discard(symbol)
            engine._pending_partial_symbols.discard(symbol)
        except Exception as exc:
            engine._pending_partial_symbols.add(symbol)
            log.error('[PARTIAL_EXIT_PENDING] symbol=%s error=%s resubmit=false', symbol, type(exc).__name__)
