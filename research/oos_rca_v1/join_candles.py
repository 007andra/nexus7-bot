"""Attach independently archived closed Binance 15m bars to OOS candidates.

Strict ID and symbol/time matching. No exchange/DB/network connections.
Candles must come from an externally verified historical market data archive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

from research.oos_rca_v1.binance_path_replay import STEP, _candle


def attach(candidates, market_bars):
    cache = {}
    for item in market_bars:
        if not isinstance(item, dict):
            raise ValueError("INVALID_MARKET_RECORD")
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("CANDLE_SYMBOL_REQUIRED")
        ts, op, hi, lo, close = _candle(item)
        key = (symbol, ts)
        if key in cache:
            raise ValueError("DUPLICATE_SYMBOL_TIMESTAMP")
        cache[key] = {"ts": ts, "o": op, "h": hi, "l": lo, "c": close}
    seen = set()
    merged = []
    missing = Counter()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("INVALID_CANDIDATE_RECORD")
        cid = candidate.get("candidate_id")
        symbol = candidate.get("symbol")
        cap = candidate.get("captured_epoch")
        if (not isinstance(cid, str) or not cid or not isinstance(symbol, str)
                or not symbol or isinstance(cap, bool)
                or not isinstance(cap, (float, int)) or not math.isfinite(cap)):
            raise ValueError("INVALID_CANDIDATE_ID_OR_TIMESTAMP")
        if cid in seen:
            raise ValueError("DUPLICATE_CANDIDATE_ID")
        seen.add(cid)
        start = math.ceil(cap / STEP) * STEP
        times = range(int(start), int(start + 16 * STEP), STEP)
        bars = [cache[(symbol, ts)] for ts in times if (symbol, ts) in cache]
        missing[cid] = 16 - len(bars)
        merged.append({**candidate, "bars": bars})
    return merged, {
        "count": len(merged),
        "candidate_with_full_240m_path": sum(n == 0 for n in missing.values()),
        "candidate_missing_any_240m_bar": sum(n > 0 for n in missing.values()),
        "total_missing_expected_240m_bars": sum(missing.values()),
        "source_requirement": "INDEPENDENT_CLOSED_15M_BINANCE_USDM_HISTORY",
        "authority": "RESEARCH_ONLY_NO_LIVE",
        "live_allowed": False,
    }


def _load(path):
    result = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("object required")
                result.append(row)
            except (ValueError, TypeError) as err:
                raise ValueError(f"{path}:{i}: {err}") from err
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates", type=Path, required=True)
    ap.add_argument("--candles", type=Path, required=True)
    ap.add_argument("--prefix", type=Path, required=True)
    args = ap.parse_args()
    joined, summary = attach(_load(args.candidates), _load(args.candles))
    canonical = "".join(json.dumps(x, sort_keys=True, allow_nan=False) + "\n" for x in joined)
    summary["joined_jsonl_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    args.prefix.parent.mkdir(parents=True, exist_ok=True)
    args.prefix.with_suffix(".jsonl").write_text(canonical, encoding="utf-8")
    args.prefix.with_suffix(".json").write_text(
        json.dumps(summary, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")


if __name__ == "__main__":
    main()
