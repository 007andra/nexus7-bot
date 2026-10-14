"""EXIT_MANAGEMENT_V1: do the production LIVE exits rescue the strategy signal?

RAW_SIGNAL_EDGE_V1 failed with a fixed 10h proxy exit. This study keeps the
same signals (same effective analyzer stack, windows, isolation and funnel)
and replaces the exit with the effective LIVE management, as wired after
runtime_bootstrap:

- native STOP_MARKET at the fill-tightened stop and TAKE_PROFIT_MARKET at the
  planned ``sig.tp`` (not shifted by the fill), both closing the remainder;
- one 50% partial when profit >= |entry - current stop| + 0.03% of entry,
  then a breakeven stop (durable_partial_exit);
- giveback trailing: once profit >= 0.5 x |tp - entry|, stop = entry +/-
  0.75 x peak favourable excursion (trailing_safety_hardening);
- stops are never cancelled, so the most protective one is active;
- no stagnation, CHoCH or regime exit (inert in LIVE), 2R exit inert.

Bars are resolved conservatively: within a bar the stop is checked before
the target, and management triggered by a bar's extreme only protects from
the next bar. The verdict uses an untouched window (2024-10 to 2025-09);
2025-10 to 2026-09 was already seen by RAW_SIGNAL_EDGE_V1 and is descriptive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from bot import raw_signal_edge_study_v1 as base

PREREG = {
    "id": "EXIT_MANAGEMENT_V1",
    "symbols": list(base.PREREG["symbols"]),
    "warmup_months": ["2024-09"],
    "holdout_months": ["2024-10", "2024-11", "2024-12", "2025-01", "2025-02", "2025-03",
                       "2025-04", "2025-05", "2025-06", "2025-07", "2025-08", "2025-09"],
    "descriptive_months": ["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03",
                           "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"],
    "windows": dict(base.PREREG["windows"]),
    "min_windows": dict(base.PREREG["min_windows"]),
    "scoring_env": dict(base.PREREG["scoring_env"]),
    "exit": {"partial_fraction": 0.5, "partial_extra_pct_of_entry": 0.0003,
             "trailing_trigger_of_target": 0.5, "trailing_keep_of_peak": 0.75,
             "max_hold_bars_15m": 2016,
             "primary_resolution": "OHLC_PATH",
             "robustness_resolution": "CONSERVATIVE_STOP_FIRST_NEXT_BAR"},
    "primary_population": "FUNNEL",
    "primary_fee": 0.0006,
    "sensitivity_fees": [0.0004, 0.0005],
    "selection": "NON_OVERLAPPING_PER_SYMBOL",
    "cluster": "SYMBOL_UTC_DAY",
    "bootstrap": dict(base.PREREG["bootstrap"]),
    "gate": dict(base.PREREG["gate"]),
}


MODES = ("OHLC_PATH", "CONSERVATIVE")


def prereg_digest() -> str:
    raw = json.dumps(PREREG, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _adverse(price: float, direction: str, *, entry: bool, slip: float) -> float:
    from bot.execution_model import adverse_fill
    return adverse_fill(price, direction, is_entry=entry, slippage_rate=slip)


def simulate_live_exit(*, mode: str = "CONSERVATIVE", **kwargs) -> dict | None:
    """Simulate one position under the effective LIVE exit management.

    ``CONSERVATIVE``: within a bar the stop is checked before the target and
    management triggered by the bar's extreme only protects from the next bar.
    ``OHLC_PATH``: each bar is walked open -> low -> high -> close when it
    closes up (open -> high -> low -> close otherwise) and every rule acts as
    the path crosses it, like the bot's frequent mark-price sampling.
    """
    if mode == "CONSERVATIVE":
        return _simulate_conservative(**kwargs)
    if mode == "OHLC_PATH":
        return _simulate_path(**kwargs)
    raise ValueError(f"unknown mode {mode}")


def _setup(direction, planned_entry, planned_sl, tp, decision_idx, k15, slippage):
    long = direction == "LONG"
    sign = 1.0 if long else -1.0
    fill = _adverse(float(k15[decision_idx]["o"]), direction, entry=True, slip=slippage)
    distance = abs(planned_entry - planned_sl)
    if fill <= 0 or distance <= 0:
        return None
    # postfill_risk_recheck: keep the more protective of the planned stop and
    # the planned distance re-anchored at the fill; the TP is not shifted.
    sl = max(planned_sl, fill - distance) if long else min(planned_sl, fill + distance)
    if (fill - sl) * sign <= 0 or (tp - fill) * sign <= 0:
        return None
    # R is the planned risk (what final sizing budgets), as in RAW_SIGNAL_EDGE_V1;
    # the realized fill-to-stop distance can be arbitrarily small after a gap.
    return long, sign, fill, sl, distance / fill


def _finish(*, direction, fill, sl, legs, partial_ts, exit_ts, reason, decision_idx,
            k15, funding, fee_rate, risk_fraction):
    from bot.execution_model import fee_return_fraction, funding_return_fraction
    from bot.backtest import _timestamp_index
    from bot.nexus_oos_real_replay import _price_at_ts
    sign = 1.0 if direction == "LONG" else -1.0
    gross = sum(sign * (px - fill) / fill * w for px, w in legs)
    fees = fee_return_fraction(fill, legs, fee_rate)
    timestamps = _timestamp_index(k15)
    funding_part, _ = funding_return_fraction(
        funding, direction, int(k15[decision_idx]["ts"]), exit_ts, fill,
        price_at_ts=lambda ts: _price_at_ts(k15, timestamps, ts),
        partial_after_ts_ms=partial_ts)
    return {"net_r": (gross - fees + funding_part) / risk_fraction,
            "gross_r": gross / risk_fraction, "exit_ts": exit_ts, "reason": reason,
            "partial": partial_ts is not None, "risk_fraction": risk_fraction,
            "bars_held": (exit_ts - int(k15[decision_idx]["ts"])) // 900_000}


def _simulate_path(*, direction: str, planned_entry: float, planned_sl: float,
                   tp: float, decision_idx: int, k15: list, funding: list,
                   fee_rate: float, slippage: float, max_bars: int | None = None) -> dict | None:
    setup = _setup(direction, planned_entry, planned_sl, tp, decision_idx, k15, slippage)
    if setup is None:
        return None
    long, sign, fill, sl, risk_fraction = setup
    rules = PREREG["exit"]
    max_bars = max_bars or rules["max_hold_bars_15m"]
    target = abs(tp - fill)
    partial_level = fill + sign * (abs(fill - sl) + fill * rules["partial_extra_pct_of_entry"])
    active_stop, pos_sl = sl, sl
    remaining, peak, partial_ts = 1.0, 0.0, None
    legs: list[tuple[float, float]] = []
    exit_ts, reason = None, "MAX_HOLD"
    last = min(len(k15), decision_idx + max_bars)

    def protective(a, b):
        return max(a, b) if long else min(a, b)

    def stop_reason():
        return "STOP" if active_stop == sl else ("BREAKEVEN" if active_stop == fill else "TRAIL")

    for j in range(decision_idx, last):
        bar = k15[j]
        o, h, lo, c = (float(bar[k]) for k in ("o", "h", "l", "c"))
        bar_end = int(bar["ts"]) + 900_000
        if (o - active_stop) * sign <= 0:
            legs.append((_adverse(o, direction, entry=False, slip=slippage), remaining))
            exit_ts, reason = bar_end, stop_reason()
            break
        if (o - tp) * sign >= 0:
            legs.append((_adverse(o, direction, entry=False, slip=slippage), remaining))
            exit_ts, reason = bar_end, "TAKE_PROFIT"
            break
        if remaining == 1.0 and (o - partial_level) * sign >= 0:   # gap through TP1
            legs.append((_adverse(o, direction, entry=False, slip=slippage), rules["partial_fraction"]))
            remaining -= rules["partial_fraction"]
            partial_ts = bar_end
            pos_sl = fill
            active_stop = protective(active_stop, fill)
        path = (o, lo, h, c) if c >= o else (o, h, lo, c)
        for a, b in zip(path, path[1:]):
            if (b - a) * sign > 0:                      # favourable segment
                if remaining == 1.0 and (b - partial_level) * sign >= 0 > (a - partial_level) * sign:
                    legs.append((_adverse(partial_level, direction, entry=False, slip=slippage),
                                 rules["partial_fraction"]))
                    remaining -= rules["partial_fraction"]
                    partial_ts = bar_end
                    pos_sl = fill
                    active_stop = protective(active_stop, fill)
                if (b - tp) * sign >= 0:
                    legs.append((_adverse(tp, direction, entry=False, slip=slippage), remaining))
                    exit_ts, reason = bar_end, "TAKE_PROFIT"
                    break
                excursion = (b - fill) * sign
                peak = max(peak, excursion)
                if excursion >= rules["trailing_trigger_of_target"] * target:
                    new = protective(fill + sign * rules["trailing_keep_of_peak"] * peak, pos_sl)
                    if (new - pos_sl) * sign > 0:
                        pos_sl = new
                        active_stop = protective(active_stop, new)
            elif (b - active_stop) * sign <= 0:         # adverse segment through the stop
                legs.append((_adverse(active_stop, direction, entry=False, slip=slippage), remaining))
                exit_ts, reason = bar_end, stop_reason()
                break
        if exit_ts is not None:
            break
    if exit_ts is None:
        reason = "DATA_END" if last == len(k15) and last - decision_idx < max_bars else "MAX_HOLD"
        last_bar = k15[last - 1]
        legs.append((_adverse(float(last_bar["c"]), direction, entry=False, slip=slippage), remaining))
        exit_ts = int(last_bar["ts"]) + 900_000
    return _finish(direction=direction, fill=fill, sl=sl, legs=legs, partial_ts=partial_ts,
                   exit_ts=exit_ts, reason=reason, decision_idx=decision_idx, k15=k15,
                   funding=funding, fee_rate=fee_rate, risk_fraction=risk_fraction)


def _simulate_conservative(*, direction: str, planned_entry: float, planned_sl: float,
                           tp: float, decision_idx: int, k15: list, funding: list,
                           fee_rate: float, slippage: float,
                           max_bars: int | None = None) -> dict | None:
    from bot.execution_model import fee_return_fraction, funding_return_fraction
    from bot.backtest import _timestamp_index
    from bot.nexus_oos_real_replay import _price_at_ts

    rules = PREREG["exit"]
    max_bars = max_bars or rules["max_hold_bars_15m"]
    long = direction == "LONG"
    sign = 1.0 if long else -1.0
    fill = _adverse(float(k15[decision_idx]["o"]), direction, entry=True, slip=slippage)
    distance = abs(planned_entry - planned_sl)
    if fill <= 0 or distance <= 0:
        return None
    # postfill_risk_recheck: keep the more protective of the planned stop and
    # the planned distance re-anchored at the fill; the TP is not shifted.
    sl = max(planned_sl, fill - distance) if long else min(planned_sl, fill + distance)
    if (fill - sl) * sign <= 0 or (tp - fill) * sign <= 0:
        return None
    risk_fraction = distance / fill   # planned risk, see _setup
    target = abs(tp - fill)
    active_stop = sl          # most protective native stop on the exchange
    pos_sl = sl               # engine's local pos.sl (read by the partial rule)
    remaining, peak = 1.0, 0.0
    partial_ts = None
    legs: list[tuple[float, float]] = []
    reason = "MAX_HOLD"
    exit_ts = None
    last = min(len(k15), decision_idx + max_bars)

    def beyond(price, level):           # price is at/through a stop level
        return price <= level if long else price >= level

    def reached(price, level):          # price is at/through a target level
        return price >= level if long else price <= level

    def partial_threshold():
        return fill + sign * (abs(fill - pos_sl) + fill * rules["partial_extra_pct_of_entry"])

    for j in range(decision_idx, last):
        bar = k15[j]
        o, h, lo, c = (float(bar[k]) for k in ("o", "h", "l", "c"))
        fav, adv = (h, lo) if long else (lo, h)
        bar_end = int(bar["ts"]) + 900_000
        if beyond(o, active_stop) or beyond(adv, active_stop):
            px = o if beyond(o, active_stop) else active_stop
            legs.append((_adverse(px, direction, entry=False, slip=slippage), remaining))
            reason = "STOP" if active_stop == sl else ("BREAKEVEN" if active_stop == fill else "TRAIL")
            exit_ts = bar_end
            break
        if reached(fav, tp):
            if remaining == 1.0 and reached(tp, partial_threshold()):
                legs.append((_adverse(partial_threshold(), direction, entry=False, slip=slippage),
                             rules["partial_fraction"]))
                remaining -= rules["partial_fraction"]
                partial_ts = bar_end
            px = o if reached(o, tp) else tp
            legs.append((_adverse(px, direction, entry=False, slip=slippage), remaining))
            reason = "TAKE_PROFIT"
            exit_ts = bar_end
            break
        excursion = (fav - fill) * sign
        if excursion > 0:
            if remaining == 1.0 and reached(fav, partial_threshold()):
                legs.append((_adverse(partial_threshold(), direction, entry=False, slip=slippage),
                             rules["partial_fraction"]))
                remaining -= rules["partial_fraction"]
                partial_ts = bar_end
                pos_sl = fill
                active_stop = max(active_stop, fill) if long else min(active_stop, fill)
            peak = max(peak, excursion)
            if excursion >= rules["trailing_trigger_of_target"] * target:
                new = fill + sign * rules["trailing_keep_of_peak"] * peak
                new = max(new, pos_sl) if long else min(new, pos_sl)
                if (new - pos_sl) * sign > 0:
                    pos_sl = new
                    active_stop = max(active_stop, new) if long else min(active_stop, new)
    if exit_ts is None:
        reason = "DATA_END" if last == len(k15) and last - decision_idx < max_bars else "MAX_HOLD"
        last_bar = k15[last - 1]
        legs.append((_adverse(float(last_bar["c"]), direction, entry=False, slip=slippage), remaining))
        exit_ts = int(last_bar["ts"]) + 900_000
    gross = sum(sign * (px - fill) / fill * w for px, w in legs)
    fees = fee_return_fraction(fill, legs, fee_rate)
    timestamps = _timestamp_index(k15)
    funding_part, _ = funding_return_fraction(
        funding, direction, int(k15[decision_idx]["ts"]), exit_ts, fill,
        price_at_ts=lambda ts: _price_at_ts(k15, timestamps, ts),
        partial_after_ts_ms=partial_ts)
    return {"net_r": (gross - fees + funding_part) / risk_fraction,
            "gross_r": gross / risk_fraction, "exit_ts": exit_ts, "reason": reason,
            "partial": partial_ts is not None, "risk_fraction": risk_fraction,
            "bars_held": (exit_ts - int(k15[decision_idx]["ts"])) // 900_000}


def evaluate_symbol(symbol: str, k15: list, k1h: list, k4h: list, funding: list,
                    *, start_ms: int, end_ms: int, fees: list[float]) -> list[dict]:
    from bot.execution_model import slippage_rate_for_symbol
    slippage = slippage_rate_for_symbol(symbol)
    rows = []
    for i, decision_ts, sig, pullback in base.iter_signals(
            symbol, k15, k1h, k4h, start_ms=start_ms, end_ms=end_ms, horizon=1,
            windows=PREREG["windows"], min_windows=PREREG["min_windows"]):
        if isinstance(sig, Exception):
            rows.append({"symbol": symbol, "ts": decision_ts, "error": type(sig).__name__})
            continue
        direction = str(sig.direction).upper()
        params = dict(direction=direction, planned_entry=float(sig.entry),
                      planned_sl=float(sig.sl), tp=float(sig.tp), decision_idx=i,
                      k15=k15, funding=funding, slippage=slippage)
        results = {}
        for mode in MODES:
            results[mode] = {f"{fee:.4f}": simulate_live_exit(mode=mode, fee_rate=fee, **params)
                             for fee in fees}
        primary = results["OHLC_PATH"][f"{fees[0]:.4f}"]
        conservative = results["CONSERVATIVE"][f"{fees[0]:.4f}"]
        if primary is None or conservative is None:
            continue
        rows.append({
            "symbol": symbol, "ts": decision_ts, "exit_ts": primary["exit_ts"],
            "gross_r": primary["gross_r"],
            "net_r": {k: v["net_r"] for k, v in results["OHLC_PATH"].items()},
            "reason": primary["reason"], "partial": primary["partial"],
            "bars_held": primary["bars_held"],
            "conservative": {"exit_ts": conservative["exit_ts"], "reason": conservative["reason"],
                             "gross_r": conservative["gross_r"],
                             "net_r": {k: v["net_r"] for k, v in results["CONSERVATIVE"].items()}},
            **base.signal_fields(sig, symbol, decision_ts, pullback),
        })
    return rows


def _worker(job: dict) -> dict:
    base._prepare_worker_env()
    from bot import binance_public_history as hist
    base._isolate_worker()
    import logging
    logging.disable(logging.WARNING)
    from bot.runtime_bootstrap import install
    install()
    cache, symbol = Path(job["cache"]), job["symbol"]
    months = job["load_months"]
    k15 = hist.load_klines(cache, symbol, "15m", months, fetch=base._no_fetch)
    k1h = hist.load_klines(cache, symbol, "1h", months, fetch=base._no_fetch)
    k4h = hist.load_klines(cache, symbol, "4h", months, fetch=base._no_fetch)
    funding = hist.load_funding(cache, symbol, months, fetch=base._no_fetch)
    start_ms, end_ms = base._month_bounds(job["months"])
    fees = [PREREG["primary_fee"]] + PREREG["sensitivity_fees"]
    started = time.perf_counter()
    rows = evaluate_symbol(symbol, k15, k1h, k4h, funding, start_ms=start_ms,
                           end_ms=end_ms, fees=fees)
    return {"symbol": symbol, "rows": rows, "elapsed_s": time.perf_counter() - started}


def all_months() -> list[str]:
    return PREREG["warmup_months"] + PREREG["holdout_months"] + PREREG["descriptive_months"]


def combined_verdict(path: str, conservative: str) -> str:
    """PASS needs both resolutions; a path-only pass is inconclusive."""
    if path == "INSUFFICIENT_EVIDENCE":
        return "INSUFFICIENT_EVIDENCE"
    if path == "RAW_SIGNAL_EDGE_FAIL":
        return "EXIT_MANAGEMENT_FAIL"
    if conservative == "RAW_SIGNAL_EDGE_PASS":
        return "EXIT_MANAGEMENT_PASS"
    return "EXIT_MANAGEMENT_INCONCLUSIVE_INTRABAR"


def build_report(rows: list[dict]) -> dict:
    """Same statistics and gate as RAW_SIGNAL_EDGE_V1, on this study's windows."""
    from collections import Counter
    fee_keys = [f"{f:.4f}" for f in [PREREG["primary_fee"]] + PREREG["sensitivity_fees"]]
    primary_fee = f"{PREREG['primary_fee']:.4f}"
    windows = {"holdout": base._month_bounds(PREREG["holdout_months"]),
               "descriptive": base._month_bounds(PREREG["descriptive_months"])}
    report = {"prereg_id": PREREG["id"], "prereg_sha256": prereg_digest(),
              "errors": sum(1 for r in rows if "error" in r), "windows": {}, **base.AUTHORITY}
    for name, (lo, hi) in windows.items():
        window_rows = [r for r in rows if "error" not in r and lo <= r["ts"] < hi]
        out = {}
        for population in base.POPULATIONS:
            chosen = base.select_non_overlapping(
                [r for r in window_rows if base._in_population(r, population)])
            pop = {"by_fee": {k: base.summarize(chosen, k) for k in fee_keys}}
            pop["gross"] = {"trades": len(chosen), "mean_r": (
                sum(r["gross_r"] for r in chosen) / len(chosen)) if chosen else None}
            pop["exit_reasons"] = dict(Counter(r["reason"] for r in chosen))
            pop["partial_rate"] = (sum(r["partial"] for r in chosen) / len(chosen)) if chosen else None
            pop["median_bars_held"] = (sorted(r["bars_held"] for r in chosen)[len(chosen) // 2]
                                       if chosen else None)
            pop["by_direction"] = base.breakdown(chosen, primary_fee, lambda r: r["direction"])
            pop["by_entry_type"] = base.breakdown(chosen, primary_fee, lambda r: r["entry_type"])
            pop["by_symbol"] = base.breakdown(chosen, primary_fee, lambda r: r["symbol"])
            pop["by_month"] = base.breakdown(
                chosen, primary_fee, lambda r: time.strftime("%Y-%m", time.gmtime(r["ts"] / 1000)))
            out[population] = pop
        report["windows"][name] = out
    for name, (lo, hi) in windows.items():
        window_rows = [r for r in rows if "error" not in r and lo <= r["ts"] < hi]
        cons_rows = [dict(r, net_r=r["conservative"]["net_r"], exit_ts=r["conservative"]["exit_ts"],
                          gross_r=r["conservative"]["gross_r"]) for r in window_rows]
        chosen = base.select_non_overlapping(
            [r for r in cons_rows if base._in_population(r, PREREG["primary_population"])])
        report["windows"][name]["CONSERVATIVE_" + PREREG["primary_population"]] = {
            "by_fee": {k: base.summarize(chosen, k) for k in fee_keys}}
    basis = report["windows"]["holdout"][PREREG["primary_population"]]["by_fee"][primary_fee]
    cons = report["windows"]["holdout"]["CONSERVATIVE_" + PREREG["primary_population"]]["by_fee"][primary_fee]
    report["verdict"] = combined_verdict(base.verdict(basis), base.verdict(cons))
    report["conservative_basis"] = cons
    report["verdict_basis"] = {"window": "holdout", "population": PREREG["primary_population"],
                               "fee": PREREG["primary_fee"], **basis}
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", default="research_cache/binance_vision")
    parser.add_argument("--out", default="artifacts/exit_management_v1")
    parser.add_argument("--symbols", nargs="+", default=PREREG["symbols"])
    parser.add_argument("--shard")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--no-report", action="store_true")
    parser.add_argument("--aggregate", nargs="+", type=Path)
    args = parser.parse_args(argv)
    out = Path(args.out)
    if args.aggregate:
        rows = base.read_rows(args.aggregate)
        report = build_report(rows)
        report["code_sha"] = base._git_head()
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps({"verdict": report["verdict"], **report["verdict_basis"]}, sort_keys=True))
        return 0
    from bot import binance_public_history as hist
    cache = Path(args.cache)
    symbols = base.shard(args.symbols, args.shard)
    if not args.skip_download:
        for symbol in symbols:
            for month in all_months():
                for interval in ("15m", "1h", "4h"):
                    hist.ensure_archive(cache, hist.kline_archive(symbol, interval, month))
                hist.ensure_archive(cache, hist.funding_archive(symbol, month))
            print(f"downloaded {symbol}", flush=True)
    if args.download_only:
        return 0
    import multiprocessing as mp
    months = PREREG["holdout_months"] + PREREG["descriptive_months"]
    jobs = [{"symbol": s, "cache": str(cache), "months": months, "load_months": all_months()}
            for s in symbols]
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    with mp.get_context("spawn").Pool(max(1, min(args.workers, len(jobs))), maxtasksperchild=1) as pool:
        for result in pool.imap_unordered(_worker, jobs):
            rows.extend(result["rows"])
            print(f"{result['symbol']}: {len(result['rows'])} signals in {result['elapsed_s']:.0f}s", flush=True)
    rows.sort(key=lambda r: (r["ts"], r["symbol"]))
    with open(out / "signals.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    if not args.no_report:
        report = build_report(rows)
        (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps({"verdict": report["verdict"], **report["verdict_basis"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
