"""Production hardening wrapper for the BGX API.

Keeps the legacy ``main:app`` module intact while adding controls that are
safe to deploy independently:

* fail-closed score-threshold alignment before the trading app is imported;
* service readiness suitable for Railway health checks;
* an explicit confirmation header for destructive emergency close-all calls;
* audit logging for destructive API attempts.

Service readiness is intentionally separate from trading permission: a
drawdown/integrity safety pause must keep the process alive so it can observe,
reconcile and recover. This module does not change trading thresholds, position
sizing, leverage, order routing, or exchange credentials.
"""
from __future__ import annotations

import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse


# ---------------------------------------------------------------------------
# Threshold contract: strategy candidate floor and NEXUS evaluation floor
# must not silently diverge in production. If NEXUS_MIN_SCORE is absent, use
# the strategy floor. If both are present but differ, fail closed at startup.
# The canonical code fallback is 60, matching production's current contract.
# ---------------------------------------------------------------------------
_strategy_floor_raw = os.environ.get("MIN_ENTRY_SCORE", "60").strip() or "60"
_nexus_floor_raw = os.environ.get("NEXUS_MIN_SCORE", _strategy_floor_raw).strip() or _strategy_floor_raw
try:
    _strategy_floor = float(_strategy_floor_raw)
    _nexus_floor = float(_nexus_floor_raw)
except ValueError as exc:
    raise RuntimeError("invalid score threshold configuration") from exc

if _strategy_floor != _nexus_floor:
    raise RuntimeError(
        "score threshold drift: "
        f"MIN_ENTRY_SCORE={_strategy_floor:g} != NEXUS_MIN_SCORE={_nexus_floor:g}"
    )
os.environ.setdefault("NEXUS_MIN_SCORE", _strategy_floor_raw)

# Console-script launchers may add the app directory only after Python's
# automatic sitecustomize discovery. Import it explicitly before composing
# the engine. Normal imports are cached: failed installations are not retried
# or marked healthy, and the existing startup classifier still fails closed.
import sitecustomize  # noqa: E402, F401

# Import only after the threshold contract has been validated.
from main import app  # noqa: E402
from bot.exchange import PAPER_TRADE, TRADING_MODE_REASON  # noqa: E402
from bot import runtime_mode_observability as runtime_mode  # noqa: E402
from bot.logger import log  # noqa: E402
from bot.service_readiness import evaluate_service_readiness, engine_task_healthy  # noqa: E402
from bot.runtime_readiness import runtime_readiness  # noqa: E402

_http_readiness_last_signature = None

def _readiness_blockers(snap):
    return [
        name for name, value in snap.__dict__.items()
        if isinstance(value, bool) and not value
    ]

def _log_http_readiness(snap):
    global _http_readiness_last_signature
    blockers = _readiness_blockers(snap)
    ready = bool(snap.ready_for_new_entries)
    signature = (ready, tuple(blockers))
    if signature == _http_readiness_last_signature:
        return blockers
    _http_readiness_last_signature = signature
    fields = " ".join(
        f"{name}={str(value).lower()}"
        for name, value in snap.__dict__.items()
        if isinstance(value, bool)
    )
    log_fn = log.info if ready else log.warning
    log_fn(
        "[HTTP_READINESS] endpoint=/ready status=%s %s "
        "ready_for_new_entries=%s blockers=%s",
        "READY" if ready else "NOT_READY",
        fields,
        str(ready).lower(),
        blockers,
    )
    return blockers


@app.middleware("http")
async def destructive_admin_guard(request: Request, call_next):
    """Require explicit human intent for the destructive close-all endpoint.

    Bearer authentication and the existing rate limiter remain enforced by the
    original endpoint. This adds a separate confirmation gate against
    accidental/cross-client invocation. It intentionally does not weaken or
    bypass the existing authentication layer.
    """
    if request.method.upper() == "POST" and request.url.path == "/api/close-all":
        confirm = request.headers.get("X-Confirm-Action", "")
        if confirm != "CLOSE_ALL_POSITIONS":
            log.warning(
                "[ADMIN_GUARD] action=close_all result=BLOCKED reason=confirmation_missing "
                "client=%s execution_effect=NONE",
                request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                status_code=428,
                content={
                    "detail": "Explicit confirmation required",
                    "required_header": "X-Confirm-Action: CLOSE_ALL_POSITIONS",
                },
            )
        log.critical(
            "[ADMIN_GUARD] action=close_all result=CONFIRMED client=%s ts=%d",
            request.client.host if request.client else "unknown",
            int(time.time()),
        )
    return await call_next(request)


def _loaded_instrument_count(engine) -> int:
    instruments = getattr(engine, "instruments", None)
    if instruments:
        return len(instruments)
    client = getattr(app.state, "client", None)
    getter = getattr(client, "get_instruments", None)
    if callable(getter):
        try:
            loaded = getter()
            return len(loaded or {})
        except Exception:
            return 0
    return 0


@app.get("/service_ready", include_in_schema=False)
async def service_readiness():
    """Railway rollout readiness, independent from financial execution permission."""
    engine = getattr(app.state, "engine", None)
    if engine is None:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "ready": False, "reason": "engine_unavailable"},
        )

    task = getattr(app.state, "engine_task", None)
    durable_ready = bool(
        getattr(engine, "_durable_state_enforced", False)
        and getattr(engine, "_durable_state_ok", False)
    )
    result = evaluate_service_readiness(
        bootstrap_complete=bool(getattr(app.state, "ready", False)),
        startup_blocked=bool(getattr(app.state, "blocked", False)),
        durable_state_ok=durable_ready,
        instrument_count=_loaded_instrument_count(engine),
        worker_healthy=engine_task_healthy(
            task,
            running=bool(getattr(engine, "_running", False)),
        ),
    )
    return JSONResponse(
        status_code=200 if result.ready else 503,
        content={
            "status": "ready" if result.ready else "not_ready",
            "ready": result.ready,
            "reason": result.reason,
            "financial_ready": bool(runtime_readiness(engine).ready_for_new_entries),
        },
    )


@app.get("/ready", include_in_schema=False)
async def readiness():
    """Canonical new-entry readiness. Process liveness is intentionally separate."""
    engine = getattr(app.state, "engine", None)
    if engine is None:
        return JSONResponse(status_code=503, content={"status":"not_ready","ready":False,"reason":"engine_unavailable"})
    snap = runtime_readiness(engine)
    blockers = _log_http_readiness(snap)
    body = dict(snap.__dict__)
    body["ready"] = snap.ready_for_new_entries
    body["ready_for_new_entries"] = snap.ready_for_new_entries
    body["blockers"] = blockers
    body["status"] = "ready" if snap.ready_for_new_entries else "not_ready"
    return JSONResponse(status_code=200 if snap.ready_for_new_entries else 503, content=body)

@app.get("/health", include_in_schema=False)
async def process_health():
    """Process liveness only; never authorizes financial execution."""
    return {"status":"healthy","healthy":True}
