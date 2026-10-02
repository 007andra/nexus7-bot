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
        # Position open time (ms); used only as REJECTION evidence of a reopen.
        "openingTimestamp": _number(raw, "openingTimestamp"),
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


class RiskReductionView:
    """Which exchange positions may be used for RISK-REDUCING actions (NOVO-01).

    Question A (may a symbol be concluded closed?) still needs an authoritative
    snapshot. Question B (may a validly identified open position be reduced or
    protected?) does not: one malformed row must never freeze the reduction of
    every other, validly identified position.

      rows               valid OPEN rows whose symbol has no rejected row
      unknown_symbols    identified but rejected -> never mutated, never flat
      unidentified_rows  rows whose symbol is unknowable -> no symbol is
                         provably flat while > 0 (the row may be any symbol)

    INV-RISK-REDUCTION-PARTIAL-SNAPSHOT-001, INV-PARTIAL-SNAPSHOT-NO-FLAT-001,
    INV-UNKNOWN-NO-MUTATION-001.
    """

    __slots__ = ("state", "rows", "unknown_symbols", "unidentified_rows")

    def __init__(self, state, rows=(), unknown_symbols=(), unidentified_rows=0):
        self.state = state
        self.unknown_symbols = tuple(sorted(set(unknown_symbols)))
        self.unidentified_rows = int(unidentified_rows)
        self.rows = [r for r in rows if r.get("symbol") not in self.unknown_symbols]

    @property
    def readable(self):
        return self.state != READ_FAILED

    @property
    def authoritative(self):
        return self.state in (VALID_COMPLETE, VALID_EMPTY)

    def symbols(self):
        return {str(r.get("symbol")) for r in self.rows}

    def row(self, symbol):
        """The single valid row of ``symbol``; None when absent/unknown/ambiguous."""
        if symbol in self.unknown_symbols:
            return None
        matches = [r for r in self.rows if r.get("symbol") == symbol]
        return matches[0] if len(matches) == 1 else None

    def flat_proven(self, symbol):
        """Absence proves flatness only for an identified, readable response."""
        return (self.readable and self.unidentified_rows == 0
                and symbol not in self.unknown_symbols and symbol not in self.symbols())

    def unknown_remains(self):
        return bool(self.unknown_symbols) or self.unidentified_rows > 0


def _open(rows):
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("symbol"):
            raise ValueError("position row malformed")
        try:
            if float(row.get("size") or 0) == 0:
                continue
        except (TypeError, ValueError):
            pass   # unparseable size is kept and fails closed downstream
        out.append(row)
    return out


async def read_for_risk_reduction(client, *, source):
    """Canonical read for risk-reducing consumers; never raises.

    A PARTIAL_INVALID read yields its validly identified rows; anything else
    that fails (REST/auth/payload) is READ_FAILED with no usable rows.
    """
    try:
        rows = await client.get_positions()
        if not isinstance(rows, list):
            raise ValueError("positions payload is not a list")
        rows = _open(rows)
    except PositionSnapshotUnconfirmed as exc:
        if exc.state != PARTIAL_INVALID:
            return RiskReductionView(READ_FAILED)
        try:
            valid = _open(exc.valid_rows)
        except ValueError:
            return RiskReductionView(READ_FAILED)
        view = RiskReductionView(PARTIAL_INVALID, valid, exc.unknown_symbols, exc.unidentified_rows)
        log.critical(
            "[POSITION_SNAPSHOT_PARTIAL] source=%s valid_symbols=%s unknown_symbols=%s "
            "unidentified_rows=%s risk_reduction=VALID_ROWS_ONLY close_inference=FORBIDDEN",
            source, ",".join(sorted(view.symbols())) or "NONE",
            ",".join(view.unknown_symbols) or "NONE", view.unidentified_rows,
        )
        return view
    except Exception as exc:
        log.critical("[POSITION_SNAPSHOT_READ_FAILED] source=%s error=%s risk_reduction=NONE",
                     source, type(exc).__name__)
        return RiskReductionView(READ_FAILED)
    return RiskReductionView(VALID_COMPLETE if rows else VALID_EMPTY, rows)
