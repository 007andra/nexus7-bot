"""H1 (TREND_LONG_V1): B2 long only while BTC is above its 200-day average, shadow mode.

Rules, identical to ``bot.trend_long_study_v1.simulate_h1``:

* Signal on a closed UTC day: close above the highest high of the previous 20
  days, only when flat on the symbol and only if BTCUSDT closed that same day
  above the simple average of its last 200 closes (that day included).
* Entry at the next day's open, stop at 2 x ATR(20) below. Exit at the next
  open after a close below the 10-day low, or at the stop. No shorts.

TREND_LONG_V1 passed its pre-registered criterion (H1_PASS), but the gain came
from 2020-2021 and 2025-2026 were negative. The decision on real money waits
for H1_PROSPECTIVE_V1. This module only observes: public daily klines once per
UTC day, virtual positions in one durable key, logs. It never sends, changes or
cancels an order and never touches risk, sizing or the entry path.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from bot import b2_trend_strategy as b2

DAY_MS = b2.DAY_MS
SYMBOLS = b2.SYMBOLS
FILTER_SYMBOL = "BTCUSDT"
SMA_DAYS = 200
SHADOW_FLAG = "STRATEGY_H1_SHADOW"
STATE_KEY = "strategy:h1:shadow:state:v1"
KLINE_LIMIT = 60
FILTER_KLINE_LIMIT = 240  # 200 closed days plus margin for gaps and the forming day
RUN_DELAY_S = b2.RUN_DELAY_S
AUTHORITY = b2.AUTHORITY


def btc_above_sma(btc_klines: list[dict], now_ms: int) -> dict:
    """Filter for the last closed day: {'day_ts': ts, 'up': bool} or {'day_ts': None}."""
    days = b2.closed_days(btc_klines, now_ms)
    if len(days) < SMA_DAYS:
        return {"day_ts": None, "up": None}
    last = days[-1]
    sma = sum(d["c"] for d in days[-SMA_DAYS:]) / SMA_DAYS
    return {"day_ts": int(last["ts"]), "up": last["c"] > sma, "close": last["c"], "sma": sma}


def allow_entry_for(btc: dict):
    def allow(sig: dict, days: list[dict]) -> bool:
        return (sig["direction"] == "LONG" and btc.get("up") is True
                and btc.get("day_ts") == int(days[-1]["ts"]))
    return allow


def step(state: dict, symbol: str, klines: list[dict], now_ms: int, btc: dict) -> list[dict]:
    return b2.step(state, symbol, klines, now_ms, allow_entry=allow_entry_for(btc))


# ---------------------------------------------------------------- runtime

def enabled() -> bool:
    return os.environ.get(SHADOW_FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def _log_event(log, ev: dict) -> None:
    pos = ev["position"]
    if ev["event"] == "ENTRY":
        log.warning(
            "[H1_SHADOW] event=ENTRY symbol=%s direction=%s entry=%.8g stop=%.8g stop_pct=%.2f "
            "channel=%.8g signal_close=%.8g %s",
            ev["symbol"], pos["direction"], ev["price"], pos["stop"], ev["stop_pct"],
            pos["channel"], pos["signal_close"], AUTHORITY,
        )
    else:
        log.warning(
            "[H1_SHADOW] event=EXIT symbol=%s direction=%s reason=%s entry=%.8g exit=%.8g "
            "gross_r=%.3f %s",
            ev["symbol"], pos["direction"], ev["reason"], pos["entry"], ev["price"], ev["r"], AUTHORITY,
        )


async def run_once(engine, log, now_ms: int | None = None) -> dict:
    from bot import database as db

    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    raw = await db.load_key_value(STATE_KEY, strict=False)
    state = json.loads(raw) if raw else {"version": 1, "positions": {}, "last_day_ts": 0}
    today = (now_ms // DAY_MS) * DAY_MS
    if int(state.get("last_day_ts", 0)) >= today:
        return state
    errors = 0
    btc = {"day_ts": None, "up": None}
    btc_klines = None
    try:
        btc_klines = await engine.client.get_klines(FILTER_SYMBOL, "D", FILTER_KLINE_LIMIT)
        btc = btc_above_sma(btc_klines, now_ms)
    except Exception as exc:
        errors += 1
        log.warning("[H1_SHADOW] event=FETCH_ERROR symbol=%s error=%s %s",
                    FILTER_SYMBOL, type(exc).__name__, AUTHORITY)
    state["filter"] = btc
    for symbol in SYMBOLS:
        if symbol == FILTER_SYMBOL and btc_klines is not None:
            klines = btc_klines
        else:
            try:
                klines = await engine.client.get_klines(symbol, "D", KLINE_LIMIT)
            except Exception as exc:
                errors += 1
                log.warning("[H1_SHADOW] event=FETCH_ERROR symbol=%s error=%s %s",
                            symbol, type(exc).__name__, AUTHORITY)
                continue
        for ev in step(state, symbol, klines, now_ms, btc):
            _log_event(log, ev)
            if ev["event"] == "EXIT":
                b2.record_exit(state, ev, now_ms)
    if errors == 0:
        state["last_day_ts"] = today
    await db.save_key_value(STATE_KEY, json.dumps(state, sort_keys=True), strict=False)
    log.warning(
        "[H1_SHADOW] event=DAY_DONE day_ts=%d btc_up=%s open_positions=%d fetch_errors=%d %s",
        today, btc.get("up"), len(state.get("positions", {})), errors, AUTHORITY,
    )
    return state


async def _loop(engine, log) -> None:
    while True:
        try:
            await run_once(engine, log)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[H1_SHADOW] event=RUN_ERROR error=%s %s", type(exc).__name__, AUTHORITY)
        now = time.time()
        next_run = (int(now // 86_400) + 1) * 86_400 + RUN_DELAY_S
        await asyncio.sleep(max(60.0, next_run - now))


def schedule_if_enabled(engine, log) -> bool:
    if not enabled():
        return False
    task = getattr(engine, "_h1_shadow_task", None)
    if task is not None and not task.done():
        return False
    starter = getattr(engine, "_start_background", None)
    if not callable(starter):
        raise RuntimeError("engine background manager unavailable")
    engine._h1_shadow_task = starter(_loop(engine, log))
    log.warning("[H1_SHADOW] status=SCHEDULED symbols=%d %s", len(SYMBOLS), AUTHORITY)
    return True
