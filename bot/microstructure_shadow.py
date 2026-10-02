"""Read-only Binance USD-M microstructure features for SHADOW research.

No feature in this module authorizes, sizes, places, amends or cancels orders.
Snapshots are causal: only book/trade observations available at collection time
are accepted, with explicit freshness and provenance.
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Mapping, Sequence

from bot.feature_contract import FeatureSchema, FeatureSpec


MICROSTRUCTURE_FEATURE_SCHEMA = FeatureSchema(
    version="microstructure-v1",
    features=(
        FeatureSpec("spread_bps"),
        FeatureSpec("book_imbalance"),
        FeatureSpec("taker_pressure"),
        FeatureSpec("depth_notional_10bps"),
        FeatureSpec("depth_notional_100bps"),
        FeatureSpec("microstructure_alignment"),
    ),
)


@dataclass(frozen=True)
class MicrostructureSnapshot:
    symbol: str
    observed_at_ms: int
    book_event_ms: int
    trade_event_ms: int
    mid_price: float
    spread_bps: float
    book_imbalance: float
    taker_pressure: float
    depth_notional_10bps: float
    depth_notional_100bps: float
    microstructure_alignment: float
    complete: bool
    source: str = "BINANCE_USDM_PUBLIC"
    execution_effect: str = "NONE"
    promotion_authority: bool = False

    def as_dict(self) -> dict:
        return asdict(self)

    def feature_values(self) -> dict[str, float]:
        return MICROSTRUCTURE_FEATURE_SCHEMA.validate({
            "spread_bps": self.spread_bps,
            "book_imbalance": self.book_imbalance,
            "taker_pressure": self.taker_pressure,
            "depth_notional_10bps": self.depth_notional_10bps,
            "depth_notional_100bps": self.depth_notional_100bps,
            "microstructure_alignment": self.microstructure_alignment,
        })

    def feature_fingerprint(self) -> str:
        return MICROSTRUCTURE_FEATURE_SCHEMA.fingerprint(
            self.feature_values(),
            symbol=self.symbol,
            decision_ts=self.observed_at_ms,
        )


def _finite(value: object, *, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} is not numeric") from exc
    if not math.isfinite(out):
        raise ValueError(f"{name} is not finite")
    return out


def _levels(rows: object, *, side: str) -> list[tuple[float, float]]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError(f"{side} levels missing")
    out: list[tuple[float, float]] = []
    for row in rows:
        if not isinstance(row, Sequence) or len(row) < 2:
            raise ValueError(f"invalid {side} level")
        price = _finite(row[0], name=f"{side}_price")
        qty = _finite(row[1], name=f"{side}_qty")
        if price <= 0 or qty < 0:
            raise ValueError(f"invalid {side} level")
        if qty:
            out.append((price, qty))
    if not out:
        raise ValueError(f"{side} levels empty")
    return out


def _notional_within(
    levels: Sequence[tuple[float, float]],
    *,
    mid: float,
    band: float,
) -> float:
    return sum(
        price * qty
        for price, qty in levels
        if abs(price / mid - 1.0) <= band
    )


def _bounded_ratio(a: float, b: float) -> float:
    total = a + b
    if total <= 0:
        return 0.0
    return max(-1.0, min(1.0, (a - b) / total))


def build_snapshot(
    symbol: str,
    depth: Mapping[str, object],
    trades: Sequence[Mapping[str, object]],
    *,
    observed_at_ms: int | None = None,
    max_age_ms: int = 5_000,
) -> MicrostructureSnapshot:
    """Build one fail-closed causal snapshot from public Binance payloads."""
    sym = str(symbol or "").upper()
    if not sym:
        raise ValueError("symbol required")
    now_ms = int(observed_at_ms if observed_at_ms is not None else time.time() * 1000)
    if max_age_ms <= 0:
        raise ValueError("max_age_ms must be positive")

    bids = _levels(depth.get("bids"), side="bid")
    asks = _levels(depth.get("asks"), side="ask")
    best_bid = max(price for price, _qty in bids)
    best_ask = min(price for price, _qty in asks)
    if best_bid > best_ask:
        raise ValueError("crossed order book")
    mid = (best_bid + best_ask) / 2.0
    spread_bps = (best_ask - best_bid) / mid * 10_000.0

    book_event_ms = int(
        depth.get("E")
        or depth.get("T")
        or depth.get("eventTime")
        or now_ms
    )
    if book_event_ms > now_ms:
        raise ValueError("future book timestamp")
    if now_ms - book_event_ms > max_age_ms:
        raise ValueError("stale order book")

    bid_10 = _notional_within(bids, mid=mid, band=0.001)
    ask_10 = _notional_within(asks, mid=mid, band=0.001)
    bid_100 = _notional_within(bids, mid=mid, band=0.01)
    ask_100 = _notional_within(asks, mid=mid, band=0.01)
    book_imbalance = _bounded_ratio(bid_10, ask_10)

    buy_notional = 0.0
    sell_notional = 0.0
    latest_trade_ms = 0
    for trade in trades:
        if not isinstance(trade, Mapping):
            raise ValueError("invalid trade row")
        ts = int(trade.get("T") or trade.get("time") or 0)
        if ts <= 0 or ts > now_ms:
            raise ValueError("invalid trade timestamp")
        if now_ms - ts > max_age_ms:
            continue
        price = _finite(trade.get("p", trade.get("price")), name="trade_price")
        qty = _finite(trade.get("q", trade.get("qty")), name="trade_qty")
        if price <= 0 or qty < 0:
            raise ValueError("invalid trade geometry")
        latest_trade_ms = max(latest_trade_ms, ts)
        notional = price * qty
        # Binance aggTrade: m=True means buyer is maker, so aggressor is seller.
        if bool(trade.get("m", False)):
            sell_notional += notional
        else:
            buy_notional += notional

    if latest_trade_ms <= 0:
        raise ValueError("no fresh trades")
    taker_pressure = _bounded_ratio(buy_notional, sell_notional)
    alignment = max(
        -1.0,
        min(1.0, 0.6 * book_imbalance + 0.4 * taker_pressure),
    )
    return MicrostructureSnapshot(
        symbol=sym,
        observed_at_ms=now_ms,
        book_event_ms=book_event_ms,
        trade_event_ms=latest_trade_ms,
        mid_price=mid,
        spread_bps=spread_bps,
        book_imbalance=book_imbalance,
        taker_pressure=taker_pressure,
        depth_notional_10bps=bid_10 + ask_10,
        depth_notional_100bps=bid_100 + ask_100,
        microstructure_alignment=alignment,
        complete=True,
    )


async def collect_binance_snapshot(
    client,
    symbol: str,
    *,
    depth_limit: int = 100,
    trade_limit: int = 500,
    observed_at_ms: int | None = None,
    max_age_ms: int = 5_000,
) -> MicrostructureSnapshot:
    """Read public Binance depth/aggTrades and build a SHADOW-only snapshot."""
    if depth_limit not in {5, 10, 20, 50, 100, 500, 1000}:
        raise ValueError("unsupported Binance depth limit")
    if not 1 <= int(trade_limit) <= 1000:
        raise ValueError("trade_limit must be in [1,1000]")
    if not hasattr(client, "_get"):
        raise ValueError("client does not expose public market reads")

    from bot.binance import to_binance

    venue_symbol = to_binance(symbol)
    import asyncio
    depth, trades = await asyncio.gather(
        client._get(
            "/fapi/v1/depth",
            {"symbol": venue_symbol, "limit": int(depth_limit)},
            auth=False,
        ),
        client._get(
            "/fapi/v1/aggTrades",
            {"symbol": venue_symbol, "limit": int(trade_limit)},
            auth=False,
        ),
    )
    if not isinstance(depth, Mapping) or not isinstance(trades, list):
        raise ValueError("invalid Binance microstructure payload")
    return build_snapshot(
        venue_symbol,
        depth,
        trades,
        observed_at_ms=observed_at_ms,
        max_age_ms=max_age_ms,
    )
