"""Non-secret namespace identity for durable LIVE account-equity HWM state."""
from __future__ import annotations

import hashlib
import os

from bot.state_authority_observability import database_authority_fingerprint

HWM_NAMESPACE_VERSION = "v3"
LEGACY_HWM_NAMESPACE_VERSION = "v2"


def _clean(value: str | None, fallback: str) -> str:
    value = (value or "").strip().lower()
    return value or fallback


def account_fingerprint() -> str:
    """One-way identity for the configured KuCoin account; never logs the API key."""
    api_key = os.environ.get("KUCOIN_API_KEY", "").strip()
    if not api_key:
        return "UNCONFIGURED"
    return hashlib.sha256(("kucoin|" + api_key).encode("utf-8")).hexdigest()[:16]


def legacy_hwm_namespace() -> str:
    """Namespace written by the previous release (Railway environment name,
    hard-coded KuCoin account binding). Read-only migration source."""
    env = _clean(os.environ.get("RAILWAY_ENVIRONMENT_NAME") or os.environ.get("RAILWAY_ENVIRONMENT"), "unknown")
    account = account_fingerprint()
    authority = database_authority_fingerprint()
    return f"{LEGACY_HWM_NAMESPACE_VERSION}:environment={env}:exchange=kucoin:account={account}:db={authority}"


def hwm_namespace() -> str:
    """NOVO-03: stable across Railway project/environment/service changes and
    bound to the ACTIVE venue account (exchange + key fingerprint + database)."""
    from bot import financial_namespace as fn
    return (f"{HWM_NAMESPACE_VERSION}:exchange={fn.exchange()}:account={fn.account_fingerprint()}"
            f":db={database_authority_fingerprint()}")


def equity_peak_key() -> str:
    return f"risk:account_equity_peak:{hwm_namespace()}"


def provenance_key() -> str:
    return f"risk:account_equity_peak:provenance:{hwm_namespace()}"
