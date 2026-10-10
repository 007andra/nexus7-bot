"""Shared fixtures for tests that exercise the one-shot path behind an ACTIVE risk epoch."""
from unittest.mock import AsyncMock

from bot import risk_epoch

TEST_EPOCH_ID = "TEST_RISK_EPOCH_30PCT_V1"

EPOCH_ENV = {
    risk_epoch.ENABLED_ENV: "true",
    risk_epoch.EPOCH_ID_ENV: TEST_EPOCH_ID,
    risk_epoch.MAX_DRAWDOWN_ENV: "0.30",
}


def active_state(start_equity=5.39561426, peak=None, equity=None, limit=0.30,
                 epoch_id=TEST_EPOCH_ID) -> dict:
    peak = float(start_equity if peak is None else peak)
    equity = float(start_equity if equity is None else equity)
    return {
        "epoch_id": epoch_id,
        "status": risk_epoch.ACTIVE,
        "reason": "observed",
        "historical_drawdown": 0.76333669401,
        "historical_drawdown_limit": 0.10,
        "historical_peak_equity": 22.7986938551,
        "epoch_drawdown_limit": limit,
        "epoch_start_equity": float(start_equity),
        "epoch_started_at": "2026-10-08T00:00:00+00:00",
        "epoch_peak_equity": peak,
        "epoch_equity": equity,
        "epoch_drawdown": risk_epoch.epoch_drawdown(peak, equity),
        "epoch_floor_equity": risk_epoch.floor_equity(peak, limit),
        "epoch_headroom_usdt": equity - risk_epoch.floor_equity(peak, limit),
        "epoch_breached_at": None,
        "baseline_digest": "f" * 64,
        **risk_epoch.AUTHORITY,
    }


def install_active_epoch(engine, **kwargs) -> dict:
    state = active_state(**kwargs)
    engine._risk_epoch_state = state
    return state


def observe_keeps_active(engine, **kwargs):
    """AsyncMock for risk_epoch.observe that re-installs an ACTIVE state."""
    async def _observe(eng, *args, **kw):
        return install_active_epoch(eng, **kwargs)
    return AsyncMock(side_effect=_observe)
