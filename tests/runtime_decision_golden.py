"""Behavioral fingerprint of the effective production decision stack.

Installs the same ``runtime_bootstrap`` production loads, then records, for
a fixed synthetic corpus, every decision a structural refactor must keep
identical: strategy signal (score, setup, SL/TP), NEXUS decision, stop-risk
sizing and trailing geometry. Synthetic candles only; this proves behavior
preservation, never market edge.

Regenerate ONLY when a behavior change is intended and reviewed:
    python -m tests.runtime_decision_golden --write
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import random
import sys
from unittest.mock import patch

GOLDEN = Path(__file__).with_name("runtime_decision_golden_v1.json")
P15, P1H, P4H = 900_000, 3_600_000, 14_400_000
T = 1_800_000_000_000 // P15 * P15
SYMBOLS = ("BTCUSDT", "SOLUSDT", "UNIUSDT")
PINNED_ENV = {
    "EXCHANGE": "binance", "PAPER_TRADE": "true", "NEXUS_TELEGRAM": "false",
    "BGX_RUNTIME_TRUTH_ENABLED": "false", "MIN_ENTRY_SCORE": "60",
    "FEE_MULTIPLIER": "2.0", "MIN_VOLUME_MULT": "0.5", "MIN_RR_RATIO": "2.0",
    "TAKER_FEE": "0.0006", "LEVERAGE": "50", "MAX_RISK_PCT": "0.01",
    "MAX_MARGIN_PCT": "0.10", "TRAILING_TRIGGER": "0.50", "TRAILING_LOCK": "0.25",
}
INSTRUMENT = {"quantityUnit": "BASE_ASSET", "qtyStep": "0.001", "minQty": "0.001",
              "minNotional": "5", "multiplier": 1.0, "tickSize": "0.0001"}


def _series(seed, drift, n=2320, vol=0.004, start=100.0):
    r, p, out = random.Random(seed), start, []
    for _ in range(n):
        o = p
        c = max(0.01, o * (1 + drift + r.gauss(0, vol)))
        h = max(o, c) * (1 + abs(r.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(r.gauss(0, vol / 2)))
        out.append((o, h, lo, c, r.uniform(50, 150)))
        p = c
    return out


def _klines(seed, drift, vol=0.004, last_volume_factor=1.0, spike=0.0):
    base = _series(seed, drift, vol=vol)
    if spike:
        # Monotonic run in the trend direction to push RSI toward its gates.
        o, h, lo, c, v = base[-1]
        tail = []
        for i in range(14):
            nc = c * (1 + spike)
            tail.append((c, max(c, nc) * 1.0005, min(c, nc) * 0.9995, nc, v))
            c = nc
        base = base[:-14] + tail
    first = T - len(base) * P15
    rows = [{"ts": first + i * P15, "o": o, "h": h, "l": lo, "c": c, "v": v}
            for i, (o, h, lo, c, v) in enumerate(base)]
    # Put the last closed bar's activity ratio near the strategy's gates.
    prior = [r["v"] for r in rows[-21:-1]]
    rows[-1]["v"] = sum(prior) / len(prior) * last_volume_factor

    def agg(period, limit):
        groups = {}
        for r in rows:
            groups.setdefault(r["ts"] // period * period, []).append(r)
        out = []
        for ts in sorted(groups):
            g = groups[ts]
            if len(g) == period // P15:
                out.append({"ts": ts, "o": g[0]["o"], "h": max(x["h"] for x in g),
                            "l": min(x["l"] for x in g), "c": g[-1]["c"],
                            "v": sum(x["v"] for x in g)})
        # Production appends the still-forming bar; keep the same shape.
        last = out[-1]
        out.append({**last, "ts": last["ts"] + period, "o": last["c"],
                    "h": last["c"], "l": last["c"], "v": 0.0})
        return out[-limit:]
    k15 = rows[-200:] + [{**rows[-1], "ts": T, "o": rows[-1]["c"],
                          "h": rows[-1]["c"], "l": rows[-1]["c"], "v": 0.0}]
    return k15[-201:], agg(P1H, 101), agg(P4H, 121)


VOLS = (0.0025, 0.004, 0.007)
VOLUME_FACTORS = (0.3, 0.395, 0.405, 0.6, 1.0, 1.4)


def corpus():
    for seed in range(60):
        vol = VOLS[seed % len(VOLS)]
        factor = VOLUME_FACTORS[(seed // len(VOLS)) % len(VOLUME_FACTORS)]
        for drift in (-0.0008, -0.0003, 0.0, 0.0003, 0.0008):
            yield f"s{seed}_d{drift}", seed, drift, vol, factor, 0.0
    for seed in range(60, 90):
        for spike in (-0.012, -0.006, 0.006, 0.012):
            drift = 0.0006 if spike > 0 else -0.0006
            yield f"s{seed}_spike{spike}", seed, drift, 0.004, 1.0, spike


def _r(x):
    return None if x is None else float(f"{float(x):.10g}")


def compute():
    os.environ.update(PINNED_ENV)
    from bot.runtime_bootstrap import install
    install()
    logging.disable(logging.CRITICAL)
    from bot import engine, nexus_ai
    from bot.hard_gate_shadow_context import scope
    from bot.professional_risk import CapitalState
    from bot.risk_manager_v3 import RiskManagerV3
    from bot.strategy import Analyzer

    out = {}
    with patch("time.time", return_value=T / 1000 + 30):
        for case, seed, drift, vol, factor, spike in corpus():
            symbol = SYMBOLS[seed % len(SYMBOLS)]
            k15, k1h, k4h = _klines(seed, drift, vol, factor, spike)
            with scope() as ctx:
                sig = Analyzer().analyze_mtf(symbol, k15, k1h, k4h, min_score=60,
                                             fee_mult=2.0, vol_mult=0.5)
                sig = sig if sig is not None else ctx.signal
            if sig is None:
                out[case] = None
                continue
            row = {k: _r(getattr(sig, k)) if isinstance(getattr(sig, k), (int, float))
                   else getattr(sig, k)
                   for k in ("direction", "regime", "entry_type", "score", "entry",
                             "sl", "tp", "rr", "expected_pnl", "total_fees",
                             "confidence")}
            ticker = {"bid": sig.entry * 0.9999, "ask": sig.entry * 1.0001,
                      "last": sig.entry, "price": sig.entry}
            d = nexus_ai.decide(symbol=symbol, k15=k15, k1h=k1h, k4h=k4h,
                                entry=sig.entry, sl=sig.sl, tp=sig.tp, ticker=ticker)
            row["nexus"] = {"decision": d.decision, "allowed": d.execution_allowed,
                            "grade": d.setup_grade, "confidence": _r(d.confidence),
                            "rr": _r(d.risk_reward), "ev": _r(d.expected_value),
                            "regime": d.market_regime,
                            "setup_quality": _r(d.setup_quality),
                            "regime_compat": _r(d.regime_compat),
                            "data_quality": _r(d.data_quality),
                            "warnings": len(d.warnings)}
            rm = RiskManagerV3()
            rm.update_capital(CapitalState(100.0, 100.0))
            rm.restore_peak_equity(100.0)
            try:
                s = rm.size_for_stop(symbol=symbol, entry=sig.entry, stop=sig.sl,
                                     instruments={symbol: INSTRUMENT},
                                     fee_rate_per_side=0.0005,
                                     expected_slippage_pct=0.002)
                row["sizing"] = {"qty": _r(s.qty), "notional": _r(s.notional),
                                 "risk_budget": _r(s.risk_budget),
                                 "required_margin": _r(s.required_margin),
                                 "binding": s.binding_constraint}
            except Exception as exc:  # recorded, not hidden
                row["sizing_error"] = type(exc).__name__
            pos = engine.Position(sig, 2.0)
            move = abs(sig.tp - sig.entry) * 0.6
            pos.update_pnl(sig.entry + move if sig.direction == "LONG" else sig.entry - move)
            row["trail_full"] = _r(pos.calc_trailing_sl())
            pos.qty = 1.0
            pos.update_pnl(pos.current_price)
            row["trail_after_partial"] = _r(pos.calc_trailing_sl())
            out[case] = row
    return out


if __name__ == "__main__":
    result = compute()
    text = json.dumps(result, sort_keys=True, indent=1) + "\n"
    if sys.argv[1:] == ["--write"]:
        GOLDEN.write_text(text)
        print(f"wrote {GOLDEN} cases={len(result)} "
              f"signals={sum(v is not None for v in result.values())}")
    else:
        sys.stdout.write(text)
