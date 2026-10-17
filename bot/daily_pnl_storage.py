"""PnL namespace independent of stop policy; non-destructive legacy import.

Legacy ledgers remain readable on every checkpoint, including records written
by an overlapping old deployment. Identical events deduplicate; conflicting
evidence fails closed. No historical daily-stop flags are imported.
"""
import json
import math
from datetime import datetime, timezone

from bot import database as db


def scope():
    # NOVO-03: stable financial scope; legacy Railway-scoped keys are read by
    # database.load_key_value during migration.
    from bot.financial_namespace import stable_scope
    return stable_scope()


def ledger_key(day):
    return f'daily_pnl_v1:{scope()}:{day}'


def legacy_keys(day):
    return [f'daily_stop_v{v}:{scope()}:pnl-ledger-v1:{day}' for v in (1, 2, 3)]


def validate(state, day):
    if not isinstance(state, dict) or state.get('version') != 1 or state.get('day') != day or not isinstance(state.get('events'), dict):
        raise ValueError('invalid daily PnL ledger')
    for token, value in state['events'].items():
        if not isinstance(token, str) or len(token) != 64 or not isinstance(value, dict):
            raise ValueError('invalid daily PnL event')
        stamp = datetime.fromisoformat(value['closed_at'])
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if isinstance(value['pnl'], bool) or not math.isfinite(float(value['pnl'])) or stamp.astimezone(timezone.utc).date().isoformat() != day:
            raise ValueError('invalid daily PnL value')


async def load(day):
    """Return canonical raw bytes plus merged state; caller persists atomically."""
    raw = await db.load_key_value(ledger_key(day), strict=True)
    state = json.loads(raw) if raw is not None else None
    if state is not None:
        validate(state, day)
    for key in legacy_keys(day):
        legacy = await db.load_key_value(key, strict=True)
        if legacy is None:
            continue
        old = json.loads(legacy)
        validate(old, day)
        if state is None:
            state = old
            continue
        for token, value in old['events'].items():
            previous = state['events'].get(token)
            if previous is not None:
                if any(previous[k] != value[k] for k in previous.keys() & value.keys()):
                    raise ValueError('conflicting legacy daily PnL evidence')
                state['events'][token] = {**value, **previous}
            else:
                state['events'][token] = value
        # Missing coverage metadata is not evidence of full history.
        if state.get('coverage_started_at') != old.get('coverage_started_at'):
            state['coverage_started_at'] = 'MIXED_LEGACY_COVERAGE'
    return raw, state
