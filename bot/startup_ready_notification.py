"""Post-startup Telegram status notification for NEXUS-7.

Observability only. This module never grants execution permission, changes risk,
modifies leverage/sizing, or mutates exchange state. The existing reviewed
TradingEngine.run owner may start this watcher without transferring callable
ownership to this module.
"""
from __future__ import annotations

import asyncio


async def _notify_when_runtime_ready(engine, log, *, timeout_s: float = 90.0) -> None:
    if getattr(engine, "paper_trade", False):
        return
    if bool(getattr(engine, "_validation_safety_lock_active", False)):
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, float(timeout_s))
    while loop.time() < deadline:
        if bool(getattr(engine, "connected", False)) and bool(getattr(engine, "active", False)):
            await asyncio.sleep(2.0)
            try:
                from bot.notifier import notify
                from bot.status_observability import execution_observability

                state = execution_observability(engine)
                mode = state.get("effective_execution_mode")
                blockers = list(state.get("execution_blockers") or ())

                if mode == "LIVE_BLOCKED":
                    blocker_text = ", ".join(blockers) or "RUNTIME_GATE"
                    dd = state.get("drawdown_pct")
                    limit = state.get("drawdown_limit_pct")
                    dd_line = ""
                    if dd is not None and limit is not None:
                        dd_line = f"\nDrawdown: *{dd:.2f}%* | limite: *{limit:.2f}%*"
                    result = await notify(
                        "🟠 *NEXUS-7 ONLINE — LIVE BLOQUEADO*\n"
                        "`━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                        "✅ Engine conectado e ativo.\n"
                        "🔒 Novas entradas estão bloqueadas por gate de risco/pré-trade.\n"
                        f"Motivo: `{blocker_text}`{dd_line}"
                    )
                elif mode == "LIVE":
                    result = await notify(
                        "🔴 *NEXUS-7 ONLINE — LIVE OPERACIONAL*\n"
                        "`━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                        "✅ Engine conectado e ativo.\n"
                        "✅ Runtime liberado pelos gates observáveis atuais.\n"
                        "⚠️ Cada nova entrada ainda passa por NEXUS, risco e validações pré-trade."
                    )
                else:
                    result = await notify(
                        "🟠 *NEXUS-7 ONLINE — ESTADO RESTRITO*\n"
                        "`━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                        f"Estado efetivo: `{mode or 'UNKNOWN'}`.\n"
                        "Nenhuma disponibilidade de novas entradas é afirmada."
                    )
                from bot.telegram_transport import delivered
                log.info(
                    "[STARTUP_READY_NOTIFICATION] sent=%s effective_mode=%s blockers=%s execution_effect=NONE",
                    str(delivered(result)).lower(),
                    mode,
                    blockers,
                )
            except Exception as exc:
                log.warning(
                    "[STARTUP_READY_NOTIFICATION] send_failed error=%s execution_effect=NONE",
                    type(exc).__name__,
                )
            return
        await asyncio.sleep(0.25)

    log.warning(
        "[STARTUP_READY_NOTIFICATION] timeout_s=%.1f connected=%s active=%s execution_effect=NONE",
        timeout_s,
        bool(getattr(engine, "connected", False)),
        bool(getattr(engine, "active", False)),
    )


def start(engine, log):
    return asyncio.create_task(_notify_when_runtime_ready(engine, log))


async def cancel(task) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
