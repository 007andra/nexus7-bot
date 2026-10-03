"""Observation-only Binance USD-M best bid/offer feed (SHADOW ONLY).

Source: the public ``<symbol>@bookTicker`` stream, which Binance pushes on
every change of best bid/ask price or quantity. Payload::

    {"e":"bookTicker","u":<update id>,"E":<event ms>,"T":<transaction ms>,
     "s":"BNBUSDT","b":"<bid>","B":"<bid qty>","a":"<ask>","A":"<ask qty>"}

``bookTicker`` is a ``/public`` route stream. The trading market-data
connection uses ``/market`` (kline, 24hrTicker) and is NOT touched: this feed
owns a separate connection, loop, parser and cache. Nothing in the trading
path reads this cache (enforced by tests); the only consumer is the shadow
cost comparison (``bbo_cost_shadow_runtime``).

Freshness policy (``snapshot``): a quote is valid only when

* it belongs to the CURRENT connection generation (a reconnect invalidates
  every earlier quote until that symbol pushes again);
* the connection delivered any frame within ``BBO_CONN_SILENCE_MS``. Because
  bookTicker only pushes on change, a quiet symbol keeps a current quote as
  long as its connection is alive; the combined stream of the 25 configured
  symbols (BTC/ETH/SOL among them) carries many frames per second, so 5 s of
  total silence means a dead socket, not a quiet market;
* the symbol itself updated within ``BBO_SYMBOL_MAX_AGE_MS`` (30 s safety cap
  against a silently dropped subscription);
* exchange-to-local lag (receive wall time - event time) was at most
  ``BBO_MAX_EXCHANGE_LAG_MS`` when received.

Thresholds are env-configurable research parameters and the feed logs
per-connection frame rates so they can be validated prospectively.

Errors never propagate: shadow data is fail-open (``SHADOW_DATA_UNAVAILABLE``)
and can never block or alter LIVE trading.
shadow_only=true decision_effect=NONE execution_effect=NONE
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
import json
import math
import os
import threading
import time

PUBLIC_ROUTE = "/public"


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


BBO_CONN_SILENCE_MS = _env_int("BBO_CONN_SILENCE_MS", 5_000)
BBO_SYMBOL_MAX_AGE_MS = _env_int("BBO_SYMBOL_MAX_AGE_MS", 30_000)
BBO_MAX_EXCHANGE_LAG_MS = _env_int("BBO_MAX_EXCHANGE_LAG_MS", 2_000)
BBO_MAX_FUTURE_SKEW_MS = _env_int("BBO_MAX_FUTURE_SKEW_MS", 1_000)
BBO_RECONNECT_SILENCE_S = 30.0
MAX_PRICE = 1e9
MAX_QTY = 1e15


class BBOParseError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class BBOQuote:
    symbol: str
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float
    update_id: int
    exchange_ms: int
    received_wall_ms: int
    received_mono_ns: int
    generation: int


@dataclass(frozen=True)
class BBOView:
    symbol: str
    valid: bool
    reason: str          # OK | MISSING_BOOK | STALE | INVALID_BOOK
    detail: str          # finer cause (e.g. RECONNECT, CONN_SILENT, SYMBOL_AGE)
    quote: BBOQuote | None
    age_ms: int | None


def _number(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise BBOParseError("INVALID_BOOK")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise BBOParseError("INVALID_BOOK") from exc
    if not math.isfinite(out):
        raise BBOParseError("INVALID_BOOK")
    return out


def _int(value, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise BBOParseError(reason)
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise BBOParseError(reason) from exc
    if out <= 0:
        raise BBOParseError(reason)
    return out


def parse_book_ticker(message: dict, *, generation: int, received_wall_ms: int,
                      received_mono_ns: int, known_symbols: frozenset) -> BBOQuote:
    """Strictly parse one bookTicker frame (combined-stream wrapper accepted)."""
    data = message.get("data", message) if isinstance(message, dict) else None
    if not isinstance(data, dict) or data.get("e") != "bookTicker":
        raise BBOParseError("NOT_BOOK_TICKER")
    symbol = str(data.get("s") or "").upper()
    if symbol not in known_symbols:
        raise BBOParseError("UNKNOWN_SYMBOL")
    bid, ask = _number(data.get("b"), "b"), _number(data.get("a"), "a")
    bid_qty, ask_qty = _number(data.get("B"), "B"), _number(data.get("A"), "A")
    if bid <= 0 or ask <= 0 or bid_qty <= 0 or ask_qty <= 0:
        raise BBOParseError("INVALID_BOOK")
    if ask < bid:
        raise BBOParseError("INVALID_BOOK")
    if max(bid, ask) > MAX_PRICE or max(bid_qty, ask_qty) > MAX_QTY:
        raise BBOParseError("INVALID_BOOK")
    update_id = _int(data.get("u"), "INVALID_UPDATE_ID")
    exchange_ms = _int(data.get("T") or data.get("E"), "INVALID_TIMESTAMP")
    if exchange_ms > int(received_wall_ms) + BBO_MAX_FUTURE_SKEW_MS:
        raise BBOParseError("INVALID_TIMESTAMP")
    if int(received_wall_ms) - exchange_ms > BBO_MAX_EXCHANGE_LAG_MS:
        raise BBOParseError("STALE_ON_ARRIVAL")
    return BBOQuote(symbol, bid, ask, bid_qty, ask_qty, update_id, exchange_ms,
                    int(received_wall_ms), int(received_mono_ns), int(generation))


class BBOCache:
    """Thread-safe, generation-aware, monotonic BBO cache. Consumers get copies."""

    def __init__(self, symbols):
        self._lock = threading.Lock()
        self.known_symbols = frozenset(str(s).upper() for s in symbols)
        self._generation = 0
        self._connected = False
        self._last_frame_mono_ns: int | None = None
        self._quotes: dict[str, BBOQuote] = {}
        self.stats: Counter = Counter()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def begin_generation(self) -> int:
        """New connection: every earlier quote becomes non-current."""
        with self._lock:
            self._generation += 1
            self._connected = True
            self._last_frame_mono_ns = None
            self.stats["generations"] += 1
            return self._generation

    def end_generation(self, generation: int) -> None:
        with self._lock:
            if generation == self._generation:
                self._connected = False

    def note_frame(self, generation: int, mono_ns: int) -> bool:
        with self._lock:
            if generation != self._generation:
                self.stats["stale_generation_frame"] += 1
                return False
            self._last_frame_mono_ns = int(mono_ns)
            return True

    def update(self, quote: BBOQuote) -> tuple[bool, str]:
        with self._lock:
            if quote.generation != self._generation:
                self.stats["rejected_old_generation"] += 1
                return False, "OLD_GENERATION"
            current = self._quotes.get(quote.symbol)
            if current is not None and current.generation == quote.generation \
                    and quote.update_id <= current.update_id:
                self.stats["rejected_non_monotonic"] += 1
                return False, "NON_MONOTONIC"
            self._quotes[quote.symbol] = quote
            self.stats["accepted"] += 1
            return True, "OK"

    def snapshot(self, symbol: str, *, now_mono_ns: int | None = None) -> BBOView:
        sym = str(symbol or "").upper()
        now = time.monotonic_ns() if now_mono_ns is None else int(now_mono_ns)
        with self._lock:
            quote = self._quotes.get(sym)
            gen, connected, last_frame = self._generation, self._connected, self._last_frame_mono_ns
        if quote is None:
            return BBOView(sym, False, "MISSING_BOOK", "NEVER_RECEIVED", None, None)
        age_ms = max(0, (now - quote.received_mono_ns) // 1_000_000)
        if quote.generation != gen:
            return BBOView(sym, False, "STALE", "RECONNECT", quote, age_ms)
        if not connected or last_frame is None:
            return BBOView(sym, False, "STALE", "DISCONNECTED", quote, age_ms)
        if (now - last_frame) // 1_000_000 > BBO_CONN_SILENCE_MS:
            return BBOView(sym, False, "STALE", "CONN_SILENT", quote, age_ms)
        if age_ms > BBO_SYMBOL_MAX_AGE_MS:
            return BBOView(sym, False, "STALE", "SYMBOL_AGE", quote, age_ms)
        if not (quote.bid > 0 and quote.ask >= quote.bid):
            return BBOView(sym, False, "INVALID_BOOK", "CACHE_INVARIANT", quote, age_ms)
        return BBOView(sym, True, "OK", "OK", quote, age_ms)

    def ingest(self, message: dict, *, generation: int, received_wall_ms: int,
               received_mono_ns: int) -> tuple[bool, str]:
        """Parse + update; never raises (shadow fail-open)."""
        try:
            if not self.note_frame(generation, received_mono_ns):
                return False, "OLD_GENERATION"
            quote = parse_book_ticker(message, generation=generation,
                                      received_wall_ms=received_wall_ms,
                                      received_mono_ns=received_mono_ns,
                                      known_symbols=self.known_symbols)
        except BBOParseError as exc:
            self.stats[f"rejected_{exc.reason}"] += 1
            return False, exc.reason
        except Exception as exc:  # noqa: BLE001 - shadow data is fail-open
            self.stats[f"rejected_{type(exc).__name__}"] += 1
            return False, "SHADOW_DATA_UNAVAILABLE"
        return self.update(quote)


def stream_url(ws_base: str, symbols) -> str:
    streams = sorted({f"{str(s).lower()}@bookTicker" for s in symbols})
    return f"{ws_base.rstrip('/')}{PUBLIC_ROUTE}/stream?streams=" + "/".join(streams)


async def run_feed(cache: BBOCache, url: str, log, *, connect=None,
                   stats_every_s: float = 300.0) -> None:
    """Own reconnect loop. Never raises except on cancellation."""
    if connect is None:
        import websockets
        connect = websockets.connect
    retry = 1.0
    last_stats = time.monotonic()
    while True:
        generation = None
        try:
            async with connect(url, ping_interval=180, ping_timeout=600,
                               close_timeout=5, max_queue=4096) as ws:
                generation = cache.begin_generation()
                log.info("[BBO_SHADOW_FEED] event=connected route=%s generation=%d "
                         "symbols=%d shadow_only=true decision_effect=NONE execution_effect=NONE",
                         PUBLIC_ROUTE, generation, len(cache.known_symbols))
                retry = 1.0
                frames = ws.__aiter__()
                while True:
                    try:
                        raw = await asyncio.wait_for(frames.__anext__(), timeout=BBO_RECONNECT_SILENCE_S)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        log.warning("[BBO_SHADOW_FEED] event=silent_stream action=reconnect "
                                    "decision_effect=NONE execution_effect=NONE")
                        break
                    try:
                        message = json.loads(raw)
                    except Exception:  # noqa: BLE001 - shadow data is fail-open
                        cache.stats["rejected_JSON"] += 1
                        continue
                    cache.ingest(message, generation=generation,
                                 received_wall_ms=int(time.time() * 1000),
                                 received_mono_ns=time.monotonic_ns())
                    if time.monotonic() - last_stats >= stats_every_s:
                        last_stats = time.monotonic()
                        log.info("[BBO_SHADOW_FEED] event=stats generation=%d stats=%s "
                                 "decision_effect=NONE execution_effect=NONE",
                                 generation, dict(cache.stats))
        except asyncio.CancelledError:
            if generation is not None:
                cache.end_generation(generation)
            raise
        except Exception as exc:  # noqa: BLE001 - shadow data is fail-open
            log.warning("[BBO_SHADOW_FEED] event=reconnect error=%s "
                        "decision_effect=NONE execution_effect=NONE", type(exc).__name__)
        if generation is not None:
            cache.end_generation(generation)
        await asyncio.sleep(retry)
        retry = min(30.0, retry * 2)


__all__ = ["BBOCache", "BBOParseError", "BBOQuote", "BBOView", "parse_book_ticker",
           "run_feed", "stream_url", "BBO_CONN_SILENCE_MS", "BBO_SYMBOL_MAX_AGE_MS",
           "BBO_MAX_EXCHANGE_LAG_MS"]
