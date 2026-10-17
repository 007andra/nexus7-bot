"""B2 daily channel breakout (TREND_BREAKOUT_V1 hypothesis B2), shadow mode.

Rules, identical to ``bot.trend_breakout_study_v1.simulate_b2``:

* Signal on a closed UTC day: close above the highest high of the previous 20
  days (LONG) or below the lowest low (SHORT), only when flat on the symbol.
* Entry at the next day's open. Initial stop at 2 x ATR(20) from the entry.
  No take profit, no partial, no trailing, no time exit.
* Exit at the next day's open after a close beyond the opposite 10-day channel,
  or at the stop.

The historical study did NOT pass its pre-registered criterion (B2_FAIL, mean
+0.20 R with a negative bootstrap lower bound). This module only observes: it
reads public daily klines once per UTC day, keeps virtual positions in one
durable key and logs what B2 would do. It never sends, changes or cancels an
order and never touches risk, sizing or the entry path.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

DAY_MS = 86_400_000
PARAMS = {"entry_days": 20, "exit_days": 10, "atr_days": 20, "stop_atr_mult": 2.0}
SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "TRXUSDT", "LINKUSDT", "AVAXUSDT",
    "DOTUSDT", "LTCUSDT", "NEARUSDT", "ATOMUSDT", "SUIUSDT",
    "PEOPLEUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "UNIUSDT",
    "AAVEUSDT", "FILUSDT", "INJUSDT", "SEIUSDT", "ETCUSDT",
)
SHADOW_FLAG = "STRATEGY_B2_SHADOW"
STATE_KEY = "strategy:b2:shadow:state:v1"
HISTORY_LIMIT = 5000  # closed virtual trades kept in the state key
KLINE_LIMIT = 60
RUN_DELAY_S = 120
AUTHORITY = "research_only=true shadow_only=true live_allowed=false decision_effect=NONE execution_effect=NONE"


def history_needed(params: dict = PARAMS) -> int:
    return max(params["entry_days"], params["exit_days"], params["atr_days"] + 1)


# ---------------------------------------------------------------- pure rules

def closed_days(klines: list[dict], now_ms: int) -> list[dict]:
    """Closed daily bars ending at the last close, newest contiguous run only."""
    bars = sorted((b for b in klines if int(b["ts"]) + DAY_MS <= now_ms), key=lambda b: b["ts"])
    run: list[dict] = []
    for bar in bars:
        if run and int(bar["ts"]) - int(run[-1]["ts"]) != DAY_MS:
            run = []
        run.append(bar)
    return run


def forming_day(klines: list[dict], now_ms: int) -> dict | None:
    today = (now_ms // DAY_MS) * DAY_MS
    for bar in klines:
        if int(bar["ts"]) == today:
            return bar
    return None


def atr(days: list[dict], n: int) -> float:
    """Mean true range of the last ``n`` days (needs ``n + 1`` days)."""
    total = 0.0
    for j in range(len(days) - n, len(days)):
        d, p = days[j], days[j - 1]
        total += max(d["h"] - d["l"], abs(d["h"] - p["c"]), abs(d["l"] - p["c"]))
    return total / n


def entry_signal(days: list[dict], params: dict = PARAMS) -> dict | None:
    """Breakout on the last closed day, or None."""
    if len(days) < history_needed(params) + 1:
        return None
    last = days[-1]
    window = days[-1 - params["entry_days"]:-1]
    hi = max(d["h"] for d in window)
    lo = min(d["l"] for d in window)
    distance = params["stop_atr_mult"] * atr(days, params["atr_days"])
    if distance <= 0:
        return None
    if last["c"] > hi:
        return {"direction": "LONG", "distance": distance, "channel": hi, "close": last["c"]}
    if last["c"] < lo:
        return {"direction": "SHORT", "distance": distance, "channel": lo, "close": last["c"]}
    return None


def exit_signal(days: list[dict], direction: str, params: dict = PARAMS) -> bool:
    """Close of the last closed day beyond the opposite exit channel."""
    if len(days) < history_needed(params) + 1:
        return False
    last = days[-1]
    window = days[-1 - params["exit_days"]:-1]
    if direction == "LONG":
        return last["c"] < min(d["l"] for d in window)
    return last["c"] > max(d["h"] for d in window)


def stop_hit(day: dict, position: dict) -> tuple[float, str] | None:
    """Stop touched during a closed day after the entry; gap opens fill at the open."""
    long = position["direction"] == "LONG"
    stop = position["stop"]
    if int(day["ts"]) > int(position["entry_day_ts"]) and (day["o"] <= stop if long else day["o"] >= stop):
        return day["o"], "STOP_GAP"
    if day["l"] <= stop if long else day["h"] >= stop:
        return stop, "STOP"
    return None


def r_multiple(position: dict, exit_px: float) -> float:
    side = 1.0 if position["direction"] == "LONG" else -1.0
    return side * (exit_px - position["entry"]) / position["distance"]


def step(state: dict, symbol: str, klines: list[dict], now_ms: int, allow_entry=None) -> list[dict]:
    """Advance one symbol to ``now_ms``; returns the events and mutates ``state``.

    ``allow_entry(signal, days)`` can veto a new entry (used by H1); exits are
    never filtered.
    """
    days = closed_days(klines, now_ms)
    today = forming_day(klines, now_ms)
    positions = state.setdefault("positions", {})
    events: list[dict] = []
    pos = positions.get(symbol)
    if pos is not None:
        for day in days:
            if int(day["ts"]) < int(pos["entry_day_ts"]) or int(day["ts"]) <= int(pos.get("checked_day_ts", 0)):
                continue
            hit = stop_hit(day, pos)
            pos["checked_day_ts"] = int(day["ts"])
            if hit is not None:
                events.append({"event": "EXIT", "symbol": symbol, "reason": hit[1], "price": hit[0],
                               "r": r_multiple(pos, hit[0]), "position": dict(pos)})
                positions.pop(symbol, None)
                pos = None
                break
        if pos is not None and days and exit_signal(days, pos["direction"]) and today is not None:
            events.append({"event": "EXIT", "symbol": symbol, "reason": "CHANNEL_EXIT",
                           "price": today["o"], "r": r_multiple(pos, today["o"]), "position": dict(pos)})
            positions.pop(symbol, None)
            pos = None
            # The study waits for the next close before a new signal; so do we.
            return events
    if pos is None and today is not None:
        sig = entry_signal(days)
        if sig is not None and allow_entry is not None and not allow_entry(sig, days):
            sig = None
        if sig is not None and int(days[-1]["ts"]) + DAY_MS == int(today["ts"]):
            side = 1.0 if sig["direction"] == "LONG" else -1.0
            entry = today["o"]
            stop = entry - side * sig["distance"]
            if stop > 0:
                positions[symbol] = {
                    "direction": sig["direction"], "entry": entry, "stop": stop,
                    "distance": sig["distance"], "entry_day_ts": int(today["ts"]),
                    "checked_day_ts": 0, "signal_close": sig["close"], "channel": sig["channel"],
                }
                events.append({"event": "ENTRY", "symbol": symbol, "price": entry,
                               "position": dict(positions[symbol]),
                               "stop_pct": 100.0 * sig["distance"] / entry})
    return events


# ---------------------------------------------------------------- runtime

def enabled() -> bool:
    return os.environ.get(SHADOW_FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def _log_event(log, ev: dict) -> None:
    pos = ev["position"]
    if ev["event"] == "ENTRY":
        log.warning(
            "[B2_SHADOW] event=ENTRY symbol=%s direction=%s entry=%.8g stop=%.8g stop_pct=%.2f "
            "channel=%.8g signal_close=%.8g %s",
            ev["symbol"], pos["direction"], ev["price"], pos["stop"], ev["stop_pct"],
            pos["channel"], pos["signal_close"], AUTHORITY,
        )
    else:
        log.warning(
            "[B2_SHADOW] event=EXIT symbol=%s direction=%s reason=%s entry=%.8g exit=%.8g "
            "gross_r=%.3f %s",
            ev["symbol"], pos["direction"], ev["reason"], pos["entry"], ev["price"], ev["r"], AUTHORITY,
        )


def record_exit(state: dict, ev: dict, now_ms: int) -> None:
    """Durable history of closed virtual trades, to compare with the study later."""
    pos = ev["position"]
    history = state.setdefault("closed", [])
    history.append({
        "symbol": ev["symbol"], "direction": pos["direction"],
        "entry_day_ts": int(pos["entry_day_ts"]), "entry": pos["entry"], "stop": pos["stop"],
        "distance": pos["distance"], "exit": ev["price"], "reason": ev["reason"],
        "gross_r": ev["r"], "recorded_at_ms": int(now_ms),
    })
    del history[:-HISTORY_LIMIT]


async def run_once(engine, log, now_ms: int | None = None) -> dict:
    from bot import database as db

    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    raw = await db.load_key_value(STATE_KEY, strict=False)
    state = json.loads(raw) if raw else {"version": 1, "positions": {}, "last_day_ts": 0}
    today = (now_ms // DAY_MS) * DAY_MS
    if int(state.get("last_day_ts", 0)) >= today:
        return state
    errors = 0
    for symbol in SYMBOLS:
        try:
            klines = await engine.client.get_klines(symbol, "D", KLINE_LIMIT)
        except Exception as exc:
            errors += 1
            log.warning("[B2_SHADOW] event=FETCH_ERROR symbol=%s error=%s %s",
                        symbol, type(exc).__name__, AUTHORITY)
            continue
        for ev in step(state, symbol, klines, now_ms):
            _log_event(log, ev)
            if ev["event"] == "EXIT":
                record_exit(state, ev, now_ms)
    if errors == 0:
        state["last_day_ts"] = today
    await db.save_key_value(STATE_KEY, json.dumps(state, sort_keys=True), strict=False)
    log.warning(
        "[B2_SHADOW] event=DAY_DONE day_ts=%d open_positions=%d fetch_errors=%d %s",
        today, len(state.get("positions", {})), errors, AUTHORITY,
    )
    return state


async def _loop(engine, log) -> None:
    while True:
        try:
            await run_once(engine, log)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[B2_SHADOW] event=RUN_ERROR error=%s %s", type(exc).__name__, AUTHORITY)
        now = time.time()
        next_run = (int(now // 86_400) + 1) * 86_400 + RUN_DELAY_S
        await asyncio.sleep(max(60.0, next_run - now))


def schedule_if_enabled(engine, log) -> bool:
    if not enabled():
        return False
    task = getattr(engine, "_b2_shadow_task", None)
    if task is not None and not task.done():
        return False
    starter = getattr(engine, "_start_background", None)
    if not callable(starter):
        raise RuntimeError("engine background manager unavailable")
    engine._b2_shadow_task = starter(_loop(engine, log))
    log.warning("[B2_SHADOW] status=SCHEDULED symbols=%d %s", len(SYMBOLS), AUTHORITY)
    return True
