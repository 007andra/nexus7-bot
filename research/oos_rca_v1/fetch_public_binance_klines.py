"""Download only PUBLIC Binance USD-M closed 15m candle history for OOS replay.

GET /fapi/v1/klines — no Binance API keys, signatures, account data or orders.
Source candidate file is Railway log fallback or read-only PostgreSQL export.
Retries/HTTP errors are explicit; incomplete histories remain missing. When
running near realtime, set --as-of-epoch to a frozen UTC time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from collections import defaultdict
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

INTERVAL_SECONDS = 900
MAX_BARS = 1500
HOST = "https://fapi.binance.com"
SYMBOL = re.compile(r"^[A-Z0-9]{5,24}$")


def _integer_epoch(x):
    if isinstance(x, bool):
        raise ValueError("INVALID_EPOCH")
    try:
        result = float(x)
    except (TypeError, ValueError) as exc:
        raise ValueError("INVALID_EPOCH") from exc
    if not math.isfinite(result) or result <= 0 or result >= 1e11:
        raise ValueError("INVALID_EPOCH")
    return result


def _fetch(symbol, start_s, end_s):
    if not SYMBOL.fullmatch(symbol):
        raise ValueError("INVALID_SYMBOL")
    params = {
        "symbol": symbol, "interval": "15m",
        "startTime": int(start_s * 1000),
        "endTime": int(end_s * 1000) - 1,
        "limit": MAX_BARS,
    }
    url = HOST + "/fapi/v1/klines?" + urlencode(params)
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": "NEXUS7-Research-OOS-ReadOnly/1.0"}), timeout=20) as response:
                payload = response.read(MAX_BARS * 180 + 1024)
            return json.loads(payload.decode("utf-8"))
        except HTTPError as exc:
            if exc.code not in (418, 429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f"PUBLIC_API_HTTP_{exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            if attempt == 2:
                raise RuntimeError("PUBLIC_API_UNREACHABLE") from exc
        time.sleep(1 + attempt)
    raise RuntimeError("PUBLIC_API_FAILED")


def download(candidates, *, as_of, fetch=_fetch):
    cutoff = _integer_epoch(as_of)
    symbols = defaultdict(list)
    for candidate in candidates:
        cid = candidate.get("candidate_id")
        symbol = candidate.get("symbol")
        cap = _integer_epoch(candidate.get("captured_epoch"))
        if not isinstance(cid, str) or not cid or not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
            raise ValueError("INVALID_CANDIDATE_ID_OR_SYMBOL")
        if cap > cutoff:
            raise ValueError("FUTURE_CANDIDATE")
        start = math.ceil(cap / INTERVAL_SECONDS) * INTERVAL_SECONDS
        # Need at most 16 candles after the next 15m boundary for 240m.
        # A candle is only eligible once its entire 900s interval is closed.
        end = min(start + 16 * INTERVAL_SECONDS, math.floor(cutoff / INTERVAL_SECONDS) * INTERVAL_SECONDS)
        if end > start:
            symbols[symbol].append((int(start), int(end)))
    raw_bars = {}
    coverage = {}
    for symbol, spans in sorted(symbols.items()):
        lo, hi = min(x[0] for x in spans), max(x[1] for x in spans)
        if hi - lo > 31 * 86400:
            raise ValueError("PER_SYMBOL_HISTORY_WINDOW_TOO_WIDE")
        expected = set()
        for start, end in spans:
            expected.update(range(start, end, INTERVAL_SECONDS))
        cursor = lo
        market = {}
        while cursor < hi:
            window_end = min(cursor + MAX_BARS * INTERVAL_SECONDS, hi)
            payload = fetch(symbol, cursor, window_end)
            if not isinstance(payload, list):
                raise ValueError("PUBLIC_API_INVALID_PAYLOAD")
            for item in payload:
                if not isinstance(item, list) or len(item) < 5:
                    raise ValueError("PUBLIC_API_INVALID_CANDLE")
                ts_raw = item[0]
                if isinstance(ts_raw, bool):
                    raise ValueError("PUBLIC_API_INVALID_TIMESTAMP")
                try:
                    open_ms = int(ts_raw)
                    vals = [float(item[i]) for i in (1, 2, 3, 4)]
                except (ValueError, TypeError) as exc:
                    raise ValueError("PUBLIC_API_INVALID_CANDLE_NUMBERS") from exc
                if open_ms % (INTERVAL_SECONDS * 1000) != 0:
                    raise ValueError("PUBLIC_API_BAD_KLINE_ALIGNMENT")
                ts = open_ms // 1000
                op, h, l, cl = vals
                if (not all(math.isfinite(v) and v > 0 for v in vals)
                        or not (l <= min(op, cl) <= max(op, cl) <= h)):
                    raise ValueError("PUBLIC_API_BAD_OHLC")
                if ts < cursor or ts >= window_end:
                    raise ValueError("PUBLIC_API_OUT_OF_REQUEST_WINDOW")
                if ts in market:
                    raise ValueError("PUBLIC_API_DUPLICATE_BAR")
                market[ts] = {"symbol": symbol, "ts": ts, "o": op, "h": h, "l": l, "c": cl}
            cursor = window_end
        have = expected & market.keys()
        coverage[symbol] = {"expected": len(expected), "present": len(have),
                            "missing": len(expected - have)}
        for ts in sorted(have):
            raw_bars[(symbol, ts)] = market[ts]
    dataset = [raw_bars[key] for key in sorted(raw_bars)]
    canonical = "".join(json.dumps(row, allow_nan=False, sort_keys=True) + "\n" for row in dataset)
    manifest = {
        "source": "BINANCE_USDM_PUBLIC_FAPI_V1_KLINES",
        "interval": "15m", "as_of_epoch": cutoff,
        "bars": len(dataset), "symbols": len(coverage), "coverage": coverage,
        "sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "expected_missing_bars": sum(x["missing"] for x in coverage.values()),
        "not_realized_pnl": True, "live_allowed": False,
    }
    return dataset, manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates", required=True, type=Path)
    ap.add_argument("--as-of-epoch", required=True, type=float)
    ap.add_argument("--prefix", required=True, type=Path)
    args = ap.parse_args()
    candidates = []
    for number, line in enumerate(args.candidates.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("JSON object expected")
            candidates.append(item)
        except (ValueError, TypeError) as exc:
            ap.error(f"input line {number}: {exc}")
    bars, manifest = download(candidates, as_of=args.as_of_epoch)
    args.prefix.parent.mkdir(parents=True, exist_ok=True)
    args.prefix.with_suffix(".jsonl").write_text(
        "".join(json.dumps(x, sort_keys=True, allow_nan=False) + "\n" for x in bars), encoding="utf-8")
    args.prefix.with_suffix(".json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
