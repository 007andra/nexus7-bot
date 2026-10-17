"""TREND_LONG_V1: two long-only trend rules on history no study here has used.

Research only. Hypotheses, each with its own pre-registered verdict:

* H1: B2 daily channel breakout, long only, only while BTC closes above its
  200-day average. Suggested by TREND_BREAKOUT_V1 (B2 gains came from longs),
  so the decisive test is on untouched data.
* H2: weekly momentum. Every Monday open, buy the 3 pairs with the highest
  positive 28-day return; stop 2 x ATR(20); exit at the next Monday open.

Decisive window: Binance SPOT daily klines 2017-08..2021-09 (no study here read
them), which include the 2018 bear market. Confirmation window: USD-M futures
daily klines 2022-01..2026-09 (already read by earlier studies, so it can only
veto: the mean must be positive there too). Archives verified by SHA-256; no
credentials, database, engine or order.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from bot import binance_public_history as hist
from bot import trend_breakout_study_v1 as base

DAY_MS = 86_400_000

PREREG = {
    "id": "TREND_LONG_V1",
    "frozen_on": "2026-10-17",
    "symbols": list(base.PREREG["symbols"]),
    "interval": "1d",
    "decisive": {"source": "SPOT", "warmup_months": hist.months_between("2017-08", "2017-12"),
                 "months": hist.months_between("2018-01", "2021-09"),
                 "funding": {"assumed_rate": 0.0002, "settlements_per_day": 3, "sign": "ADVERSE"}},
    "confirmation": {"source": "USDM", "warmup_months": hist.months_between("2021-04", "2021-12"),
                     "months": hist.months_between("2022-01", "2026-09"), "funding": "REAL"},
    "fee": 0.0006,
    "slippage": "execution_model.slippage_rate_for_symbol, every fill",
    "H1": {"entry_days": 20, "exit_days": 10, "atr_days": 20, "stop_atr_mult": 2.0,
           "filter": "BTCUSDT close > SMA(200) of BTCUSDT closes, on the signal day",
           "direction": "LONG_ONLY"},
    "H2": {"lookback_days": 28, "top_n": 3, "require_positive_return": True,
           "atr_days": 20, "stop_atr_mult": 2.0, "rebalance": "MONDAY_OPEN_UTC",
           "exit": "STOP_OR_NEXT_MONDAY_OPEN", "costs": "every week is a full round trip"},
    "cluster": "UTC_MONTH",
    "bootstrap": {"replicates": 10_000, "seed": 20261017,
                  "lower_quantile": 0.025, "why": "two hypotheses, Bonferroni of 5%"},
    "gate": {"min_trades": 150, "min_symbols": 8},
    "criterion": "decisive: gate, mean > 0, 2.5% lower > 0, every LOSO > 0; AND confirmation mean > 0",
}
AUTHORITY = {"research_only": True, "live_allowed": False,
             "decision_effect": "NONE", "execution_effect": "NONE"}
ROOT = Path(__file__).resolve().parents[1]


def prereg_digest() -> str:
    raw = json.dumps(PREREG, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- days

def daily_bars(rows: list[dict]) -> list[dict]:
    """Sorted, de-duplicated daily bars; the run is cut at the first gap."""
    by_day = {}
    for bar in rows:
        by_day[bar["ts"] // DAY_MS] = {**bar, "day": bar["ts"] // DAY_MS}
    return [by_day[d] for d in sorted(by_day)]


def _consecutive(days: list[dict], i: int, n: int) -> bool:
    if i - n < 0:
        return False
    window = days[i - n:i + 1]
    return all(b["day"] - a["day"] == 1 for a, b in zip(window, window[1:]))


def _atr(days: list[dict], i: int, n: int) -> float:
    total = 0.0
    for j in range(i - n + 1, i + 1):
        d, p = days[j], days[j - 1]
        total += max(d["h"] - d["l"], abs(d["h"] - p["c"]), abs(d["l"] - p["c"]))
    return total / n


def btc_filter(btc_days: list[dict]) -> dict[int, bool]:
    """day -> BTC close above its 200-day simple average (needs 200 consecutive days)."""
    out = {}
    for i in range(len(btc_days)):
        if _consecutive(btc_days, i, 199):
            sma = sum(d["c"] for d in btc_days[i - 199:i + 1]) / 200
            out[btc_days[i]["day"]] = btc_days[i]["c"] > sma
    return out


# ---------------------------------------------------------------- costs

def _fill(price: float, is_entry: bool, slip: float) -> float:
    from bot.execution_model import adverse_fill
    return adverse_fill(price, "LONG", is_entry=is_entry, slippage_rate=slip)


def trade_row(hyp, symbol, entry_bar_ts, entry_px, exit_ts, exit_px, distance, reason, slip, funding):
    """Net R of one long trade; ``funding`` is ('REAL', events) or ('ASSUMED', rate)."""
    from bot.execution_model import fee_return_fraction, funding_return_fraction

    fill = _fill(entry_px, True, slip)
    out = _fill(exit_px, False, slip)
    risk = distance / fill
    gross = (out - fill) / fill
    if funding[0] == "REAL":
        fund, _ = funding_return_fraction(funding[1], "LONG", entry_bar_ts, exit_ts, fill)
    else:
        settlements = max(0, int((exit_ts - entry_bar_ts) // (DAY_MS // 3)))
        fund = -funding[1] * settlements
    fee = PREREG["fee"]
    net = (gross - fee_return_fraction(fill, [(out, 1.0)], fee) + fund) / risk
    month = datetime.fromtimestamp(entry_bar_ts / 1000, tz=timezone.utc).strftime("%Y-%m")
    return {"hypothesis": hyp, "symbol": symbol, "ts": entry_bar_ts, "exit_ts": exit_ts,
            "reason": reason, "gross_r": gross / risk, "net_r": net, "funding_r": fund / risk,
            "risk_fraction": risk, "cluster": month}


# ---------------------------------------------------------------- H1

def simulate_h1(symbol, days, btc_up, slip, funding):
    spec = PREREG["H1"]
    need = max(spec["entry_days"], spec["exit_days"], spec["atr_days"] + 1)
    rows, pos, pending_entry, pending_exit = [], None, None, False
    for i, today in enumerate(days):
        consecutive = i > 0 and today["day"] - days[i - 1]["day"] == 1
        if pos is not None and not consecutive:
            prev = days[i - 1]
            rows.append(trade_row("H1", symbol, pos["ts"], pos["px"], prev["ts"] + DAY_MS - 1,
                                  prev["c"], pos["distance"], "DATA_GAP", slip, funding))
            pos, pending_exit = None, False
        if pending_entry is not None and pos is None and consecutive:
            pos = {"ts": today["ts"], "px": today["o"], "distance": pending_entry,
                   "stop": today["o"] - pending_entry}
        pending_entry = None
        if pos is not None and pending_exit:
            rows.append(trade_row("H1", symbol, pos["ts"], pos["px"], today["ts"], today["o"],
                                  pos["distance"], "CHANNEL_EXIT", slip, funding))
            pos, pending_exit = None, False
        if pos is not None:
            if today["ts"] > pos["ts"] and today["o"] <= pos["stop"]:
                rows.append(trade_row("H1", symbol, pos["ts"], pos["px"], today["ts"] + DAY_MS - 1,
                                      today["o"], pos["distance"], "STOP_GAP", slip, funding))
                pos = None
            elif today["l"] <= pos["stop"]:
                rows.append(trade_row("H1", symbol, pos["ts"], pos["px"], today["ts"] + DAY_MS - 1,
                                      pos["stop"], pos["distance"], "STOP", slip, funding))
                pos = None
        if not _consecutive(days, i, need):
            continue
        if pos is not None:
            low = min(d["l"] for d in days[i - spec["exit_days"]:i])
            if today["c"] < low:
                pending_exit = True
        elif btc_up.get(today["day"]) is True:
            high = max(d["h"] for d in days[i - spec["entry_days"]:i])
            distance = spec["stop_atr_mult"] * _atr(days, i, spec["atr_days"])
            if distance > 0 and today["c"] > high:
                pending_entry = distance
    return rows  # an open position at the data end is not counted


# ---------------------------------------------------------------- H2

def simulate_h2(days_by_symbol, slips, fundings):
    """Weekly top-3 momentum across symbols; each week is an independent trade."""
    spec = PREREG["H2"]
    index = {s: {d["day"]: k for k, d in enumerate(days)} for s, days in days_by_symbol.items()}
    all_days = sorted({d["day"] for days in days_by_symbol.values() for d in days})
    mondays = [d for d in all_days if datetime.fromtimestamp(d * 86_400, tz=timezone.utc).weekday() == 0]
    rows = []
    for monday, next_monday in zip(mondays, mondays[1:]):
        if next_monday - monday != 7:
            continue
        ranked = []
        for symbol, days in days_by_symbol.items():
            k = index[symbol].get(monday)
            if k is None or not _consecutive(days, k, max(spec["lookback_days"], spec["atr_days"]) + 1):
                continue
            prev = days[k - 1]  # Sunday close: the last information before the Monday open
            ret = prev["c"] / days[k - 1 - spec["lookback_days"]]["c"] - 1.0
            if spec["require_positive_return"] and ret <= 0:
                continue
            ranked.append((ret, symbol, k))
        ranked.sort(key=lambda t: (-t[0], t[1]))
        for ret, symbol, k in ranked[:spec["top_n"]]:
            days = days_by_symbol[symbol]
            distance = spec["stop_atr_mult"] * _atr(days, k - 1, spec["atr_days"])
            if distance <= 0:
                continue
            entry = days[k]["o"]
            stop = entry - distance
            exit_px, exit_ts, reason = None, None, None
            for j in range(k, len(days)):
                d = days[j]
                if d["day"] >= next_monday:
                    if d["day"] == next_monday:
                        exit_px, exit_ts, reason = d["o"], d["ts"], "WEEKLY_EXIT"
                    break
                if j > k and d["o"] <= stop:
                    exit_px, exit_ts, reason = d["o"], d["ts"] + DAY_MS - 1, "STOP_GAP"
                    break
                if d["l"] <= stop:
                    exit_px, exit_ts, reason = stop, d["ts"] + DAY_MS - 1, "STOP"
                    break
            if exit_px is None:
                continue  # gap or data end inside the week: not counted
            rows.append(trade_row("H2", symbol, days[k]["ts"], entry, exit_ts, exit_px, distance,
                                  reason, slips[symbol], fundings[symbol]))
    return rows


# ---------------------------------------------------------------- statistics

def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"trades": 0, "symbols": 0}
    values = [r["net_r"] for r in rows]
    by_cluster, by_symbol = defaultdict(list), defaultdict(list)
    for r in rows:
        by_cluster[r["cluster"]].append(r["net_r"])
        by_symbol[r["symbol"]].append(r["net_r"])
    total = sum(values)
    loso = [(total - sum(v)) / (len(values) - len(v)) for v in by_symbol.values() if len(values) > len(v)]
    b = PREREG["bootstrap"]
    return {"trades": len(values), "symbols": len(by_symbol), "clusters": len(by_cluster),
            "mean_r": total / len(values),
            "win_rate": sum(1 for v in values if v > 0) / len(values),
            "bootstrap_lower_r": base.cluster_bootstrap_lower(
                by_cluster, replicates=b["replicates"], seed=b["seed"], quantile=b["lower_quantile"]),
            "loso_min_mean_r": min(loso) if loso else None}


def verdict(hyp: str, decisive: dict, confirmation: dict) -> str:
    g = PREREG["gate"]
    if decisive.get("trades", 0) < g["min_trades"] or decisive.get("symbols", 0) < g["min_symbols"]:
        return f"{hyp}_INSUFFICIENT_EVIDENCE"
    ok = (decisive["mean_r"] > 0 and (decisive["bootstrap_lower_r"] or -1) > 0
          and (decisive["loso_min_mean_r"] or -1) > 0
          and confirmation.get("trades", 0) > 0 and confirmation["mean_r"] > 0)
    return f"{hyp}_{'PASS' if ok else 'FAIL'}"


def _breakdown(rows, field):
    groups = defaultdict(list)
    for r in rows:
        groups[field(r)].append(r["net_r"])
    return {str(k): {"trades": len(v), "mean_r": sum(v) / len(v)} for k, v in sorted(groups.items())}


def build_report(rows_by_window: dict) -> dict:
    report = {"prereg_id": PREREG["id"], "prereg_sha256": prereg_digest(), "hypotheses": {}, **AUTHORITY}
    for hyp in ("H1", "H2"):
        out = {}
        for window in ("decisive", "confirmation"):
            lo, hi = base.month_bounds(PREREG[window]["months"])
            rows = [r for r in rows_by_window.get(window, []) if r["hypothesis"] == hyp and lo <= r["ts"] < hi]
            out[window] = {"summary": summarize(rows),
                           "by_year": _breakdown(rows, lambda r: r["cluster"][:4]),
                           "by_symbol": _breakdown(rows, lambda r: r["symbol"]),
                           "exit_reasons": _breakdown(rows, lambda r: r["reason"]),
                           "gross_mean_r": (sum(r["gross_r"] for r in rows) / len(rows)) if rows else None}
        out["verdict"] = verdict(hyp, out["decisive"]["summary"], out["confirmation"]["summary"])
        report["hypotheses"][hyp] = out
    return report


# ---------------------------------------------------------------- data

def _archive(window: str, symbol: str, month: str) -> str:
    if PREREG[window]["source"] == "SPOT":
        return hist.spot_kline_archive(symbol, PREREG["interval"], month)
    return hist.kline_archive(symbol, PREREG["interval"], month)


def download(cache: Path, window: str, *, fetch=hist._fetch) -> dict:
    spec = PREREG[window]
    months = spec["warmup_months"] + spec["months"]
    avail = {}
    for symbol in PREREG["symbols"]:
        absent = {"klines": [], "funding": []}
        for month in months:
            jobs = [("klines", _archive(window, symbol, month))]
            if spec["source"] == "USDM":
                jobs.append(("funding", hist.funding_archive(symbol, month)))
            for kind, archive in jobs:
                try:
                    hist.ensure_archive(cache, archive, fetch=fetch)
                except urllib.error.HTTPError as exc:
                    if exc.code != 404:
                        raise
                    absent[kind].append(month)
        avail[symbol] = absent
        print(f"downloaded {window} {symbol} klines_absent={len(absent['klines'])}", flush=True)
    return avail


def _no_fetch(url):
    raise PermissionError(f"simulation reads the verified cache only: {url}")


def load_window(cache: Path, window: str, avail: dict):
    spec = PREREG[window]
    months = spec["warmup_months"] + spec["months"]
    days_by_symbol, fundings = {}, {}
    for symbol in PREREG["symbols"]:
        present = [m for m in months if m not in avail[symbol]["klines"]]
        bars = []
        for month in present:
            path = hist.ensure_archive(cache, _archive(window, symbol, month), fetch=_no_fetch)
            bars.extend(hist.parse_klines(hist._csv_rows(path)))
        if not bars:
            continue
        days_by_symbol[symbol] = daily_bars(bars)
        if spec["source"] == "USDM":
            fmonths = [m for m in present if m not in avail[symbol]["funding"]]
            events = hist.load_funding(cache, symbol, fmonths, fetch=_no_fetch) if fmonths else []
            fundings[symbol] = ("REAL", events)
        else:
            fundings[symbol] = ("ASSUMED", spec["funding"]["assumed_rate"])
    return days_by_symbol, fundings


def run_window(days_by_symbol, fundings) -> list[dict]:
    from bot.execution_model import slippage_rate_for_symbol

    slips = {s: slippage_rate_for_symbol(s) for s in days_by_symbol}
    btc_up = btc_filter(days_by_symbol.get("BTCUSDT", []))
    rows = []
    for symbol, days in days_by_symbol.items():
        rows.extend(simulate_h1(symbol, days, btc_up, slips[symbol], fundings[symbol]))
    rows.extend(simulate_h2(days_by_symbol, slips, fundings))
    return rows


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", default="research_cache/binance_vision")
    parser.add_argument("--out", default="artifacts/trend_long_v1")
    args = parser.parse_args(argv)
    cache, out = Path(args.cache), Path(args.out)
    rows_by_window = {}
    for window in ("decisive", "confirmation"):
        avail = download(cache, window)
        rows_by_window[window] = run_window(*load_window(cache, window, avail))
    report = build_report(rows_by_window)
    report["code_sha"] = _git_head()
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print("PREREG", report["prereg_id"], report["prereg_sha256"])
    for hyp, data in report["hypotheses"].items():
        print("VERDICT", hyp, data["verdict"])
        for window in ("decisive", "confirmation"):
            w = data[window]
            print("SUMMARY", hyp, window, json.dumps(w["summary"], sort_keys=True), "gross", w["gross_mean_r"])
            for key in ("by_year", "by_symbol", "exit_reasons"):
                print("BREAKDOWN", hyp, window, key, json.dumps(w[key], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
