"""Point-in-time Binance USD-M instrument-rule snapshots for research parity.

The public exchangeInfo endpoint describes rules at observation time. NEXUS stores
the observation timestamp and hash explicitly so research never treats today's
filters as historical truth.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from math import isfinite
from typing import Mapping


@dataclass(frozen=True)
class InstrumentRuleSnapshot:
    symbol: str
    observed_at_ms: int
    status: str
    contract_type: str
    margin_asset: str
    price_tick: float
    min_price: float
    max_price: float
    qty_step: float
    min_qty: float
    max_qty: float
    min_notional: float
    onboard_date_ms: int | None

    def __post_init__(self) -> None:
        if not self.symbol or self.observed_at_ms <= 0:
            raise ValueError("invalid instrument snapshot identity")
        numeric = (
            self.price_tick, self.min_price, self.max_price,
            self.qty_step, self.min_qty, self.max_qty, self.min_notional,
        )
        if not all(isfinite(float(v)) and float(v) >= 0 for v in numeric):
            raise ValueError("invalid instrument rule")
        if self.price_tick <= 0 or self.qty_step <= 0:
            raise ValueError("tick and step must be positive")

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _filters(symbol_info: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    rows = symbol_info.get("filters", [])
    if not isinstance(rows, list):
        raise ValueError("exchangeInfo filters must be a list")
    out = {}
    for item in rows:
        if isinstance(item, Mapping) and item.get("filterType"):
            out[str(item["filterType"])] = item
    return out


def _float(mapping: Mapping[str, object], key: str, default: float = 0.0) -> float:
    try:
        return float(mapping.get(key, default) or default)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid exchangeInfo field {key}") from exc


def snapshot_symbol(
    symbol_info: Mapping[str, object],
    *,
    observed_at_ms: int,
) -> InstrumentRuleSnapshot:
    filters = _filters(symbol_info)
    price = filters.get("PRICE_FILTER", {})
    lot = filters.get("LOT_SIZE", {})
    notional = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
    return InstrumentRuleSnapshot(
        symbol=str(symbol_info.get("symbol", "")).upper(),
        observed_at_ms=int(observed_at_ms),
        status=str(symbol_info.get("status", "UNKNOWN")),
        contract_type=str(symbol_info.get("contractType", "UNKNOWN")),
        margin_asset=str(symbol_info.get("marginAsset", "")),
        price_tick=_float(price, "tickSize"),
        min_price=_float(price, "minPrice"),
        max_price=_float(price, "maxPrice"),
        qty_step=_float(lot, "stepSize"),
        min_qty=_float(lot, "minQty"),
        max_qty=_float(lot, "maxQty"),
        min_notional=_float(notional, "notional", _float(notional, "minNotional")),
        onboard_date_ms=(
            int(symbol_info["onboardDate"])
            if symbol_info.get("onboardDate") is not None else None
        ),
    )


def snapshot_exchange_info(
    payload: Mapping[str, object],
    *,
    observed_at_ms: int,
) -> tuple[InstrumentRuleSnapshot, ...]:
    symbols = payload.get("symbols", [])
    if not isinstance(symbols, list):
        raise ValueError("exchangeInfo symbols must be a list")
    snapshots = [
        snapshot_symbol(row, observed_at_ms=observed_at_ms)
        for row in symbols if isinstance(row, Mapping)
    ]
    names = [row.symbol for row in snapshots]
    if len(names) != len(set(names)):
        raise ValueError("duplicate symbol in exchangeInfo snapshot")
    return tuple(sorted(snapshots, key=lambda row: row.symbol))
