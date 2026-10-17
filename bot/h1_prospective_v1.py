"""H1_PROSPECTIVE_V1: does H1 pay its costs on data that did not exist yet?

TREND_LONG_V1 passed H1 (B2 long only while BTC is above its 200-day average)
on untouched 2018-2021 spot data, but the gain came from 2020-2021 and the
last two years of the confirmation window were negative. This is the
prospective test: the same frozen rule, the same 25 pairs and costs, on USD-M
daily klines with real funding, only on months that start after this
pre-registration was frozen (2026-10-17). It is re-run as Binance publishes
each month; the verdict can only be read at the two pre-registered looks.

Research only: public archives verified by SHA-256, no credentials, database,
engine or order.
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
from bot import trend_long_study_v1 as long_study

PREREG = {
    "id": "H1_PROSPECTIVE_V1",
    "frozen_on": "2026-10-17",
    "rule": {**long_study.PREREG["H1"], "source": "TREND_LONG_V1 H1, unchanged"},
    "symbols": list(long_study.PREREG["symbols"]),
    "source": "USDM",
    "interval": "1d",
    "warmup_months": hist.months_between("2026-01", "2026-10"),
    "first_month": "2026-11",
    "interim_last_month": "2027-10",
    "final_last_month": "2028-10",
    "fee": long_study.PREREG["fee"],
    "slippage": "execution_model.slippage_rate_for_symbol, every fill",
    "funding": "REAL; a symbol-month without a funding archive adds no funding and is listed in the report",
    "trade_counts_by": "entry month; positions still open at the data end are excluded",
    "cluster": "UTC_MONTH",
    "bootstrap": {"replicates": 10_000, "seed": 20261117},
    "gate": {"min_trades": 100, "min_symbols": 10},
    "interim": {"lower_quantile": 0.01,
                "fail_if": "trades > 0 and mean <= 0 (futility, final)",
                "pass_if": "gate met, 1% bootstrap lower > 0 and every LOSO mean > 0",
                "else": "CONTINUE to the final look"},
    "final": {"lower_quantile": 0.05,
              "pass_if": "gate met, 5% bootstrap lower > 0 and every LOSO mean > 0",
              "else": "FAIL, or INSUFFICIENT_EVIDENCE below the gate"},
}
AUTHORITY = {"research_only": True, "live_allowed": False,
             "decision_effect": "NONE", "execution_effect": "NONE"}
ROOT = Path(__file__).resolve().parents[1]
REFERENCE_SYMBOL = "BTCUSDT"


def prereg_digest() -> str:
    raw = json.dumps(PREREG, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def months_until(last_month: str) -> list[str]:
    return hist.months_between(PREREG["warmup_months"][0], last_month)


def previous_month(now: datetime) -> str:
    y, m = now.year, now.month
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04d}-{m:02d}"


# ---------------------------------------------------------------- data

def download(cache: Path, symbols: list[str], months: list[str], *, fetch=hist._fetch) -> dict:
    """Only HTTP 404 means 'not published yet / not listed'; anything else fails."""
    avail = {}
    for symbol in symbols:
        entry = {"klines_absent": [], "funding_absent": []}
        for month in months:
            for kind, archive in (("klines_absent", hist.kline_archive(symbol, PREREG["interval"], month)),
                                  ("funding_absent", hist.funding_archive(symbol, month))):
                try:
                    hist.ensure_archive(cache, archive, fetch=fetch)
                except urllib.error.HTTPError as exc:
                    if exc.code != 404:
                        raise
                    entry[kind].append(month)
        avail[symbol] = entry
    return avail


def published_through(avail: dict, months: list[str]) -> str | None:
    """Last month of the contiguous run Binance has published (BTCUSDT as reference)."""
    last = None
    for month in months:
        if month in avail[REFERENCE_SYMBOL]["klines_absent"]:
            break
        last = month
    return last


def _no_fetch(url):
    raise PermissionError(f"simulation must read the verified cache only: {url}")


def load(cache: Path, symbol: str, months: list[str], entry: dict):
    present = [m for m in months if m not in entry["klines_absent"]]
    bars = []
    for month in present:
        path = hist.ensure_archive(cache, hist.kline_archive(symbol, PREREG["interval"], month), fetch=_no_fetch)
        bars.extend(hist.parse_klines(hist._csv_rows(path)))
    funding_months = [m for m in present if m not in entry["funding_absent"]]
    events = hist.load_funding(cache, symbol, funding_months, fetch=_no_fetch) if funding_months else []
    return long_study.daily_bars(bars), ("REAL", events)


def simulate(days_by_symbol: dict, fundings: dict) -> list[dict]:
    from bot.execution_model import slippage_rate_for_symbol

    btc_up = long_study.btc_filter(days_by_symbol.get(REFERENCE_SYMBOL, []))
    rows = []
    for symbol, days in days_by_symbol.items():
        rows.extend(long_study.simulate_h1(symbol, days, btc_up, slippage_rate_for_symbol(symbol),
                                           fundings[symbol]))
    return rows


# ---------------------------------------------------------------- statistics

def eligible(rows: list[dict], first: str, last: str) -> list[dict]:
    lo, hi = base.month_bounds(hist.months_between(first, last))
    return [r for r in rows if lo <= r["ts"] < hi]


def summarize(rows: list[dict], quantile: float) -> dict:
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
    return {
        "trades": len(values), "symbols": len(by_symbol), "clusters": len(by_cluster),
        "mean_r": total / len(values), "sum_r": total,
        "win_rate": sum(1 for v in values if v > 0) / len(values),
        "bootstrap_lower_r": base.cluster_bootstrap_lower(
            by_cluster, replicates=b["replicates"], seed=b["seed"], quantile=quantile),
        "lower_quantile": quantile,
        "loso_min_mean_r": min(loso) if loso else None,
    }


def _gate(s: dict) -> bool:
    g = PREREG["gate"]
    return s.get("trades", 0) >= g["min_trades"] and s.get("symbols", 0) >= g["min_symbols"]


def _passes(s: dict) -> bool:
    return (_gate(s) and s["mean_r"] > 0 and (s["bootstrap_lower_r"] or -1) > 0
            and (s["loso_min_mean_r"] or -1) > 0)


def verdict(rows: list[dict], through: str | None) -> dict:
    if through is None or through < PREREG["interim_last_month"]:
        window = (PREREG["first_month"], through) if through and through >= PREREG["first_month"] else None
        s = summarize(eligible(rows, *window), PREREG["final"]["lower_quantile"]) if window else {"trades": 0}
        return {"look": "NONE_YET", "verdict": "NOT_YET", "descriptive": s,
                "next_look_after": PREREG["interim_last_month"]}
    interim = summarize(eligible(rows, PREREG["first_month"], PREREG["interim_last_month"]),
                        PREREG["interim"]["lower_quantile"])
    if interim.get("trades", 0) and interim["mean_r"] <= 0:
        return {"look": "INTERIM", "verdict": "H1_PROSPECTIVE_FAIL", "basis": interim, "reason": "futility"}
    if _passes(interim):
        return {"look": "INTERIM", "verdict": "H1_PROSPECTIVE_PASS", "basis": interim}
    if through < PREREG["final_last_month"]:
        return {"look": "INTERIM", "verdict": "CONTINUE", "basis": interim,
                "next_look_after": PREREG["final_last_month"]}
    final = summarize(eligible(rows, PREREG["first_month"], PREREG["final_last_month"]),
                      PREREG["final"]["lower_quantile"])
    if not _gate(final):
        return {"look": "FINAL", "verdict": "H1_PROSPECTIVE_INSUFFICIENT_EVIDENCE", "basis": final}
    return {"look": "FINAL", "verdict": "H1_PROSPECTIVE_PASS" if _passes(final) else "H1_PROSPECTIVE_FAIL",
            "basis": final}


def _breakdown(rows, field):
    groups = defaultdict(list)
    for r in rows:
        groups[field(r)].append(r["net_r"])
    return {str(k): {"trades": len(v), "mean_r": sum(v) / len(v)} for k, v in sorted(groups.items())}


def build_report(rows: list[dict], through: str | None, funding_absent: dict | None = None) -> dict:
    report = {"prereg_id": PREREG["id"], "prereg_sha256": prereg_digest(),
              "published_through": through, **AUTHORITY}
    report.update(verdict(rows, through))
    if through and through >= PREREG["first_month"]:
        seen = eligible(rows, PREREG["first_month"], through)
        report["by_month"] = _breakdown(seen, lambda r: r["cluster"])
        report["exit_reasons"] = _breakdown(seen, lambda r: r["reason"])
    report["funding_absent"] = {s: m for s, m in sorted((funding_absent or {}).items()) if m}
    return report


# ----------------------------------------------------------------------- CLI

def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", default="research_cache/binance_vision")
    parser.add_argument("--out", default="artifacts/h1_prospective_v1")
    parser.add_argument("--through", help="last month to request (default: previous calendar month)")
    args = parser.parse_args(argv)
    cache, out = Path(args.cache), Path(args.out)
    last = args.through or previous_month(datetime.now(timezone.utc))
    months = months_until(last)
    avail = download(cache, PREREG["symbols"], months)
    through = published_through(avail, months)
    usable = [m for m in months if through and m <= through]
    days_by_symbol, fundings, funding_absent = {}, {}, {}
    for symbol in PREREG["symbols"]:
        days, funding = load(cache, symbol, usable, avail[symbol])
        if days:
            days_by_symbol[symbol], fundings[symbol] = days, funding
        funding_absent[symbol] = [m for m in avail[symbol]["funding_absent"]
                                  if m in usable and m not in avail[symbol]["klines_absent"]]
    report = build_report(simulate(days_by_symbol, fundings), through, funding_absent)
    report["code_sha"] = _git_head()
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print("PREREG", report["prereg_id"], report["prereg_sha256"], "through", through)
    print("VERDICT", report["look"], report["verdict"],
          json.dumps(report.get("basis", report.get("descriptive")), sort_keys=True))
    for key in ("by_month", "exit_reasons", "funding_absent"):
        if key in report:
            print("BREAKDOWN", key, json.dumps(report[key], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
