"""RAW_SIGNAL_EDGE_V1: does the production strategy's raw signal pay its costs?

Research only. Replays the *effective* production analyzer stack (the
runtime_bootstrap wrappers over ``Analyzer.analyze_mtf``, called exactly as
the HARD_GATE_SHADOW scan calls it) over Binance USD-M public history, one
closed 15m boundary at a time, and simulates every signal with fees,
slippage and funding. NEXUS is not called: historical OI, order book and news
are not available, so this measures the strategy signal before the filter.

Isolation: the replay runs in worker processes with the wall clock frozen at
each decision boundary and every non-loopback socket blocked by an audit
hook. No credentials, database, engine, exchange client or order is used.
The pre-registration (PREREG) is fixed in code before any data is read; the
verdict is computed only on the holdout window.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PREREG = {
    "id": "RAW_SIGNAL_EDGE_V1",
    "symbols": [
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
        "ADAUSDT", "DOGEUSDT", "TRXUSDT", "LINKUSDT", "AVAXUSDT",
        "DOTUSDT", "LTCUSDT", "NEARUSDT", "ATOMUSDT", "SUIUSDT",
        "PEOPLEUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "UNIUSDT",
        "AAVEUSDT", "FILUSDT", "INJUSDT", "SEIUSDT", "ETCUSDT",
    ],
    "warmup_months": ["2025-09"],
    "discovery_months": ["2025-10", "2025-11", "2025-12", "2026-01",
                         "2026-02", "2026-03", "2026-04", "2026-05"],
    "holdout_months": ["2026-06", "2026-07", "2026-08", "2026-09"],
    "windows": {"15m": 200, "1h": 100, "4h": 120},
    "min_windows": {"15m": 60, "1h": 40, "4h": 20},
    "scoring_env": {"MIN_ENTRY_SCORE": "60", "MIN_RR_RATIO": "2.0",
                    "MIN_VOLUME_MULT": "0.5", "FEE_MULTIPLIER": "2.0"},
    "primary_population": "FUNNEL",
    "primary_fee": 0.0006,
    "sensitivity_fees": [0.0004, 0.0005],
    "outcome_bars_15m": 40,
    "selection": "NON_OVERLAPPING_PER_SYMBOL",
    "cluster": "SYMBOL_UTC_DAY",
    "bootstrap": {"replicates": 10_000, "seed": 20261014, "lower_quantile": 0.05},
    "gate": {"min_trades": 150, "min_symbols": 10},
}
POPULATIONS = ("ALL", "PULLBACK_PASS", "FUNNEL")
AUTHORITY = {"research_only": True, "nexus_called": False, "promotion_allowed": False,
             "live_allowed": False, "decision_effect": "NONE", "execution_effect": "NONE"}
ROOT = Path(__file__).resolve().parents[1]
_CLOCK = [0.0]


def prereg_digest() -> str:
    raw = json.dumps(PREREG, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _month_bounds(months: list[str]) -> tuple[int, int]:
    first = datetime.strptime(months[0], "%Y-%m").replace(tzinfo=timezone.utc)
    y, m = (int(x) for x in months[-1].split("-"))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    end = datetime(y, m, 1, tzinfo=timezone.utc)
    return int(first.timestamp() * 1000), int(end.timestamp() * 1000)


def session_for(ts_ms: int) -> str:
    hour = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour
    return "ASIA" if hour < 8 else ("LONDON" if hour < 16 else "NEW_YORK")


def funnel_flags(sig, symbol: str, decision_ts: int, pullback: str, min_score: int) -> dict:
    """Pre-NEXUS production funnel, evaluated at the decision time (not now)."""
    from bot.engine import TradingEngine
    penalty = int(TradingEngine._SESSION_PENALTY.get(session_for(decision_ts), {}).get(symbol, 0))
    adjusted = max(0, int(sig.score) + penalty)
    regime = str(getattr(sig, "regime", "RANGING"))
    params = TradingEngine._REGIME_PARAMS.get(regime, TradingEngine._REGIME_PARAMS["RANGING"])
    regime_ok = str(sig.direction) in params.get("allowed_sides", ["LONG", "SHORT"])
    pullback_pass = pullback != "BLOCKED"
    return {
        "pullback_pass": pullback_pass,
        "adjusted_score": adjusted,
        "regime_allows": regime_ok,
        "expected_pnl_positive": float(getattr(sig, "expected_pnl", 0.0) or 0.0) > 0,
        "funnel_pass": (pullback_pass and adjusted >= min_score and regime_ok
                        and float(getattr(sig, "expected_pnl", 0.0) or 0.0) > 0),
    }


def _isolate_worker() -> None:
    """Freeze the clock and block outbound sockets in this research process."""
    import socket

    def hook(event, args):
        if event == "socket.connect" and args:
            addr = args[1]
            host = addr[0] if isinstance(addr, tuple) else addr
            if host not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
                raise PermissionError(f"network blocked in research worker: {host}")
    sys.addaudithook(hook)
    time.time = lambda: _CLOCK[0]
    socket.setdefaulttimeout(1)


def evaluate_symbol(symbol: str, k15: list, k1h: list, k4h: list, funding: list,
                    *, start_ms: int, end_ms: int, fees: list[float]) -> list[dict]:
    """Every production-stack signal for ``symbol`` with simulated outcomes."""
    from bot.backtest import _closed_window_by_ts, _timestamp_index
    from bot.config import cfg
    from bot.execution_model import slippage_rate_for_symbol
    from bot.hard_gate_shadow_context import scope
    from bot.nexus_oos_real_replay import _simulate_net_r
    from bot.strategy import Analyzer

    analyzer = Analyzer()
    ts15, ts1h, ts4h = _timestamp_index(k15), _timestamp_index(k1h), _timestamp_index(k4h)
    slippage = slippage_rate_for_symbol(symbol)
    horizon = PREREG["outcome_bars_15m"]
    wins, mins = PREREG["windows"], PREREG["min_windows"]
    rows = []
    for i, bar in enumerate(k15):
        decision_ts = int(bar["ts"])
        if not start_ms <= decision_ts < end_ms or i + horizon > len(k15):
            continue
        w15 = _closed_window_by_ts(k15, ts15, decision_ts, 15, wins["15m"])
        w1h = _closed_window_by_ts(k1h, ts1h, decision_ts, 60, wins["1h"])
        w4h = _closed_window_by_ts(k4h, ts4h, decision_ts, 240, wins["4h"])
        if len(w15) < mins["15m"] or len(w1h) < mins["1h"] or len(w4h) < mins["4h"]:
            continue
        _CLOCK[0] = decision_ts / 1000.0 + 1.0
        try:
            with scope() as context:
                sig = analyzer.analyze_mtf(symbol, list(w15), list(w1h), list(w4h),
                                           min_score=cfg.MIN_ENTRY_SCORE,
                                           fee_mult=cfg.FEE_MULTIPLIER,
                                           vol_mult=cfg.MIN_VOLUME_MULT)
                sig = sig if sig is not None else context.signal
                pullback = context.pullback
        except Exception as exc:
            rows.append({"symbol": symbol, "ts": decision_ts, "error": type(exc).__name__})
            continue
        if sig is None:
            continue
        direction = str(sig.direction).upper()
        outcome = {}
        exit_ts = None
        for fee in fees:
            detail = _simulate_net_r(
                direction=direction, signal_entry=float(sig.entry), signal_sl=float(sig.sl),
                signal_tp=float(sig.tp),
                signal_tp1=float(getattr(sig, "tp1", sig.tp) or sig.tp),
                signal_tp2=float(getattr(sig, "tp2", sig.tp) or sig.tp),
                decision_idx=i, decision_ts=decision_ts, klines_15=k15,
                funding_events=funding, fee_rate=fee, slippage_rate=slippage,
                return_detail=True,
            )
            if detail is None:
                break
            outcome[f"{fee:.4f}"] = detail["net_r"]
            exit_ts = int(detail["exit_ts"])
            gross_r = detail["gross"] / detail["risk_fraction"]
        if exit_ts is None:
            continue
        rows.append({
            "symbol": symbol, "ts": decision_ts, "exit_ts": exit_ts,
            "direction": direction, "entry_type": str(getattr(sig, "entry_type", "")),
            "regime": str(getattr(sig, "regime", "")), "score": int(sig.score),
            "rr": float(sig.rr), "pullback": str(pullback), "gross_r": gross_r,
            "net_r": outcome,
            **funnel_flags(sig, symbol, decision_ts, str(pullback), int(cfg.MIN_ENTRY_SCORE)),
        })
    return rows


FORCED_ENV = {"EXCHANGE": "binance", "PAPER_TRADE": "true", "NEXUS_TELEGRAM": "false",
              "BGX_RUNTIME_TRUTH_ENABLED": "false", "PYTHONHASHSEED": "0"}
_SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASS", "DATABASE", "DSN", "PG",
                   "TELEGRAM", "WEBHOOK", "COOKIE", "AUTH")


def _prepare_worker_env() -> None:
    for name in list(os.environ):
        if any(marker in name.upper() for marker in _SECRET_MARKERS):
            del os.environ[name]
    os.environ.update(FORCED_ENV)
    os.environ.update(PREREG["scoring_env"])


def _worker(job: dict) -> dict:
    _prepare_worker_env()
    from bot import binance_public_history as hist
    _isolate_worker()
    import logging
    logging.disable(logging.WARNING)
    from bot.runtime_bootstrap import install
    install()
    cache = Path(job["cache"])
    months = PREREG["warmup_months"] + job["months"]
    symbol = job["symbol"]
    k15 = hist.load_klines(cache, symbol, "15m", months, fetch=_no_fetch)
    k1h = hist.load_klines(cache, symbol, "1h", months, fetch=_no_fetch)
    k4h = hist.load_klines(cache, symbol, "4h", months, fetch=_no_fetch)
    funding = hist.load_funding(cache, symbol, months, fetch=_no_fetch)
    start_ms, end_ms = _month_bounds(job["months"])
    fees = [PREREG["primary_fee"]] + PREREG["sensitivity_fees"]
    started = time.perf_counter()
    rows = evaluate_symbol(symbol, k15, k1h, k4h, funding, start_ms=start_ms, end_ms=end_ms, fees=fees)
    return {"symbol": symbol, "rows": rows, "elapsed_s": time.perf_counter() - started}


def _no_fetch(url):
    raise PermissionError(f"worker must read the verified cache only: {url}")


# ---------------------------------------------------------------- statistics

def _in_population(row: dict, population: str) -> bool:
    if "error" in row or float(row.get("rr", 0)) < float(PREREG["scoring_env"]["MIN_RR_RATIO"]):
        return False
    if population == "ALL":
        return True
    if population == "PULLBACK_PASS":
        return bool(row["pullback_pass"])
    return bool(row["funnel_pass"])


def select_non_overlapping(rows: list[dict]) -> list[dict]:
    """One open simulated trade per symbol: the next only after the last exit."""
    chosen, busy_until = [], {}
    for row in sorted(rows, key=lambda r: (r["ts"], r["symbol"])):
        if row["ts"] >= busy_until.get(row["symbol"], -1):
            chosen.append(row)
            busy_until[row["symbol"]] = row["exit_ts"]
    return chosen


def _cluster(row: dict) -> tuple:
    return (row["symbol"], row["ts"] // 86_400_000)


def cluster_bootstrap_lower(values_by_cluster: dict, *, replicates: int, seed: int,
                            quantile: float) -> float | None:
    keys = sorted(values_by_cluster)
    if not keys:
        return None
    rng = random.Random(seed)
    sums = [sum(values_by_cluster[k]) for k in keys]
    counts = [len(values_by_cluster[k]) for k in keys]
    means = []
    for _ in range(replicates):
        s = n = 0
        for _ in keys:
            j = rng.randrange(len(keys))
            s += sums[j]
            n += counts[j]
        means.append(s / n if n else -math.inf)
    means.sort()
    return means[int(math.floor(quantile * replicates))]


def summarize(rows: list[dict], fee_key: str) -> dict:
    values = [r["net_r"][fee_key] for r in rows if fee_key in r["net_r"]]
    if not values:
        return {"trades": 0}
    by_cluster = defaultdict(list)
    by_symbol = defaultdict(list)
    for r in rows:
        if fee_key in r["net_r"]:
            by_cluster[_cluster(r)].append(r["net_r"][fee_key])
            by_symbol[r["symbol"]].append(r["net_r"][fee_key])
    total = sum(values)
    loso = {}
    for sym, vals in by_symbol.items():
        rest = len(values) - len(vals)
        loso[sym] = (total - sum(vals)) / rest if rest else None
    wins = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    b = PREREG["bootstrap"]
    return {
        "trades": len(values), "symbols": len(by_symbol), "clusters": len(by_cluster),
        "mean_r": total / len(values), "sum_r": total,
        "win_rate": sum(1 for v in values if v > 0) / len(values),
        "profit_factor": (wins / losses) if losses > 0 else None,
        "bootstrap_lower_r": cluster_bootstrap_lower(
            by_cluster, replicates=b["replicates"], seed=b["seed"], quantile=b["lower_quantile"]),
        "loso_min_mean_r": min((v for v in loso.values() if v is not None), default=None),
    }


def breakdown(rows: list[dict], fee_key: str, field) -> dict:
    groups = defaultdict(list)
    for r in rows:
        if fee_key in r["net_r"]:
            groups[field(r)].append(r["net_r"][fee_key])
    return {str(k): {"trades": len(v), "mean_r": sum(v) / len(v)} for k, v in sorted(groups.items())}


def verdict(summary: dict) -> str:
    g = PREREG["gate"]
    if summary.get("trades", 0) < g["min_trades"] or summary.get("symbols", 0) < g["min_symbols"]:
        return "INSUFFICIENT_EVIDENCE"
    ok = (summary["mean_r"] > 0 and (summary["bootstrap_lower_r"] or -1) > 0
          and (summary["loso_min_mean_r"] or -1) > 0)
    return "RAW_SIGNAL_EDGE_PASS" if ok else "RAW_SIGNAL_EDGE_FAIL"


def build_report(rows: list[dict]) -> dict:
    fee_keys = [f"{f:.4f}" for f in [PREREG["primary_fee"]] + PREREG["sensitivity_fees"]]
    primary_fee = f"{PREREG['primary_fee']:.4f}"
    windows = {"discovery": _month_bounds(PREREG["discovery_months"]),
               "holdout": _month_bounds(PREREG["holdout_months"])}
    report = {"prereg_id": PREREG["id"], "prereg_sha256": prereg_digest(),
              "errors": sum(1 for r in rows if "error" in r), "windows": {}, **AUTHORITY}
    for name, (lo, hi) in windows.items():
        window_rows = [r for r in rows if "error" not in r and lo <= r["ts"] < hi]
        out = {}
        for population in POPULATIONS:
            chosen = select_non_overlapping([r for r in window_rows if _in_population(r, population)])
            pop = {"by_fee": {k: summarize(chosen, k) for k in fee_keys}}
            pop["gross"] = {"trades": len(chosen),
                            "mean_r": (sum(r["gross_r"] for r in chosen) / len(chosen)) if chosen else None}
            pop["by_direction"] = breakdown(chosen, primary_fee, lambda r: r["direction"])
            pop["by_entry_type"] = breakdown(chosen, primary_fee, lambda r: r["entry_type"])
            pop["by_regime"] = breakdown(chosen, primary_fee, lambda r: r["regime"])
            pop["by_symbol"] = breakdown(chosen, primary_fee, lambda r: r["symbol"])
            pop["by_month"] = breakdown(
                chosen, primary_fee,
                lambda r: datetime.fromtimestamp(r["ts"] / 1000, tz=timezone.utc).strftime("%Y-%m"))
            out[population] = pop
        report["windows"][name] = out
    holdout_primary = report["windows"]["holdout"][PREREG["primary_population"]]["by_fee"][primary_fee]
    report["verdict"] = verdict(holdout_primary)
    report["verdict_basis"] = {"window": "holdout", "population": PREREG["primary_population"],
                               "fee": PREREG["primary_fee"], **holdout_primary}
    return report


# ----------------------------------------------------------------------- CLI

def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def download(cache: Path, symbols: list[str]) -> None:
    from bot import binance_public_history as hist
    months = PREREG["warmup_months"] + PREREG["discovery_months"] + PREREG["holdout_months"]
    for symbol in symbols:
        for month in months:
            for interval in ("15m", "1h", "4h"):
                hist.ensure_archive(cache, hist.kline_archive(symbol, interval, month))
            hist.ensure_archive(cache, hist.funding_archive(symbol, month))
        print(f"downloaded {symbol}", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", default="research_cache/binance_vision")
    parser.add_argument("--out", default="artifacts/raw_signal_edge_v1")
    parser.add_argument("--symbols", nargs="+", default=PREREG["symbols"])
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--skip-download", action="store_true")
    args = parser.parse_args(argv)
    cache, out = Path(args.cache), Path(args.out)
    if not args.skip_download:
        download(cache, args.symbols)
    if args.download_only:
        return 0
    import multiprocessing as mp
    months = PREREG["discovery_months"] + PREREG["holdout_months"]
    jobs = [{"symbol": s, "cache": str(cache), "months": months} for s in args.symbols]
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    ctx = mp.get_context("spawn")
    with ctx.Pool(args.workers, maxtasksperchild=1) as pool:
        for result in pool.imap_unordered(_worker, jobs):
            rows.extend(result["rows"])
            print(f"{result['symbol']}: {len(result['rows'])} signals in {result['elapsed_s']:.0f}s", flush=True)
    rows.sort(key=lambda r: (r["ts"], r["symbol"]))
    with open(out / "signals.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    report = build_report(rows)
    report["code_sha"] = _git_head()
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], **report["verdict_basis"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
