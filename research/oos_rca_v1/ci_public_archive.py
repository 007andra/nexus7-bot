"""Research-only public Binance USD-M 15m market archive for GitHub Actions.

No candidate/trading/account data. No credentials. No private APIs.
Only public Binance candle archives and GET /fapi/v1/klines.
Do not use returned prices as execution fills.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SYMBOLS = (
    "AAVEUSDT", "ADAUSDT", "APTUSDT", "ARBUSDT", "ATOMUSDT", "AVAXUSDT",
    "BTCUSDT", "DOGEUSDT", "DOTUSDT", "ETCUSDT", "ETHUSDT", "FILUSDT",
    "INJUSDT", "LINKUSDT", "LTCUSDT", "NEARUSDT", "OPUSDT", "PEOPLEUSDT",
    "SEIUSDT", "SOLUSDT", "SUIUSDT", "UNIUSDT",
)
DAYS = ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08")
AS_OF = int(datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc).timestamp())
DATA_VISION = "https://data.binance.vision/data/futures/um/daily/klines/"
PUBLIC_FAPI = "https://fapi.binance.com/fapi/v1/klines"
MAX_BYTES = 1500000


def _get(url, cap=MAX_BYTES):
    response = urlopen(Request(url, headers={"User-Agent": "NEXUS7-Public-Market-Research/1"}), timeout=18)
    with response:
        data = response.read(cap + 1)
    if len(data) > cap:
        raise ValueError("RESOURCE_TOO_LARGE")
    return data


def _day_range(day):
    first = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
    return first, first + 86400


def _row(symbol, ts_ms, vals):
    ts = int(ts_ms)
    if ts % 900000:
        raise ValueError("MISALIGNED_KLINE")
    a = [float(x) for x in vals]
    op, hi, lo, close = a
    if not (0 < lo <= min(op, close) <= max(op, close) <= hi):
        raise ValueError("BAD_OHLC")
    return {"symbol": symbol, "ts": ts // 1000, "o": op, "h": hi, "l": lo, "c": close}


def _fetch_zip(symbol, day):
    url = f"{DATA_VISION}{symbol}/15m/{symbol}-15m-{day}.zip"
    data = _get(url)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = archive.namelist()
        if len(files) != 1:
            raise ValueError("ZIP_CONTENT_INVALID")
        with archive.open(files[0]) as src:
            text = src.read(MAX_BYTES).decode("utf-8")
    output = []
    for fields in csv.reader(io.StringIO(text)):
        if not fields:
            continue
        try:
            output.append(_row(symbol, fields[0], fields[1:5]))
        except ValueError:
            if fields[0].lower() == "open_time":
                continue
            raise
    return output, url


def _fetch_rest(symbol, day):
    first, last = _day_range(day)
    params = urlencode({"symbol": symbol, "interval": "15m", "startTime": first * 1000,
                        "endTime": last * 1000 - 1, "limit": 1000})
    data = _get(PUBLIC_FAPI + "?" + params)
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("PUBLIC_KLINES_INVALID")
    return [_row(symbol, item[0], item[1:5]) for item in payload], PUBLIC_FAPI


def main():
    root = Path("public_market_archive")
    root.mkdir(parents=True, exist_ok=True)
    out = {}
    day_stats = []
    for symbol in SYMBOLS:
        for day in DAYS:
            first, last = _day_range(day)
            errors = []
            candles, source = None, None
            for label, func in (("binance_data_vision", _fetch_zip), ("binance_usdm_public_rest", _fetch_rest)):
                try:
                    candles, source = func(symbol, day)
                    break
                except (HTTPError, URLError, ValueError, TimeoutError, zipfile.BadZipFile, json.JSONDecodeError) as ex:
                    errors.append(f"{label}:{type(ex).__name__}")
            if candles is None:
                day_stats.append({"symbol": symbol, "day": day, "status": "UNAVAILABLE", "errors": errors})
                continue
            n = 0
            for candle in candles:
                ts = candle["ts"]
                if ts < first or ts >= last or ts + 900 > AS_OF:
                    continue
                key = (symbol, ts)
                if key in out:
                    raise ValueError("DUPLICATE_PUBLIC_CANDLE")
                out[key] = candle
                n += 1
            day_stats.append({"symbol": symbol, "day": day, "status": "AVAILABLE",
                              "bars": n, "source": source})
    data = [out[key] for key in sorted(out)]
    text = "".join(json.dumps(v, separators=(",", ":"), sort_keys=True) + "\n" for v in data)
    (root / "public_binance_usdm_15m.jsonl").write_text(text, encoding="utf-8")
    report = {
        "research_only": True,
        "account_or_candidate_data_included": False,
        "as_of_utc": datetime.fromtimestamp(AS_OF, timezone.utc).isoformat(),
        "symbols": len(SYMBOLS), "bars": len(data), "day_coverage": day_stats,
        "data_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "no_execution_fill_proof": True,
    }
    (root / "manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Public Binance USD-M market history: {len(data)} candles, no candidate data")
    if not data:
        raise RuntimeError("NO_VERIFIED_PUBLIC_BINANCE_MARKET_DATA")


if __name__ == "__main__":
    main()
