"""Opt-in, DB-free observation of internal NEXUS loop/lease liveness.

Never restarts tasks, renews ownership, writes DB, queries Binance, or authorizes
orders. This is independent of the engine's own loop and its shared DB lock.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import math
import os
import time

FLAG = "NEXUS_INTERNAL_LIVENESS_DIAGNOSTIC_V1"
INTERVAL_SECONDS = 15.0
STALE_LOOP_SECONDS = 90.0
EMIT_EVERY_SECONDS = 300.0


def enabled():
    return os.environ.get(FLAG, "false").strip().lower() in ("true", "1", "yes", "on")


def _task_status(task):
    if task is None:
        return "NOT_STARTED"
    return "DONE" if task.done() else "RUNNING"


def _task_terminal_kind(task):
    """Classify only completion form; never log task exceptions or repr."""
    if task is None:
        return "NOT_STARTED"
    if not task.done():
        return "RUNNING"
    try:
        if task.cancelled():
            return "CANCELLED"
        return "EXCEPTION" if task.exception() is not None else "RETURNED"
    except BaseException:
        # The observer must never propagate a task-inspection failure.
        return "UNKNOWN"


def snapshot(engine, task, *, monotonic=None, now_utc=None):
    """Read local state only. No async, I/O, guard bypass, or secret fields."""
    monotonic = time.monotonic() if monotonic is None else float(monotonic)
    now_utc = datetime.now(timezone.utc) if now_utc is None else now_utc
    cycle_started = getattr(engine, "_liveness_cycle_started_monotonic", None)
    cycle_age = None
    if isinstance(cycle_started, (int, float)) and math.isfinite(cycle_started):
        cycle_age = max(0.0, monotonic - cycle_started)
    heartbeat_task = getattr(engine, "_ownership_heartbeat_task", None)
    heartbeat_status = _task_status(heartbeat_task)
    engine_status = _task_status(task)
    expires = getattr(engine, "_execution_ownership_expires_at", None)
    lease_remaining = None
    if isinstance(expires, datetime) and expires.tzinfo is not None:
        lease_remaining = (expires - now_utc).total_seconds()
        if not math.isfinite(lease_remaining):
            lease_remaining = None
    if engine_status == "DONE" or heartbeat_status == "DONE":
        state = "TASK_DONE"
    elif lease_remaining is not None and lease_remaining <= 0:
        state = "LOCAL_LEASE_EXPIRED"
    elif cycle_age is not None and cycle_age >= STALE_LOOP_SECONDS:
        state = "LOOP_STALE"
    elif cycle_age is None:
        state = "STARTUP_PENDING"
    else:
        state = "RUNNING"
    return {
        "status": state,
        "engine_task": engine_status,
        "heartbeat_task": heartbeat_status,
        "engine_exit_kind": _task_terminal_kind(task),
        "heartbeat_exit_kind": _task_terminal_kind(heartbeat_task),
        "main_loop_caught_cancel": bool(
            getattr(engine, "_liveness_cancelled_in_main_loop", False)
        ),
        "cycle_age_s": round(cycle_age, 1) if cycle_age is not None else None,
        "local_lease_remaining_s": round(lease_remaining, 1) if lease_remaining is not None else None,
        "observation_only": True,
        "db_reads": 0,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
        "promotion_allowed": False,
        "live_allowed": False,
    }


def format_event(result):
    """Fixed, allowlisted output. Never includes task exception or credentials."""
    number = lambda v: "NA" if v is None else f"{v:.1f}"
    return (
        "[ENGINE_INTERNAL_LIVENESS_V1] "
        f"status={result['status']} "
        f"engine_task={result['engine_task']} "
        f"heartbeat_task={result['heartbeat_task']} "
        f"engine_exit_kind={result['engine_exit_kind']} "
        f"heartbeat_exit_kind={result['heartbeat_exit_kind']} "
        f"main_loop_caught_cancel={str(result['main_loop_caught_cancel']).lower()} "
        f"cycle_age_s={number(result['cycle_age_s'])} "
        f"local_lease_remaining_s={number(result['local_lease_remaining_s'])} "
        "observation_only=true db_reads=0 decision_effect=NONE "
        "execution_effect=NONE promotion_allowed=false live_allowed=false"
    )


UNHEALTHY_STATUSES = ("TASK_DONE", "LOCAL_LEASE_EXPIRED", "LOOP_STALE")
ALERT_REPEAT_SECONDS = 1800.0
ALERT_TIMEOUT_SECONDS = 20.0


def format_alert(result):
    """Operator alert text; same allowlisted fields as the log line."""
    return (
        "⚠️ ENGINE PARADO — " + format_event(result) + "\n"
        "O processo segue online, mas o loop do engine não está saudável. "
        "Nenhuma ação automática foi tomada: verificar posições/proteções na "
        "Binance e decidir reinício manual."
    )


def _dispatch_alert(alert, text, logger):
    """Fire-and-forget so a slow Telegram call never stalls the watcher."""
    async def _send():
        try:
            await asyncio.wait_for(alert(text), timeout=ALERT_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[ENGINE_INTERNAL_LIVENESS_V1] alert_delivery=FAILED error=%s "
                "observation_only=true execution_effect=NONE", type(exc).__name__,
            )
    return asyncio.ensure_future(_send())


async def run(engine, engine_task, *, sleep=asyncio.sleep, clock=time.monotonic,
              interval_s=INTERVAL_SECONDS, emit_every_s=EMIT_EVERY_SECONDS,
              logger=None, alert=None, alert_repeat_s=ALERT_REPEAT_SECONDS):
    """Independent ticker: capped logging, no mutation or recovery action.

    ``alert`` (optional async callable) is notified on entering an unhealthy
    status and at most every ``alert_repeat_s`` while it persists. Alerting
    is notification only: it never restarts tasks or touches trading state.
    """
    if logger is None:
        from bot.logger import log
        logger = log
    previous_status = None
    last_logged = None
    last_alerted = None
    pending_alerts = set()
    while True:
        try:
            now = clock()
            result = snapshot(engine, engine_task, monotonic=now)
            must_emit = (
                previous_status != result["status"] or last_logged is None
                or now - last_logged >= emit_every_s
            )
            if must_emit:
                line = format_event(result)
                if result["status"] in UNHEALTHY_STATUSES:
                    logger.warning("%s", line)
                else:
                    logger.info("%s", line)
                last_logged = now
            if result["status"] in UNHEALTHY_STATUSES:
                if alert is not None and (
                    previous_status != result["status"] or last_alerted is None
                    or now - last_alerted >= alert_repeat_s
                ):
                    task = _dispatch_alert(alert, format_alert(result), logger)
                    pending_alerts.add(task)
                    task.add_done_callback(pending_alerts.discard)
                    last_alerted = now
            else:
                last_alerted = None
            previous_status = result["status"]
        except asyncio.CancelledError:
            raise
        except Exception:
            # Observer error is never allowed to change any trading path.
            logger.warning(
                "[ENGINE_INTERNAL_LIVENESS_V1] status=OBSERVER_ERROR "
                "observation_only=true db_reads=0 decision_effect=NONE "
                "execution_effect=NONE promotion_allowed=false live_allowed=false"
            )
        await sleep(interval_s)
