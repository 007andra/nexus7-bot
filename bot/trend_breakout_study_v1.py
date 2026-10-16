"""TREND_BREAKOUT_V1: do two trend-following rules pay their costs?

Research only. Two hypotheses, each with its own pre-registered verdict:

* B1 (day trade): intraday volatility breakout. Each UTC day, in the direction
  of the 20-day trend, enter when price moves k x the previous day's range
  away from the day's open; stop at 0.5 x that range; close at the end of the
  UTC day.
* B2 (swing): daily channel breakout. Enter at the next open after a close
  beyond the 20-day channel; initial stop 2 x ATR(20); exit at the next open
  after a close beyond the opposite 10-day channel.

Both run on Binance USD-M public 15m history (data.binance.vision, verified
by SHA-256), with taker fees, static slippage and real funding. No
credentials, database, engine, analyzer, exchange client or order is used.
PREREG is fixed in code before any data is read and the verdict is computed
only on the holdout window, which no earlier study in this series read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import subprocess
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from bot import binance_public_history as hist

DAY_MS = 86_400_000
BAR_MS = 900_000
BARS_PER_DAY = 96

PREREG = {
    "id": "TREND_BREAKOUT_V1",
    "symbols": [
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
        "ADAUSDT", "DOGEUSDT", "TRXUSDT", "LINKUSDT", "AVAXUSDT",
        "DOTUSDT", "LTCUSDT", "NEARUSDT", "ATOMUSDT", "SUIUSDT",
        "PEOPLEUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "UNIUSDT",
        "AAVEUSDT", "FILUSDT", "INJUSDT", "SEIUSDT", "ETCUSDT",
    ],
    "interval": "15m",
    "warmup_months": ["2021-10", "2021-11", "2021-12"],
    "holdout_months": hist.months_between("2022-01", "2024-08"),
    "descriptive_months": hist.months_between("2024-09", "2026-09"),
    "listing_rule": "a symbol enters at its first published month; absent months after it drop their days",
    "complete_day": "96 contiguous 15m bars inside the UTC day",
    "primary_fee": 0.0006,
    "sensitivity_fees": [0.0004, 0.0005],
    "slippage": "execution_model.slippage_rate_for_symbol, every fill",
    "missing_funding": {"rate": 0.0001, "settlements_utc_hours": [0, 8, 16], "sign": "ADVERSE"},
    "B1": {
        "primary_k": 0.5,
        "sensitivity_k": [0.3, 0.7],
        "stop_range_mult": 0.5,
        "trend_sma_days": 20,
        "last_entry_bar_utc_hour": 20,
        "entry": "STOP_AT_TRIGGER_OR_GAP_OPEN",
        "exit": "STOP_OR_UTC_DAY_CLOSE",
        "same_bar": "CONSERVATIVE_STOP_AFTER_ENTRY",
        "cluster": "UTC_DAY",
    },
    "B2": {
        "primary": {"entry_days": 20, "exit_days": 10},
        "sensitivity": [{"entry_days": 55, "exit_days": 20}],
        "atr_days": 20,
        "stop_atr_mult": 2.0,
        "entry": "NEXT_DAY_OPEN",
        "exit": "STOP_OR_NEXT_OPEN_AFTER_OPPOSITE_CHANNEL_CLOSE",
        "cluster": "UTC_MONTH",
    },
    "risk_basis": "planned stop distance over the entry fill",
    "bootstrap": {"replicates": 10_000, "seed": 20261016, "lower_quantile": 0.05},
    "gate": {"min_trades": 150, "min_symbols": 10},
    "criterion": "holdout, primary variant, primary fee: mean > 0, bootstrap lower > 0, every leave-one-symbol-out mean > 0",
}
AUTHORITY = {"research_only": True, "promotion_allowed": False, "live_allowed": False,
             "decision_effect": "NONE", "execution_effect": "NONE"}
ROOT = Path(__file__).resolve().parents[1]


def prereg_digest() -> str:
    raw = json.dumps(PREREG, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def month_bounds(months: list[str]) -> tuple[int, int]:
    first = datetime.strptime(months[0], "%Y-%m").replace(tzinfo=timezone.utc)
    y, m = (int(x) for x in months[-1].split("-"))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return int(first.timestamp() * 1000), int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)


def b1_variant(k: float) -> str:
    return f"B1_k{k:.1f}"


def b2_variant(entry_days: int, exit_days: int) -> str:
    return f"B2_{entry_days}_{exit_days}"


PRIMARY = {"B1": b1_variant(PREREG["B1"]["primary_k"]),
           "B2": b2_variant(PREREG["B2"]["primary"]["entry_days"], PREREG["B2"]["primary"]["exit_days"])}


# ---------------------------------------------------------------- days

def build_days(k15: list[dict]) -> list[dict]:
    """UTC days with their 15m bars; ``complete`` only with 96 contiguous bars."""
    by_day = defaultdict(list)
    for bar in k15:
        by_day[bar["ts"] // DAY_MS].append(bar)
    days = []
    for day in sorted(by_day):
        bars = sorted(by_day[day], key=lambda b: b["ts"])
        complete = (len(bars) == BARS_PER_DAY and bars[0]["ts"] == day * DAY_MS
                    and all(b2["ts"] - b1["ts"] == BAR_MS for b1, b2 in zip(bars, bars[1:])))
        days.append({"day": day, "bars": bars, "complete": complete,
                     "o": bars[0]["o"], "h": max(b["h"] for b in bars),
                     "l": min(b["l"] for b in bars), "c": bars[-1]["c"]})
    return days


def _history_ok(days: list[dict], i: int, n: int) -> bool:
    """Days i-n..i are complete and calendar-consecutive."""
    if i - n < 0:
        return False
    window = days[i - n:i + 1]
    return (all(d["complete"] for d in window)
            and all(b["day"] - a["day"] == 1 for a, b in zip(window, window[1:])))


# ---------------------------------------------------------------- costs

def _fill(price: float, direction: str, is_entry: bool, slip: float) -> float:
    from bot.execution_model import adverse_fill
    return adverse_fill(price, direction, is_entry=is_entry, slippage_rate=slip)


def _net(direction: str, entry_fill: float, exit_fill: float, risk_fraction: float,
         entry_ts: int, exit_ts: int, funding: dict, fees: list[float]) -> dict:
    """Net R per fee. ``funding`` is ``{"events": [...], "absent": [(lo, hi), ...]}``."""
    from bot.execution_model import fee_return_fraction, funding_return_fraction
    side = 1.0 if direction == "LONG" else -1.0
    gross = side * (exit_fill - entry_fill) / entry_fill
    real, count = funding_return_fraction(funding["events"], direction, entry_ts, exit_ts, entry_fill)
    assumed, assumed_count = _assumed_funding(entry_ts, exit_ts, funding["absent"])
    fund = real + assumed
    return {
        "gross_r": gross / risk_fraction,
        "funding_r": fund / risk_fraction,
        "funding_events": count + assumed_count,
        "funding_assumed": assumed_count > 0,
        "net_r": {f"{fee:.4f}": (gross - fee_return_fraction(entry_fill, [(exit_fill, 1.0)], fee) + fund)
                  / risk_fraction for fee in fees},
    }


def _assumed_funding(entry_ts: int, exit_ts: int, absent: list) -> tuple[float, int]:
    """Adverse flat rate at each standard settlement inside a month whose archive is absent."""
    if not absent:
        return 0.0, 0
    spec = PREREG["missing_funding"]
    count = 0
    day = entry_ts // DAY_MS
    while day * DAY_MS <= exit_ts:
        for hour in spec["settlements_utc_hours"]:
            tp = day * DAY_MS + hour * 3_600_000
            if entry_ts < tp <= exit_ts and any(lo <= tp < hi for lo, hi in absent):
                count += 1
        day += 1
    return -spec["rate"] * count, count


# ---------------------------------------------------------------- B1

def simulate_b1_day(days: list[dict], i: int, *, k: float, slip: float,
                    funding: dict, fees: list[float]) -> dict | None:
    """One B1 trade on day ``i`` or None (no setup, no trigger, missing history)."""
    spec = PREREG["B1"]
    n = spec["trend_sma_days"]
    if not _history_ok(days, i, n):
        return None
    prev, today = days[i - 1], days[i]
    rng = prev["h"] - prev["l"]
    if rng <= 0:
        return None
    sma = sum(d["c"] for d in days[i - n:i]) / n
    if prev["c"] > sma:
        direction = "LONG"
    elif prev["c"] < sma:
        direction = "SHORT"
    else:
        return None
    side = 1.0 if direction == "LONG" else -1.0
    trigger = today["o"] + side * k * rng
    stop = trigger - side * spec["stop_range_mult"] * rng
    if stop <= 0:
        return None
    last_entry_ms = today["day"] * DAY_MS + spec["last_entry_bar_utc_hour"] * 3_600_000

    entry = None
    for bar in today["bars"]:
        if entry is None:
            if bar["ts"] >= last_entry_ms:
                return None
            hit = bar["h"] >= trigger if direction == "LONG" else bar["l"] <= trigger
            if not hit:
                continue
            gapped = bar["o"] >= trigger if direction == "LONG" else bar["o"] <= trigger
            px = bar["o"] if gapped else trigger
            entry = {"ts": bar["ts"], "px": px, "fill": _fill(px, direction, True, slip)}
            # Conservative: a stop touched anywhere in the entry bar counts.
            touched = bar["l"] <= stop if direction == "LONG" else bar["h"] >= stop
            if touched:
                return _b1_row(direction, entry, stop, stop, bar["ts"] + BAR_MS - 1, "STOP",
                               rng, k, slip, funding, fees, today)
            continue
        gap = bar["o"] <= stop if direction == "LONG" else bar["o"] >= stop
        if gap:
            return _b1_row(direction, entry, stop, bar["o"], bar["ts"] + BAR_MS - 1, "STOP_GAP",
                           rng, k, slip, funding, fees, today)
        touched = bar["l"] <= stop if direction == "LONG" else bar["h"] >= stop
        if touched:
            return _b1_row(direction, entry, stop, stop, bar["ts"] + BAR_MS - 1, "STOP",
                           rng, k, slip, funding, fees, today)
    if entry is None:
        return None
    last = today["bars"][-1]
    return _b1_row(direction, entry, stop, last["c"], last["ts"] + BAR_MS - 1, "DAY_CLOSE",
                   rng, k, slip, funding, fees, today)


def _b1_row(direction, entry, stop, exit_px, exit_ts, reason, rng, k, slip, funding, fees, today):
    risk_fraction = PREREG["B1"]["stop_range_mult"] * rng / entry["fill"]
    exit_fill = _fill(exit_px, direction, False, slip)
    row = {"hypothesis": "B1", "variant": b1_variant(k), "direction": direction,
           "ts": entry["ts"], "exit_ts": exit_ts, "reason": reason,
           "entry_fill": entry["fill"], "exit_fill": exit_fill, "stop": stop,
           "risk_fraction": risk_fraction, "cluster": str(today["day"])}
    row.update(_net(direction, entry["fill"], exit_fill, risk_fraction, entry["ts"], exit_ts, funding, fees))
    return row


def evaluate_b1(symbol: str, days: list[dict], *, slip: float, funding: dict,
                fees: list[float]) -> list[dict]:
    rows = []
    for k in [PREREG["B1"]["primary_k"]] + PREREG["B1"]["sensitivity_k"]:
        for i in range(len(days)):
            row = simulate_b1_day(days, i, k=k, slip=slip, funding=funding, fees=fees)
            if row is not None:
                row["symbol"] = symbol
                rows.append(row)
    return rows


# ---------------------------------------------------------------- B2

def _atr(days: list[dict], i: int, n: int) -> float:
    """Mean true range of days i-n+1..i."""
    total = 0.0
    for j in range(i - n + 1, i + 1):
        d, p = days[j], days[j - 1]
        total += max(d["h"] - d["l"], abs(d["h"] - p["c"]), abs(d["l"] - p["c"]))
    return total / n


def simulate_b2(symbol: str, days: list[dict], *, entry_days: int, exit_days: int, slip: float,
                funding: dict, fees: list[float]) -> list[dict]:
    spec = PREREG["B2"]
    need = max(entry_days, exit_days, spec["atr_days"] + 1)
    variant = b2_variant(entry_days, exit_days)
    rows = []
    pos = None
    pending_entry = None
    pending_exit = False
    for i, today in enumerate(days):
        consecutive = i > 0 and today["day"] - days[i - 1]["day"] == 1
        if pos is not None and (not today["complete"] or not consecutive):
            prev_last = days[i - 1]["bars"][-1]
            rows.append(_b2_row(symbol, variant, pos, prev_last["c"], prev_last["ts"] + BAR_MS - 1,
                                "DATA_GAP", slip, funding, fees))
            pos, pending_exit = None, False
        if pending_entry is not None and pos is None and today["complete"] and consecutive:
            direction, distance = pending_entry
            first = today["bars"][0]
            fill = _fill(first["o"], direction, True, slip)
            side = 1.0 if direction == "LONG" else -1.0
            stop = fill - side * distance
            if stop > 0:
                pos = {"direction": direction, "ts": first["ts"], "fill": fill, "stop": stop,
                       "risk_fraction": distance / fill}
        pending_entry = None
        if pos is not None and pending_exit:
            first = today["bars"][0]
            rows.append(_b2_row(symbol, variant, pos, first["o"], first["ts"], "CHANNEL_EXIT",
                                slip, funding, fees))
            pos, pending_exit = None, False
        if pos is not None:
            stopped = _b2_stop_today(today, pos, pos["ts"])
            if stopped is not None:
                px, ts, reason = stopped
                rows.append(_b2_row(symbol, variant, pos, px, ts, reason, slip, funding, fees))
                pos = None
        if not _history_ok(days, i, need):
            continue
        close = today["c"]
        if pos is not None:
            lows = min(d["l"] for d in days[i - exit_days:i])
            highs = max(d["h"] for d in days[i - exit_days:i])
            if (pos["direction"] == "LONG" and close < lows) or (pos["direction"] == "SHORT" and close > highs):
                pending_exit = True
        else:
            hi = max(d["h"] for d in days[i - entry_days:i])
            lo = min(d["l"] for d in days[i - entry_days:i])
            distance = spec["stop_atr_mult"] * _atr(days, i, spec["atr_days"])
            if distance > 0 and close > hi:
                pending_entry = ("LONG", distance)
            elif distance > 0 and close < lo:
                pending_entry = ("SHORT", distance)
    if pos is not None and days:
        last = days[-1]["bars"][-1]
        rows.append(_b2_row(symbol, variant, pos, last["c"], last["ts"] + BAR_MS - 1, "DATA_END",
                            slip, funding, fees))
    return rows


def _b2_stop_today(today: dict, pos: dict, entry_ts: int):
    long = pos["direction"] == "LONG"
    stop = pos["stop"]
    for bar in today["bars"]:
        if bar["ts"] < entry_ts:
            continue
        if bar["ts"] > entry_ts and (bar["o"] <= stop if long else bar["o"] >= stop):
            return bar["o"], bar["ts"] + BAR_MS - 1, "STOP_GAP"
        if bar["l"] <= stop if long else bar["h"] >= stop:
            return stop, bar["ts"] + BAR_MS - 1, "STOP"
    return None


def _b2_row(symbol, variant, pos, exit_px, exit_ts, reason, slip, funding, fees):
    exit_fill = _fill(exit_px, pos["direction"], False, slip)
    month = datetime.fromtimestamp(pos["ts"] / 1000, tz=timezone.utc).strftime("%Y-%m")
    row = {"hypothesis": "B2", "variant": variant, "symbol": symbol, "direction": pos["direction"],
           "ts": pos["ts"], "exit_ts": exit_ts, "reason": reason, "entry_fill": pos["fill"],
           "exit_fill": exit_fill, "stop": pos["stop"], "risk_fraction": pos["risk_fraction"],
           "cluster": month}
    row.update(_net(pos["direction"], pos["fill"], exit_fill, pos["risk_fraction"], pos["ts"],
                    exit_ts, funding, fees))
    return row


def evaluate_b2(symbol: str, days: list[dict], *, slip: float, funding: dict,
                fees: list[float]) -> list[dict]:
    rows = []
    for v in [PREREG["B2"]["primary"]] + PREREG["B2"]["sensitivity"]:
        rows.extend(simulate_b2(symbol, days, entry_days=v["entry_days"], exit_days=v["exit_days"],
                                slip=slip, funding=funding, fees=fees))
    return rows


# ---------------------------------------------------------------- statistics

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
    if not rows:
        return {"trades": 0}
    values = [r["net_r"][fee_key] for r in rows]
    by_cluster, by_symbol = defaultdict(list), defaultdict(list)
    for r, v in zip(rows, values):
        by_cluster[r["cluster"]].append(v)
        by_symbol[r["symbol"]].append(v)
    total = sum(values)
    loso = [(total - sum(v)) / (len(values) - len(v)) for v in by_symbol.values() if len(values) > len(v)]
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
        "loso_min_mean_r": min(loso) if loso else None,
    }


def verdict(hypothesis: str, summary: dict) -> str:
    g = PREREG["gate"]
    if summary.get("trades", 0) < g["min_trades"] or summary.get("symbols", 0) < g["min_symbols"]:
        return f"{hypothesis}_INSUFFICIENT_EVIDENCE"
    ok = (summary["mean_r"] > 0 and (summary["bootstrap_lower_r"] or -1) > 0
          and (summary["loso_min_mean_r"] or -1) > 0)
    return f"{hypothesis}_{'PASS' if ok else 'FAIL'}"


def _breakdown(rows, fee_key, field):
    groups = defaultdict(list)
    for r in rows:
        groups[field(r)].append(r["net_r"][fee_key])
    return {str(k): {"trades": len(v), "mean_r": sum(v) / len(v)} for k, v in sorted(groups.items())}


def _median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def _month(ts):
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")


def build_report(rows: list[dict], data_notes: dict | None = None) -> dict:
    fee_keys = [f"{f:.4f}" for f in [PREREG["primary_fee"]] + PREREG["sensitivity_fees"]]
    primary_fee = fee_keys[0]
    windows = {"holdout": month_bounds(PREREG["holdout_months"]),
               "descriptive": month_bounds(PREREG["descriptive_months"])}
    report = {"prereg_id": PREREG["id"], "prereg_sha256": prereg_digest(),
              "data_notes": data_notes or {}, "hypotheses": {}, **AUTHORITY}
    for hypothesis in ("B1", "B2"):
        variants = sorted({r["variant"] for r in rows if r["hypothesis"] == hypothesis} | {PRIMARY[hypothesis]})
        out = {}
        for name, (lo, hi) in windows.items():
            per_variant = {}
            for variant in variants:
                chosen = [r for r in rows if r["variant"] == variant and lo <= r["ts"] < hi]
                data = {"by_fee": {k: summarize(chosen, k) for k in fee_keys}}
                if variant == PRIMARY[hypothesis] and chosen:
                    data.update({
                        "gross_mean_r": sum(r["gross_r"] for r in chosen) / len(chosen),
                        "funding_mean_r": sum(r["funding_r"] for r in chosen) / len(chosen),
                        "funding_assumed_trades": sum(1 for r in chosen if r["funding_assumed"]),
                        "median_stop_pct": 100 * _median([r["risk_fraction"] for r in chosen]),
                        "median_hold_hours": _median([(r["exit_ts"] - r["ts"]) / 3_600_000 for r in chosen]),
                        "exit_reasons": _breakdown(chosen, primary_fee, lambda r: r["reason"]),
                        "by_direction": _breakdown(chosen, primary_fee, lambda r: r["direction"]),
                        "by_symbol": _breakdown(chosen, primary_fee, lambda r: r["symbol"]),
                        "by_month": _breakdown(chosen, primary_fee, lambda r: _month(r["ts"])),
                    })
                per_variant[variant] = data
            out[name] = per_variant
        basis = out["holdout"][PRIMARY[hypothesis]]["by_fee"][primary_fee]
        out["verdict"] = verdict(hypothesis, basis)
        out["verdict_basis"] = {"window": "holdout", "variant": PRIMARY[hypothesis],
                                "fee": PREREG["primary_fee"], **basis}
        report["hypotheses"][hypothesis] = out
    return report


# ---------------------------------------------------------------- data

AVAILABILITY = "availability.json"


def _months_all() -> list[str]:
    return PREREG["warmup_months"] + PREREG["holdout_months"] + PREREG["descriptive_months"]


def download(cache: Path, symbols: list[str], *, fetch=hist._fetch) -> dict:
    """Fetch every archive; only HTTP 404 is recorded as absent, anything else fails."""
    path = cache / AVAILABILITY
    avail = json.loads(path.read_text()) if path.exists() else {}
    for symbol in symbols:
        entry = {"klines_absent": [], "funding_absent": []}
        for month in _months_all():
            for kind, archive in (("klines_absent", hist.kline_archive(symbol, PREREG["interval"], month)),
                                  ("funding_absent", hist.funding_archive(symbol, month))):
                try:
                    hist.ensure_archive(cache, archive, fetch=fetch)
                except urllib.error.HTTPError as exc:
                    if exc.code != 404:
                        raise
                    entry[kind].append(month)
        avail[symbol] = entry
        cache.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(avail, indent=1, sort_keys=True))
        print(f"downloaded {symbol} klines_absent={len(entry['klines_absent'])} "
              f"funding_absent={len(entry['funding_absent'])}", flush=True)
    return avail


def _no_fetch(url):
    raise PermissionError(f"simulation must read the verified cache only: {url}")


def load_symbol(cache: Path, symbol: str, avail: dict) -> tuple[list[dict], dict, dict]:
    entry = avail[symbol]
    months = _months_all()
    present = [m for m in months if m not in entry["klines_absent"]]
    notes = {"first_month": present[0] if present else None,
             "absent_after_listing": [m for m in entry["klines_absent"] if present and m > present[0]]}
    bars = []
    for month in present:
        path = hist.ensure_archive(cache, hist.kline_archive(symbol, PREREG["interval"], month), fetch=_no_fetch)
        bars.extend(hist.parse_klines(hist._csv_rows(path)))
    by_ts = {b["ts"]: b for b in bars}
    bars = [by_ts[ts] for ts in sorted(by_ts)]
    funding_months = [m for m in present if m not in entry["funding_absent"]]
    absent = [m for m in present if m in entry["funding_absent"]]
    funding = {"events": hist.load_funding(cache, symbol, funding_months, fetch=_no_fetch) if funding_months else [],
               "absent": [month_bounds([m]) for m in absent]}
    notes["funding_absent"] = absent
    return bars, funding, notes


def evaluate_symbol(symbol: str, bars: list[dict], funding: dict) -> tuple[list[dict], dict]:
    from bot.execution_model import slippage_rate_for_symbol
    slip = slippage_rate_for_symbol(symbol)
    fees = [PREREG["primary_fee"]] + PREREG["sensitivity_fees"]
    days = build_days(bars)
    rows = evaluate_b1(symbol, days, slip=slip, funding=funding, fees=fees)
    rows += evaluate_b2(symbol, days, slip=slip, funding=funding, fees=fees)
    notes = {"days": len(days), "incomplete_days": sum(1 for d in days if not d["complete"]), "slippage": slip}
    return rows, notes


# ----------------------------------------------------------------------- CLI

def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", default="research_cache/binance_vision")
    parser.add_argument("--out", default="artifacts/trend_breakout_v1")
    parser.add_argument("--symbols", nargs="+", default=PREREG["symbols"])
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--skip-download", action="store_true")
    args = parser.parse_args(argv)
    cache, out = Path(args.cache), Path(args.out)
    if not args.skip_download:
        download(cache, args.symbols)
    if args.download_only:
        return 0
    avail = json.loads((cache / AVAILABILITY).read_text())
    rows, data_notes = [], {}
    for symbol in args.symbols:
        bars, funding, notes = load_symbol(cache, symbol, avail)
        sym_rows, sym_notes = evaluate_symbol(symbol, bars, funding)
        rows.extend(sym_rows)
        data_notes[symbol] = {**notes, **sym_notes, "rows": len(sym_rows)}
        print(f"{symbol}: {json.dumps(data_notes[symbol], sort_keys=True)}", flush=True)
    rows.sort(key=lambda r: (r["ts"], r["symbol"], r["variant"]))
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "trades.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    report = build_report(rows, data_notes)
    report["code_sha"] = _git_head()
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    for hypothesis, data in report["hypotheses"].items():
        print("VERDICT", hypothesis, data["verdict"], json.dumps(data["verdict_basis"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
