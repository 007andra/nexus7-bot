"""Persist operational daily PnL and reconcile estimates with exchange truth.

The execution engine may temporarily record a conservative local estimate when a
position disappears before exchange fill/history indexing has caught up. That
estimate remains valid for risk decisions until authoritative evidence arrives.
Once exchange fills + durable BGX lineage are reconciled, an idempotent
adjustment converts the operational ledger from the estimate to the
exchange-confirmed realized PnL without double counting the trade.
"""
import asyncio
import hashlib
import json
import math
import os
from datetime import datetime, timezone

from bot import database as db
from bot.logger import log
from bot import daily_pnl_storage

_ESTIMATED_SOURCE = 'ESTIMATED_LOCAL_MARK_AND_FEE_RATE'
_CONFIRMED_ADJUSTMENT_SOURCE = 'KUCOIN_RECONCILIATION_ADJUSTMENT'
_MATCH_WINDOW_SECONDS = 120.0


def _reconciliation_source(receipt=None):
    exchange = 'KUCOIN'
    if isinstance(receipt, dict):
        candidate = str(receipt.get('exchange') or '').strip().upper()
        if candidate in {'KUCOIN', 'BINANCE'}:
            exchange = candidate
    return exchange, f'{exchange}_RECONCILIATION_ADJUSTMENT'


def utc_day(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).date().isoformat()


def _ledger_key(day):
    return daily_pnl_storage.ledger_key(day)


def event(trade):
    pnl = float(trade.pnl)
    if not math.isfinite(pnl):
        raise ValueError('nonfinite realized PnL')
    identity = [trade.symbol, trade.direction, trade.opened_at.isoformat(),
                trade.closed_at.isoformat(), str(trade.qty), str(trade.entry),
                str(trade.exit_price)]
    token = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    value = {
        'pnl': pnl,
        'source': getattr(trade, 'accounting_source', 'ENGINE_OPERATIONAL_NET_PNL'),
        'closed_at': trade.closed_at.isoformat(),
        'symbol': str(getattr(trade, 'symbol', '') or ''),
        'opened_at': trade.opened_at.isoformat(),
    }
    opening_order_id = str(getattr(trade, 'opening_order_id', '') or '')
    if opening_order_id:
        value['opening_order_id'] = opening_order_id
    return token, value


def _normalized_symbol(value):
    raw = str(value or '').upper()
    if raw == 'XBTUSDTM':
        return 'BTCUSDT'
    if raw.endswith('USDTM'):
        return raw[:-1]
    return raw


def _iter_estimates(rows, row):
    """Yield same-symbol finite estimated events with parsed close timestamps."""
    expected_symbol = _normalized_symbol(row.get('symbol')) if isinstance(row, dict) else ''
    for token, value in (rows or {}).items():
        if not isinstance(token, str) or not isinstance(value, dict):
            continue
        if value.get('source') != _ESTIMATED_SOURCE:
            continue
        value_symbol = _normalized_symbol(value.get('symbol'))
        if value_symbol and expected_symbol and value_symbol != expected_symbol:
            continue
        try:
            stamp = datetime.fromisoformat(str(value['closed_at']))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            estimate = float(value['pnl'])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(estimate):
            continue
        yield token, estimate, str(value.get('opening_order_id') or ''), stamp.astimezone(timezone.utc)


def _candidate_estimates(rows, row):
    """Legacy fallback candidates constrained by symbol and close-time proximity."""
    try:
        close_ms = int(row['closeTime'])
    except (KeyError, TypeError, ValueError, OverflowError):
        return []
    if close_ms <= 0:
        return []
    confirmed_closed = datetime.fromtimestamp(close_ms / 1000.0, tz=timezone.utc)
    candidates = []
    for token, estimate, opening_order_id, stamp in _iter_estimates(rows, row):
        delta = abs((stamp - confirmed_closed).total_seconds())
        if delta <= _MATCH_WINDOW_SECONDS:
            candidates.append((token, estimate, opening_order_id))
    return candidates


def _lineage_candidates(rows, row, opening_order_id):
    """Return exact same-symbol opening-order matches regardless of close-time drift.

    Exchange history indexing, restart recovery, websocket delay and direct-close
    accounting can move the local estimate timestamp outside the legacy 120-second
    proximity window. Once both sides carry the same durable opening order ID, that
    identity is stronger than time proximity and must be authoritative.
    """
    target = str(opening_order_id or '')
    if not target:
        return []
    matches = []
    for token, estimate, candidate_opening_id, _stamp in _iter_estimates(rows, row):
        if candidate_opening_id == target:
            matches.append((token, estimate, candidate_opening_id))
    return matches


def _confirmed_adjustment(rows, row, receipt=None):
    """Return one idempotent estimate->confirmed adjustment.

    Exact opening-order lineage is authoritative and intentionally independent of
    timestamp proximity. Legacy events without lineage retain the conservative
    symbol+time fallback and require exactly one candidate.
    """
    if not isinstance(rows, dict) or not isinstance(row, dict):
        return None
    close_id = str(row.get('closeId') or '')
    if not close_id:
        return None
    try:
        confirmed = float(row['pnl'])
        close_ms = int(row['closeTime'])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(confirmed) or close_ms <= 0:
        return None

    opening_ids = []
    if isinstance(receipt, dict):
        raw_ids = receipt.get('opening_order_ids')
        if isinstance(raw_ids, list):
            opening_ids = [str(x or '') for x in raw_ids if str(x or '')]

    selected = []
    if len(opening_ids) == 1:
        selected = _lineage_candidates(rows, row, opening_ids[0])
        if len(selected) != 1:
            # A contradictory or ambiguous modern lineage event must never fall
            # back to fuzzy matching. Exact lineage is either unique or unproven.
            if selected or any(
                candidate_opening_id
                for _, _, candidate_opening_id, _ in _iter_estimates(rows, row)
            ):
                return None

    if not selected:
        candidates = _candidate_estimates(rows, row)
        # Legacy estimates without opening-order lineage may still use the old
        # unique symbol+time fallback. Modern lineage never degrades to time.
        if len(opening_ids) == 1 and any(item[2] for item in candidates):
            return None
        if len(candidates) != 1:
            return None
        selected = candidates

    estimate_token, estimated, matched_opening_order_id = selected[0]
    confirmed_closed = datetime.fromtimestamp(close_ms / 1000.0, tz=timezone.utc)
    expected_symbol = _normalized_symbol(row.get('symbol'))
    adjustment = confirmed - estimated
    exchange, adjustment_source = _reconciliation_source(receipt)
    adjustment_token = hashlib.sha256(
        (exchange.lower() + '-confirmed:' + close_id).encode()
    ).hexdigest()
    value = {
        'pnl': adjustment,
        'source': adjustment_source,
        'closed_at': confirmed_closed.isoformat(),
        'symbol': expected_symbol,
        'close_id': close_id,
        'estimated_event': estimate_token,
        'estimated_pnl': estimated,
        'confirmed_pnl': confirmed,
    }
    if matched_opening_order_id:
        value['opening_order_id'] = matched_opening_order_id
    return adjustment_token, value


def realized(stats, now=None):
    day = utc_day(now or datetime.now(timezone.utc))
    rows = dict(getattr(stats, '_durable_daily_pnl', {}).get(day, {}))
    for trade in stats.trades:
        if utc_day(trade.closed_at) == day:
            key, value = event(trade)
            rows[key] = value
    return math.fsum(float(value['pnl']) for value in rows.values())


def evidence_breakdown(stats, now=None):
    day = utc_day(now or datetime.now(timezone.utc))
    rows = dict(getattr(stats, '_durable_daily_pnl', {}).get(day, {}))
    estimated = adjustments = other = 0.0
    estimated_events = confirmed_adjustments = 0
    for value in rows.values():
        if not isinstance(value, dict):
            continue
        pnl = float(value.get('pnl', 0.0) or 0.0)
        source = value.get('source')
        if source == _ESTIMATED_SOURCE:
            estimated += pnl
            estimated_events += 1
        elif (
            source == _CONFIRMED_ADJUSTMENT_SOURCE
            or (
                isinstance(source, str)
                and source.endswith('_RECONCILIATION_ADJUSTMENT')
            )
        ):
            adjustments += pnl
            confirmed_adjustments += 1
        else:
            other += pnl
    return {
        'risk_effective_pnl': math.fsum((estimated, adjustments, other)),
        'estimated_component': estimated,
        'confirmation_adjustment': adjustments,
        'other_component': other,
        'estimated_events': estimated_events,
        'confirmed_adjustments': confirmed_adjustments,
    }


async def reconcile_confirmed_exchange(engine, row, receipt):
    if getattr(engine, 'paper_trade', False) or getattr(engine, '_validation_safety_lock_active', False):
        return False
    if not isinstance(receipt, dict) or not isinstance(row, dict):
        return False
    if not (receipt.get('ownership') == 'BGX_ORDER_IDS'
            and receipt.get('fills_reconciled') is True
            and receipt.get('lineage_reconciled') is True):
        return False

    try:
        close_ms = int(row['closeTime'])
        instant = datetime.fromtimestamp(close_ms / 1000.0, tz=timezone.utc)
        day = utc_day(instant)
        if db.configured_postgres_unavailable():
            raise db.PersistenceError('configured PostgreSQL unavailable')
        if os.environ.get('RAILWAY_SERVICE_ID') and not db._is_pg and db.SQLITE_PATH.startswith('/tmp/'):
            raise db.PersistenceError('ephemeral daily PnL storage')
        if not hasattr(engine, '_daily_pnl_lock'):
            engine._daily_pnl_lock = asyncio.Lock()
        async with engine._daily_pnl_lock:
            key = _ledger_key(day)
            raw, state = await daily_pnl_storage.load(day)
            if state is None:
                log.warning('[DURABLE_DAILY_PNL_RECONCILE] close_id=%s result=NO_LEDGER adjustment=NONE', row.get('closeId', 'NA'))
                return False
            if state.get('version') != 1 or state.get('day') != day or not isinstance(state.get('events'), dict):
                raise ValueError('invalid daily PnL ledger')
            rows = state['events']
            match = _confirmed_adjustment(rows, row, receipt)
            if match is None:
                candidates = _candidate_estimates(rows, row)
                opening_ids = receipt.get('opening_order_ids') if isinstance(receipt, dict) else None
                lineage_count = 0
                if isinstance(opening_ids, list) and len(opening_ids) == 1:
                    lineage_count = len(_lineage_candidates(rows, row, opening_ids[0]))
                result = 'NO_MATCHING_ESTIMATE' if not candidates and not lineage_count else 'ESTIMATE_MATCH_UNCONFIRMED'
                log.warning(
                    '[DURABLE_DAILY_PNL_RECONCILE] symbol=%s close_id=%s result=%s '
                    'candidate_count=%s lineage_candidate_count=%s adjustment=NONE risk_policy_unchanged=true',
                    row.get('symbol', 'NA'), row.get('closeId', 'NA'), result,
                    len(candidates), lineage_count,
                )
                return False
            token, value = match
            previous = rows.get(token)
            if previous is not None and previous != value:
                raise ValueError('conflicting confirmed PnL adjustment')
            rows[token] = value
            encoded = json.dumps(state, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if encoded != raw:
                if await db.save_key_value(key, encoded, strict=True) is not True:
                    raise db.PersistenceError('confirmed PnL adjustment write unconfirmed')
            engine.stats._durable_daily_pnl = {day: dict(rows)}
            engine._daily_pnl_ok = True
            total = math.fsum(float(v['pnl']) for v in rows.values())
            exchange, _ = _reconciliation_source(receipt)
            log.warning(
                '[DURABLE_DAILY_PNL_RECONCILED] symbol=%s close_id=%s estimated_pnl=%s '
                'confirmed_exchange_pnl=%s exchange=%s adjustment=%s risk_effective_pnl=%s '
                'match_basis=%s fills_confirmed=true durable=true entry_policy_unchanged=true',
                row.get('symbol', 'NA'), row.get('closeId', 'NA'), value['estimated_pnl'],
                value['confirmed_pnl'], exchange, value['pnl'], total,
                'OPENING_ORDER_ID' if value.get('opening_order_id') else 'SYMBOL_TIME_UNIQUE',
            )
            return True
    except Exception as exc:
        engine._daily_pnl_ok = False
        log.error(
            '[DURABLE_DAILY_PNL_RECONCILE] symbol=%s close_id=%s result=UNCONFIRMED '
            'adjustment=NONE entries_fail_closed=true error=%s',
            row.get('symbol', 'NA'), row.get('closeId', 'NA'), type(exc).__name__,
        )
        return False


async def checkpoint(engine, extra=None, now=None):
    if getattr(engine, 'paper_trade', False) or getattr(engine, '_validation_safety_lock_active', False):
        return True
    if not getattr(engine, '_durable_state_enforced', False):
        return True
    if not hasattr(engine, '_daily_pnl_lock'):
        engine._daily_pnl_lock = asyncio.Lock()
    async with engine._daily_pnl_lock:
        try:
            if db.configured_postgres_unavailable():
                raise db.PersistenceError('configured PostgreSQL unavailable')
            if os.environ.get('RAILWAY_SERVICE_ID') and not db._is_pg and db.SQLITE_PATH.startswith('/tmp/'):
                raise db.PersistenceError('ephemeral daily PnL storage')
            day = utc_day(now or datetime.now(timezone.utc))
            key = _ledger_key(day)
            raw, state = await daily_pnl_storage.load(day)
            state = state if state is not None else {
                'version': 1, 'day': day, 'events': {},
                'coverage_started_at': (now or datetime.now(timezone.utc)).isoformat()}
            if state.get('version') != 1 or state.get('day') != day or not isinstance(state.get('events'), dict):
                raise ValueError('invalid daily PnL ledger')
            rows = state['events']
            for token, value in rows.items():
                if not isinstance(token, str) or len(token) != 64 or not isinstance(value, dict):
                    raise ValueError('invalid daily PnL event')
                if not math.isfinite(float(value['pnl'])) or utc_day(datetime.fromisoformat(value['closed_at'])) != day:
                    raise ValueError('invalid daily PnL value')
            for trade in list(engine.stats.trades) + ([extra] if extra is not None else []):
                if utc_day(trade.closed_at) != day:
                    continue
                token, value = event(trade)
                if token in rows and rows[token] != value:
                    old = rows[token]
                    comparable = {k: value[k] for k in ('pnl', 'source', 'closed_at')}
                    if any(old.get(k) != v for k, v in comparable.items()):
                        raise ValueError('conflicting daily PnL event')
                    merged = dict(old)
                    merged.update({k: v for k, v in value.items() if k not in old})
                    rows[token] = merged
                else:
                    rows[token] = value
            encoded = json.dumps(state, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if encoded != raw:
                if await db.save_key_value(key, encoded, strict=True) is not True:
                    raise db.PersistenceError('daily PnL write unconfirmed')
            engine.stats._durable_daily_pnl = {day: dict(rows)}
            engine._daily_pnl_ok = True
            total = math.fsum(float(v['pnl']) for v in rows.values())
            estimates = sum(1 for v in rows.values() if v.get('source') == _ESTIMATED_SOURCE)
            adjustments = sum(1 for v in rows.values() if v.get('source') == _CONFIRMED_ADJUSTMENT_SOURCE)
            fingerprint = (day, len(rows), total, estimates, adjustments)
            if getattr(engine, '_daily_pnl_logged', None) != fingerprint:
                log.info(
                    '[DURABLE_DAILY_PNL] day=%s events=%s risk_effective_pnl=%s estimated_events=%s '
                    'confirmed_adjustments=%s durable=true source=ENGINE_OPERATIONAL_WITH_EVIDENCE '
                    'history_backfill=false coverage_started_at=%s',
                    *fingerprint, state.get('coverage_started_at', 'UNCONFIRMED'))
                engine._daily_pnl_logged = fingerprint
            return True
        except Exception as exc:
            engine._daily_pnl_ok = False
            log.error('[DURABLE_DAILY_PNL] state=UNCONFIRMED entries_blocked=true error=%s', type(exc).__name__)
            return False
