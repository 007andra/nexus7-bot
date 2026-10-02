"""LIVE 2R exits: durable at-most-once submission; accounting owned by sync.

An ambiguous intent is recovered by clientOid, never blindly resubmitted.
Pending/partial fills retain the local position and its exchange protection.
"""
import hashlib
import json
import math

from bot import database as db
from bot.logger import log


def identity(symbol, position):
    lineage = getattr(position, '_forensic_lineage', {}) or {}
    opening = lineage.get('opening_order_id') or lineage.get('order_id')
    if not opening:
        raise ValueError('exact opening order identity required for LIVE RR exit')
    # NOVO-03: stable financial scope instead of Railway IDs.
    from bot.financial_namespace import stable_scope
    token = hashlib.sha256(json.dumps([stable_scope(), symbol, str(opening), 'RR_DOUBLE']).encode()).hexdigest()
    return 'rr_exit_v1:' + token, 'rr-' + token


def _legacy_identity(symbol, position):
    lineage = getattr(position, '_forensic_lineage', {}) or {}
    opening = lineage.get('opening_order_id') or lineage.get('order_id')
    from bot.financial_namespace import legacy_railway_values
    token = hashlib.sha256(json.dumps(legacy_railway_values() + [symbol, str(opening), 'RR_DOUBLE']).encode()).hexdigest()
    return 'rr_exit_v1:' + token, 'rr-' + token


async def durable_identity(symbol, position, kind='rr'):
    """(key, idem, raw) — the stable identity, or the previous release's
    identity when ONLY a legacy record exists (an in-flight exit keeps its
    exchange idempotency key across the namespace migration)."""
    key, idem = identity(symbol, position)
    legacy_key, legacy_idem = _legacy_identity(symbol, position)
    if kind == 'partial':
        key, idem = key.replace('rr_exit_v1:', 'partial_exit_v1:'), idem.replace('rr-', 'partial-', 1)
        legacy_key = legacy_key.replace('rr_exit_v1:', 'partial_exit_v1:')
        legacy_idem = legacy_idem.replace('rr-', 'partial-', 1)
    raw = await db.load_key_value(key, strict=True)
    if raw is None and legacy_key != key:
        legacy_raw = await db.load_key_value(legacy_key, strict=True)
        if legacy_raw is not None:
            log.warning('[FINANCIAL_NAMESPACE] key_family=%s_exit source=LEGACY_SCOPE identity=LEGACY', kind)
            return legacy_key, legacy_idem, legacy_raw
    return key, idem, raw


async def _persist(key, state):
    if await db.save_key_value(key, json.dumps(state, sort_keys=True, allow_nan=False), strict=True) is not True:
        raise db.PersistenceError('RR exit persistence unconfirmed')


async def check(engine):
    for symbol, position in list(engine.positions.items()):
        try:
            if symbol in getattr(engine, '_pending_partial_symbols', set()):
                continue
            entry, stop, price = map(float, (position.entry, position.sl, position.current_price or position.entry))
            if not all(math.isfinite(v) and v > 0 for v in (entry, stop, price)):
                continue
            if position.direction not in ('LONG', 'SHORT'):
                raise ValueError('invalid RR direction')
            key, idem, raw = await durable_identity(symbol, position, 'rr')
            state = json.loads(raw) if raw is not None else None
            if state is not None and (not isinstance(state, dict) or state.get('idem') != idem or not state.get('client_oid')):
                raise ValueError('invalid durable RR exit')
            if state is None:
                distance = abs(entry - stop)
                profit = price - entry if position.direction == 'LONG' else entry - price
                if distance <= 0 or profit < 2 * distance:
                    continue
                qty = float(position.qty)
                if not math.isfinite(qty) or qty <= 0:
                    raise ValueError('invalid RR quantity')
                side = 'Sell' if position.direction == 'LONG' else 'Buy'
                oid = engine.client.build_client_oid(symbol, side, qty, idem)
                state = dict(idem=idem, client_oid=oid, order_id='', qty=qty)
                # Write before network: crash/timeout can only recover, not retry.
                await _persist(key, state)
                result = await engine.client.place_order(
                    symbol=symbol, side=side, qty=qty, sl=0, tp=0,
                    instruments=engine.instruments, reduce_only=True,
                    idem_key=idem, single_submission=True,
                )
                state['order_id'] = str(result.get('orderId') or '') if isinstance(result, dict) else ''
                await _persist(key, state)
            if not state['order_id']:
                recovered = await engine.client.get_order_by_client_oid(state['client_oid'])
                state['order_id'] = str(recovered.get('orderId') or '') if isinstance(recovered, dict) else ''
                if state['order_id']:
                    await _persist(key, state)
            if not state['order_id']:
                log.error('[RR_EXIT_PENDING] symbol=%s reason=ambiguous_submission resubmit=false local_position_retained=true', symbol)
                continue
            fill = await engine.client.wait_for_fill(state['order_id'], timeout_s=8.0)
            if not isinstance(fill, dict) or fill.get('filled') is not True:
                log.warning('[RR_EXIT_PENDING] symbol=%s order_id=%s fill_unconfirmed=true local_position_retained=true', symbol, state['order_id'])
                continue
            positions = await engine.client.get_positions()
            if not isinstance(positions, list):
                raise ValueError('exchange position read unconfirmed')
            sizes = [float(p.get('size', p.get('currentQty'))) for p in positions if p.get('symbol') == symbol]
            if any(not math.isfinite(v) for v in sizes):
                raise ValueError('invalid exchange position quantity')
            if any(abs(v) > 0 for v in sizes):
                log.warning('[RR_EXIT_PENDING] symbol=%s residual_position=true local_position_retained=true', symbol)
                continue
            # One accounting owner handles exchange-flat positions and attaches
            # opening lineage. Do not manufacture a trade from the trigger mark.
            await engine._sync_positions()
            log.info('[RR_EXIT_CONFIRMED] symbol=%s order_id=%s fill_confirmed=true exchange_flat=true accounting_owner=POSITION_SYNC', symbol, state['order_id'])
        except Exception as exc:
            log.error('[RR_EXIT_UNCONFIRMED] symbol=%s error=%s local_position_retained=true', symbol, type(exc).__name__)
