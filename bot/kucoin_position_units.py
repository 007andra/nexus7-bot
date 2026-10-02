"""Normalize KuCoin Futures position quantities at the engine read boundary.

KuCoin ``currentQty`` is a native contract count, while BGX engine/risk/Position
quantities are base asset. The canonical KuCoin client intentionally remains the
exchange adapter; this runtime composition converts only inbound position rows
before they enter engine state.

No order, position, stop, leverage, threshold or exchange setting is mutated.
"""
from __future__ import annotations

from typing import Any

from bot.logger import log
from bot.quantity import contracts_to_base


class KuCoinPositionUnitAdapter:
    """Transparent client proxy whose ``get_positions`` returns base units."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    @property
    def entries_paused(self):
        return getattr(self._client, 'entries_paused', False)

    @entries_paused.setter
    def entries_paused(self, value):
        self._client.entries_paused = value

    async def get_positions(self) -> list:
        from bot.position_snapshot import PositionSnapshotUnconfirmed
        try:
            rows = await self._client.get_positions()
        except PositionSnapshotUnconfirmed as exc:
            # F-014: keep the non-authoritative contract; convert the valid rows
            # (informational only) to base units when possible.
            try:
                valid = self._normalize(exc.valid_rows)
            except Exception:
                valid = []
            raise PositionSnapshotUnconfirmed(
                exc.state, valid_rows=valid, unknown_symbols=exc.unknown_symbols,
                unidentified_rows=exc.unidentified_rows, rejected=exc.rejected,
            ) from exc
        if not isinstance(rows, list):
            raise RuntimeError("KuCoin positions payload is not a list")
        return self._normalize(rows)

    def _normalize(self, rows) -> list:
        instruments = getattr(self._client, "_instruments", None)
        if not isinstance(instruments, dict) or not instruments:
            raise RuntimeError("KuCoin instrument metadata unavailable for position normalization")

        normalized = []
        for raw in rows:
            if not isinstance(raw, dict):
                raise RuntimeError("KuCoin position row is not a mapping")
            row = dict(raw)
            symbol = str(row.get("symbol") or "")
            if not symbol:
                raise RuntimeError("KuCoin position row missing symbol")
            info = instruments.get(symbol)
            if not isinstance(info, dict):
                raise RuntimeError(
                    f"KuCoin instrument metadata unavailable for position symbol={symbol}"
                )

            contracts = row.get("size", 0)
            base_size = contracts_to_base(contracts, info)
            row["sizeContracts"] = float(contracts)
            row["size"] = base_size
            row["sizeUnit"] = "BASE_ASSET"
            normalized.append(row)

            log.debug(
                "[POSITION_UNIT_NORMALIZATION] symbol=%s contracts=%s multiplier=%s "
                "base_qty=%s execution_effect=NONE",
                symbol,
                contracts,
                info.get("multiplier"),
                base_size,
            )

        return normalized
