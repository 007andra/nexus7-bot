"""Authority of an exchange position snapshot (F-014).

UNKNOWN != FLAT. A symbol missing from a normalized position list proves the
exchange is flat for it only when every row of the response was validated
(INV-POSITION-AUTHORITY-001). A row that cannot be interpreted never
disappears silently: the read is reported through the same exception contract
callers already use for an unavailable positions payload, so no caller can
infer a close, realize PnL, retire protection or persist a close from it
(INV-POSITION-CLOSE-001, INV-POSITION-PERSISTENCE-001).

Snapshot states:
  VALID_COMPLETE  every row valid (open rows returned, valid zero rows = flat)
  VALID_EMPTY     the exchange returned a well-formed empty list
  PARTIAL_INVALID response received, >=1 row rejected  -> raises
  READ_FAILED     envelope missing/malformed (REST/auth/rate-limit) -> raises
"""
from __future__ import annotations

import math

from bot.logger import log

VALID_COMPLETE = "VALID_COMPLETE"
VALID_EMPTY = "VALID_EMPTY"
PARTIAL_INVALID = "PARTIAL_INVALID"
READ_FAILED = "READ_FAILED"


class PositionSnapshotUnconfirmed(RuntimeError):
    """The position read has no authority for flat/close conclusions.

    ``valid_rows`` (normalized, informational only), ``unknown_symbols``
    (identified rows that were rejected) and ``unidentified_rows`` (rows whose
    symbol could not be identified) describe the partial read.
    """

    def __init__(self, state, *, valid_rows=(), unknown_symbols=(),
                 unidentified_rows=0, rejected=()):
        self.state = state
        self.valid_rows = list(valid_rows)
        self.unknown_symbols = tuple(sorted(set(unknown_symbols)))
        self.unidentified_rows = int(unidentified_rows)
        self.rejected = tuple(rejected)
        super().__init__(
            f"POSITIONS_UNCONFIRMED: state={state} "
            f"unknown_symbols={','.join(self.unknown_symbols) or 'NONE'} "
            f"unidentified_rows={self.unidentified_rows}"
        )


class _RowRejected(ValueError):
    def __init__(self, field, reason):
        super().__init__(f"{field}:{reason}")
        self.field = field
        self.reason = reason


def _number(row, field, *, required=False, positive=False, default=0.0):
    value = row.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise _RowRejected(field, "missing")
        return float(default)
    if isinstance(value, bool):
        raise _RowRejected(field, "not_numeric")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise _RowRejected(field, "not_numeric") from None
    if not math.isfinite(number):
        raise _RowRejected(field, "non_finite")
    if positive and number <= 0:
        raise _RowRejected(field, "not_positive")
    return number


def _kucoin_row(raw, to_standard):
    """Return the normalized open row, or None for a valid zero (flat) row."""
    qty = _number(raw, "currentQty", required=True)      # sign = side
    if qty == 0:
        return None
    entry = _number(raw, "avgEntryPrice", required=True, positive=True)
    liquidation = _number(raw, "liquidationPrice")
    return {
        "symbol": to_standard(str(raw["symbol"])),
        "side": "Buy" if qty > 0 else "Sell",
        "size": abs(qty),
        "entryPrice": entry,
        "avgPrice": entry,
        "markPrice": _number(raw, "markPrice"),
        "unrealisedPnl": _number(raw, "unrealisedPnl"),
        "leverage": _number(raw, "realLeverage", default=1.0),
        "liquidationPrice": liquidation,
        "liqPrice": liquidation,
        "stopLoss": _number(raw, "stopLoss"),
        "takeProfit": _number(raw, "takeProfit"),
        "posMargin": _number(raw, "posMargin"),
    }


def normalize_kucoin_positions(raw_rows, to_standard, *, source="kucoin.get_positions"):
    """Validate every KuCoin position row; never drop one silently."""
    positions, unknown, rejected = [], set(), []
    unidentified = 0
    for raw in raw_rows:
        symbol = raw.get("symbol") if isinstance(raw, dict) else None
        identified = isinstance(symbol, str) and symbol.strip().isalnum()
        try:
            if not isinstance(raw, dict):
                raise _RowRejected("row", "not_mapping")
            if not identified:
                raise _RowRejected("symbol", "missing_or_invalid")
            row = _kucoin_row(raw, to_standard)
        except _RowRejected as exc:
            std = to_standard(symbol) if identified else ""
            if identified:
                unknown.add(std)
            else:
                unidentified += 1
            rejected.append((std or "?", exc.field, exc.reason))
            log.critical(
                "[POSITION_ROW_REJECTED] symbol=%s field=%s reason=%s source=%s "
                "snapshot_authority=%s",
                std or "UNIDENTIFIED", exc.field, exc.reason, source, PARTIAL_INVALID,
            )
            continue
        if row is not None:
            positions.append(row)

    if rejected:
        log.critical(
            "[POSITION_SNAPSHOT_PARTIAL] source=%s valid_open_rows=%s unknown_symbols=%s "
            "unidentified_rows=%s snapshot_authority=%s",
            source, len(positions), ",".join(sorted(unknown)) or "NONE", unidentified,
            PARTIAL_INVALID,
        )
        for symbol in sorted(unknown):
            log.critical(
                "[POSITION_STATE_UNKNOWN] symbol=%s reason=row_rejected source=%s "
                "assumed=POSSIBLY_OPEN", symbol, source,
            )
        log.critical(
            "[POSITION_CLOSE_INFERENCE_BLOCKED] source=%s scope=%s reason=%s "
            "close_inference=FORBIDDEN realized_pnl=NONE protection_retirement=NONE",
            source, "GLOBAL", "partial_invalid_snapshot",
        )
        raise PositionSnapshotUnconfirmed(
            PARTIAL_INVALID, valid_rows=positions, unknown_symbols=unknown,
            unidentified_rows=unidentified, rejected=rejected,
        )
    return positions
