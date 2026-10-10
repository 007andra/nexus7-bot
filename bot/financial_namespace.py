"""NOVO-03 — stable durable financial namespace (independent of Railway IDs).

Durable financial state (daily stop, daily PnL ledger, partial/RR exit
idempotency, trade lineage, accounting evidence, equity HWM) was namespaced by
``RAILWAY_PROJECT_ID|RAILWAY_SERVICE_ID|RAILWAY_ENVIRONMENT_ID`` (or the
Railway environment name). Moving/recreating the Railway project, environment
or service changed every key: a triggered daily stop, the HWM peak (drawdown
gate) or a partial exit's idempotency silently "disappeared". The HWM namespace
also bound the account with ``KUCOIN_API_KEY`` and ``exchange=kucoin`` even on
Binance, so two Binance accounts sharing one database shared one namespace.

Stable scope = sha256("nexus-financial-v1" | exchange | account | database):
  * exchange: the ACTIVE venue (bot.exchange selection, EXCHANGE env);
  * account : one-way fingerprint of the ACTIVE venue's API key (never logged);
  * database: the canonical PostgreSQL authority fingerprint.
Same DB + same account + different Railway project/env/service => same scope.
Different accounts => different scopes (never collide).

Migration: ``legacy_key_for(new_key)`` maps a stable-scope key to the key the
previous release wrote; ``database.load_key_value`` reads it when the stable
key is absent, so upgrading never loses existing state. New writes go to the
stable key, which then wins.
"""
from __future__ import annotations

import hashlib
import os

VERSION = "nexus-financial-v1"
_ALIASES: dict[str, str] = {}


def exchange() -> str:
    raw = os.environ.get("EXCHANGE", "kucoin").strip().lower().replace("-", "_")
    return "binance" if raw in {"binance", "binance_usdm", "binance_futures", "usdm"} else "kucoin"


def account_fingerprint() -> str:
    """One-way identity of the ACTIVE venue's account; the key is never logged."""
    venue = exchange()
    api_key = os.environ.get("BINANCE_API_KEY" if venue == "binance" else "KUCOIN_API_KEY", "").strip()
    if not api_key:
        return "UNCONFIGURED"
    return hashlib.sha256((venue + "|" + api_key).encode("utf-8")).hexdigest()[:16]


def database_fingerprint() -> str:
    from bot.state_authority_observability import database_authority_fingerprint
    return database_authority_fingerprint()


def stable_scope() -> str:
    raw = "|".join((VERSION, exchange(), account_fingerprint(), database_fingerprint()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def legacy_railway_values() -> list[str]:
    return [os.environ.get(k, "") for k in
            ("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT_ID")]


def legacy_railway_scope() -> str:
    """The scope written by the previous release (kept ONLY for reads)."""
    return hashlib.sha256("|".join(legacy_railway_values()).encode()).hexdigest()[:24]


def register_alias(new_key: str, legacy_key: str) -> None:
    if new_key and legacy_key and new_key != legacy_key:
        _ALIASES[new_key] = legacy_key


def legacy_key_for(key: str) -> str | None:
    """Key the previous release used for the same state, or None."""
    if key in _ALIASES:
        return _ALIASES[key]
    stable, legacy = stable_scope(), legacy_railway_scope()
    if stable != legacy and stable in key:
        return key.replace(stable, legacy)
    from bot import hwm_namespace
    new_ns, old_ns = hwm_namespace.hwm_namespace(), hwm_namespace.legacy_hwm_namespace()
    if new_ns != old_ns and new_ns in key:
        return key.replace(new_ns, old_ns)
    return None
