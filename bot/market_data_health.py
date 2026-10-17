"""Canonical public market-data freshness authority (Binance USD-M).

One ``MarketDataHealth`` instance is owned by each exchange client
(``BinanceClient.market_data_health``). It is the single writer target for
public websocket freshness and the single source PilotGuard gate 11 reads.

Contract:
- only a *validated and fully processed* public market event (``kline`` or
  ``24hrTicker``) advances freshness; the client calls ``record_public_event``
  after its cache mutation succeeded;
- REST seeding, private user-data events, malformed frames, unknown events,
  handler errors and (re)connects never advance freshness;
- age is measured with ``time.monotonic()`` so wall-clock jumps/skew cannot
  turn stale data into PASS; the wall-clock value is exposed only for logs and
  legacy readers;
- never received -> ``no_market_data``; age > limit -> ``stale_market_data``;
  age == limit is still PASS (same ``>`` boundary as the historical gate).

No trading threshold lives here; the 120 s limit stays in ``bot.pilot``.
"""
from __future__ import annotations

import secrets
import threading
import time

PUBLIC_MARKET_EVENTS = frozenset({"kline", "24hrTicker"})

REASON_NO_DATA = "no_market_data"
REASON_STALE = "stale_market_data"
REASON_CLOCK = "clock_regression"


class MarketDataHealth:
    """Thread-safe, monotonic freshness state for public market data."""

    def __init__(self, *, monotonic=time.monotonic, wall=time.time):
        self._monotonic = monotonic
        self._wall = wall
        self._lock = threading.Lock()
        self.instance_id = secrets.token_hex(4)
        self._last_mono: float | None = None
        self._last_wall: float | None = None
        self._last_event: str | None = None
        self._last_symbol: str | None = None
        self._events = 0
        self._connected = False
        self._connection_epoch = 0
        self._events_this_connection = 0

    # ── writers ──────────────────────────────────────────────────────────
    def record_public_event(self, event: str, symbol: str) -> bool:
        """Advance freshness for one processed public market event."""
        if event not in PUBLIC_MARKET_EVENTS or not symbol:
            return False
        now_mono = self._monotonic()
        now_wall = self._wall()
        with self._lock:
            if self._last_mono is not None and now_mono < self._last_mono:
                # A monotonic source must never move backwards; refuse to
                # overwrite a newer observation with an older one.
                return False
            self._last_mono = now_mono
            self._last_wall = now_wall
            self._last_event = event
            self._last_symbol = str(symbol)
            self._events += 1
            self._events_this_connection += 1
            return True

    def mark_connected(self) -> int:
        """A (re)connect happened. Freshness is NOT advanced by this."""
        with self._lock:
            self._connected = True
            self._connection_epoch += 1
            self._events_this_connection = 0
            return self._connection_epoch

    def mark_disconnected(self) -> None:
        with self._lock:
            self._connected = False

    # ── readers ──────────────────────────────────────────────────────────
    def age_s(self) -> float | None:
        with self._lock:
            last = self._last_mono
        if last is None:
            return None
        return self._monotonic() - last

    @property
    def last_update_wall(self) -> float:
        """Wall-clock receipt time of the last valid event, 0.0 if never."""
        with self._lock:
            return float(self._last_wall or 0.0)

    def check(self, max_age_s: float) -> tuple[bool, str, float | None]:
        """Return ``(ok, reason, age_s)`` for a fail-closed freshness gate."""
        age = self.age_s()
        if age is None:
            return False, REASON_NO_DATA, None
        if age < 0:
            return False, REASON_CLOCK, age
        if age > float(max_age_s):
            return False, REASON_STALE, age
        return True, "fresh", age

    def snapshot(self) -> dict:
        age = self.age_s()
        with self._lock:
            return {
                "instance": self.instance_id,
                "connected": self._connected,
                "connection_epoch": self._connection_epoch,
                "events_total": self._events,
                "events_this_connection": self._events_this_connection,
                "last_event": self._last_event,
                "last_symbol": self._last_symbol,
                "last_update_wall": float(self._last_wall or 0.0),
                "age_s": age,
            }
