"""Persist only evidence-backed LIVE daily stops before the scanner can run.

Version 3 retires the v2 namespace because a bare in-memory ``daily_stopped``
flag could be persisted as ``STOP`` without recording what PnL breached which
limit. A fresh stop requires confirmed durable daily-PnL state plus current
realized+unrealized PnL at or below the configured loss limit.

A proven LIVE daily-stop breach may be bypassed only with the exact-day
``DAILY_STOP_OVERRIDE_UTC_DAY=YYYY-MM-DD`` break-glass control. The historical
``DAILY_STOP_OPERATOR_OVERRIDE=true`` persistent bypass is intentionally
retired: when present it is logged as rejected and cannot authorize entries.

The stop evidence and PnL ledger are never deleted or rewritten. The date-scoped
override applies only to a valid daily-stop breach; persistence/storage integrity
failures remain fail-closed. All unrelated risk and execution protections stay
active.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone

from bot import database as db
from bot import durable_daily_pnl
from bot.logger import log


STATE_VERSION = 3
_STATE_SOURCE = "RUNTIME_DAILY_STOP"
_OVERRIDE_ENV = "DAILY_STOP_OVERRIDE_UTC_DAY"
_PERSISTENT_OVERRIDE_ENV = "DAILY_STOP_OPERATOR_OVERRIDE"


def state_key(day):
    # Deliberately excludes deployment/commit: a deploy is not a new day.
    # v3 retires the evidence-free v2 namespace without weakening future
    # persistence of legitimately triggered daily stops.
    # NOVO-03: stable financial scope (not Railway IDs); legacy keys are read
    # by database.load_key_value during migration.
    from bot.financial_namespace import stable_scope
    return f'daily_stop_v{STATE_VERSION}:' + stable_scope() + ':' + day


def _finite(value, default=None):
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _stop_limit(engine):
    direct = _finite(getattr(engine, 'daily_stop_loss', None))
    if direct is not None and direct > 0:
        return direct
    tracker = getattr(engine, 'daily_tracker', None)
    tracked = _finite(getattr(tracker, 'daily_stop_loss', None)) if tracker is not None else None
    return tracked if tracked is not None and tracked > 0 else None


def _unrealized(engine):
    total = 0.0
    for position in getattr(engine, 'positions', {}).values():
        pnl = _finite(getattr(position, 'pnl', 0.0), 0.0)
        total += pnl
    return total


def _clear_unproven_flag(engine):
    engine.daily_stopped = False
    tracker = getattr(engine, 'daily_tracker', None)
    if tracker is not None:
        tracker.daily_stopped = False


def _persistent_override_enabled():
    """Return whether the retired persistent bypass was explicitly requested."""
    return str(os.environ.get(_PERSISTENT_OVERRIDE_ENV, '') or '').strip().lower() == 'true'


def _operator_override_mode(day):
    # Permanent bypasses are intentionally not authorization. A break-glass
    # override must name the exact UTC day so it expires automatically.
    configured_day = str(os.environ.get(_OVERRIDE_ENV, '') or '').strip()
    if configured_day and configured_day == day:
        return 'date_scoped'
    return None


def _warn_rejected_persistent_override(engine, day):
    if not _persistent_override_enabled():
        return
    marker = f'rejected:{day}'
    if getattr(engine, '_daily_stop_persistent_override_rejected_logged', None) == marker:
        return
    log.critical(
        '[DAILY_STOP_OPERATOR_OVERRIDE] day=%s mode=persistent active=false '
        'entries_blocked=true retired=true reason=persistent_bypass_not_permitted '
        'use=%s evidence_preserved=true pnl_preserved=true leverage_unchanged=true sizing_unchanged=true',
        day, _OVERRIDE_ENV,
    )
    engine._daily_stop_persistent_override_rejected_logged = marker


def _operator_override_active(engine, day, *, state=None, combined=None, stop_limit=None):
    mode = _operator_override_mode(day)
    if mode is None:
        _warn_rejected_persistent_override(engine, day)
        return False

    # Only the daily stop flag is bypassed. The durable evidence and PnL ledger
    # remain intact, and any unrelated storage/integrity failure is handled
    # earlier by entries_blocked() and therefore stays fail-closed.
    _clear_unproven_flag(engine)
    marker = f'{mode}:{day}'
    if getattr(engine, '_daily_stop_override_logged', None) != marker:
        trigger_pnl = state.get('trigger_pnl') if isinstance(state, dict) else combined
        effective_limit = state.get('stop_limit') if isinstance(state, dict) else stop_limit
        log.critical(
            '[DAILY_STOP_OPERATOR_OVERRIDE] day=%s mode=%s active=true entries_blocked=false '
            'trigger_pnl=%s stop_limit=%s evidence_preserved=true pnl_preserved=true '
            'auto_expires_utc=true leverage_unchanged=true sizing_unchanged=true',
            day, mode, trigger_pnl, effective_limit,
        )
        engine._daily_stop_override_logged = marker
    return True


def _decode_state(raw, day):
    if raw is None:
        return None
    try:
        state = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise db.PersistenceError('invalid daily stop json') from exc
    if not isinstance(state, dict):
        raise db.PersistenceError('invalid daily stop object')
    if state.get('version') != STATE_VERSION or state.get('day') != day:
        raise db.PersistenceError('invalid daily stop version/day')
    if state.get('source') != _STATE_SOURCE:
        raise db.PersistenceError('invalid daily stop source')
    trigger_pnl = _finite(state.get('trigger_pnl'))
    stop_limit = _finite(state.get('stop_limit'))
    triggered_at = state.get('triggered_at')
    if trigger_pnl is None or stop_limit is None or stop_limit <= 0:
        raise db.PersistenceError('invalid daily stop evidence')
    if trigger_pnl > -stop_limit + 1e-12:
        raise db.PersistenceError('daily stop evidence does not prove breach')
    try:
        stamp = datetime.fromisoformat(str(triggered_at))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp.astimezone(timezone.utc).date().isoformat() != day:
            raise ValueError('trigger day mismatch')
    except (TypeError, ValueError) as exc:
        raise db.PersistenceError('invalid daily stop timestamp') from exc
    return state


def _encode_state(day, combined_pnl, stop_limit, now):
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return json.dumps({
        'version': STATE_VERSION,
        'day': day,
        'trigger_pnl': float(combined_pnl),
        'stop_limit': float(stop_limit),
        'triggered_at': stamp.astimezone(timezone.utc).isoformat(),
        'source': _STATE_SOURCE,
    }, sort_keys=True, separators=(',', ':'), allow_nan=False)


async def entries_blocked(engine, now=None):
    if getattr(engine, 'paper_trade', False) or getattr(engine, '_validation_safety_lock_active', False):
        return False
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    day = instant.astimezone(timezone.utc).date().isoformat()
    key = state_key(day)
    try:
        if db.configured_postgres_unavailable():
            raise db.PersistenceError('configured PostgreSQL unavailable')
        if os.environ.get('RAILWAY_SERVICE_ID') and not db._is_pg and db.SQLITE_PATH.startswith('/tmp/'):
            raise db.PersistenceError('ephemeral SQLite cannot preserve a deployment stop')

        stored = await db.load_key_value(key, strict=True)
        state = _decode_state(stored, day)
        if state is not None:
            engine.daily_stopped = True
            tracker = getattr(engine, 'daily_tracker', None)
            if tracker is not None:
                tracker.daily_stopped = True
            if getattr(engine, '_daily_stop_restored_key', None) != key:
                log.warning(
                    '[DURABLE_DAILY_STOP] day=%s state=RESTORED entries_blocked=true '
                    'trigger_pnl=%s stop_limit=%s evidence=v3 protection_unchanged=true',
                    day, state['trigger_pnl'], state['stop_limit'])
                engine._daily_stop_restored_key = key
            if _operator_override_active(engine, day, state=state):
                return False
            return True

        if not getattr(engine, 'daily_stopped', False):
            return False

        # A fresh durable STOP cannot be manufactured from a boolean. The
        # durable ledger must already be confirmed for this runtime iteration.
        if getattr(engine, '_daily_pnl_ok', False) is not True:
            log.error(
                '[DURABLE_DAILY_STOP] day=%s state=TRIGGER_UNCONFIRMED '
                'entries_blocked=true reason=daily_pnl_unconfirmed', day)
            return True

        stop_limit = _stop_limit(engine)
        if stop_limit is None:
            log.error(
                '[DURABLE_DAILY_STOP] day=%s state=TRIGGER_UNCONFIRMED '
                'entries_blocked=true reason=invalid_stop_limit', day)
            return True

        stats = getattr(engine, 'stats', None)
        if stats is None:
            log.error(
                '[DURABLE_DAILY_STOP] day=%s state=TRIGGER_UNCONFIRMED '
                'entries_blocked=true reason=missing_stats', day)
            return True

        realized = float(durable_daily_pnl.realized(stats, instant))
        unrealized = float(_unrealized(engine))
        combined = realized + unrealized

        if combined > -stop_limit + 1e-12:
            # The boolean is stale/unproven. Clearing only this flag restores
            # entry eligibility; all other risk/execution protections remain.
            _clear_unproven_flag(engine)
            log.warning(
                '[DURABLE_DAILY_STOP] day=%s state=UNPROVEN_FLAG_CLEARED '
                'entries_blocked=false realized=%s unrealized=%s combined=%s '
                'stop_limit=%s protection_unchanged=true',
                day, realized, unrealized, combined, stop_limit)
            return False

        encoded = _encode_state(day, combined, stop_limit, instant)
        if await db.save_key_value(key, encoded, strict=True) is not True:
            raise db.PersistenceError('daily stop write unconfirmed')
        log.warning(
            '[DURABLE_DAILY_STOP] day=%s state=PERSISTED entries_blocked=true '
            'trigger_pnl=%s stop_limit=%s evidence=v3', day, combined, stop_limit)
        if _operator_override_active(
            engine, day, state=json.loads(encoded), combined=combined, stop_limit=stop_limit
        ):
            return False
        return True
    except Exception as exc:
        # Storage or state-integrity failure remains fail-closed. It does not
        # fabricate a loss event or mutate the durable stop record. The operator
        # override deliberately cannot bypass this branch.
        log.error('[DURABLE_DAILY_STOP] state=UNCONFIRMED entries_blocked=true error=%s', type(exc).__name__)
        return True
