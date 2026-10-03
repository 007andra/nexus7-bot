"""Fail-closed authorization gate for leaving validation-held SHADOW mode.

This module has no exchange/network access and performs no mutations. It only
answers whether every explicit operator acknowledgement required for the
controlled real-money pilot is present. Missing, misspelled, partial, or
ambiguous configuration always keeps VALIDATION_LOCK in force.
"""
from __future__ import annotations

import os

LIVE_TRADING_TOKEN = "I_UNDERSTAND_THE_RISK"
PILOT_RELEASE_TOKEN = "I_APPROVE_TWO_LIVE_PILOT_ORDERS"
VALIDATION_RELEASE_TOKEN = "I_APPROVE_CONTROLLED_LIVE_PILOT_EXECUTION"


def _value(name: str) -> str:
    return os.environ.get(name, "").strip()


def _exchange_migration_ready() -> bool:
    exchange = _value("EXCHANGE").lower().replace("-", "_")
    if exchange in {"binance", "binance_usdm", "binance_futures", "usdm"}:
        return _value("BINANCE_LIVE_MIGRATION_READY").lower() == "true"
    return True


def release_checks() -> dict[str, bool]:
    """Return each independent prerequisite without granting execution."""
    return {
        "exchange_migration_ready": _exchange_migration_ready(),
        "paper_disabled": _value("PAPER_TRADE").lower() == "false",
        "live_trading_confirmed": _value("LIVE_TRADING_CONFIRMED") == LIVE_TRADING_TOKEN,
        "pilot_enabled": _value("REAL_TRADING_PILOT").lower() == "true",
        "pilot_account_confirmed": _value("PILOT_ACCOUNT_CONFIRMED").lower() == "true",
        "pilot_release_approved": _value("PILOT_RELEASE_APPROVED") == PILOT_RELEASE_TOKEN,
        "validation_release_approved": (
            _value("VALIDATION_LOCK_RELEASE_APPROVED") == VALIDATION_RELEASE_TOKEN
        ),
    }


def live_pilot_release_authorized() -> bool:
    """True only when every explicit release prerequisite matches exactly."""
    checks = release_checks()
    return bool(checks) and all(checks.values())


def missing_release_checks() -> list[str]:
    """Stable names for operator observability; never include secret values."""
    return [name for name, ok in release_checks().items() if not ok]
