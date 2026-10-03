"""Exit-quality and PnL attribution diagnostics for completed trades."""
from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import mean
from typing import Iterable


@dataclass(frozen=True)
class ExitEvidence:
    trade_id: str
    exit_reason: str
    realized_r: float
    mfe_r: float
    mae_r: float
    gross_pnl: float
    fees: float
    funding_pnl: float
    entry_slippage_pnl: float
    exit_slippage_pnl: float

    def validate(self) -> "ExitEvidence":
        if not self.trade_id or not self.exit_reason:
            raise ValueError("trade identity and exit reason required")
        vals = (
            self.realized_r,
            self.mfe_r,
            self.mae_r,
            self.gross_pnl,
            self.fees,
            self.funding_pnl,
            self.entry_slippage_pnl,
            self.exit_slippage_pnl,
        )
        if not all(math.isfinite(float(value)) for value in vals):
            raise ValueError("non-finite exit evidence")
        return self


def trade_attribution(trade: ExitEvidence) -> dict[str, float | str]:
    x = trade.validate()
    execution_drag = (
        x.entry_slippage_pnl + x.exit_slippage_pnl - abs(x.fees)
    )
    net_pnl = x.gross_pnl + x.funding_pnl + execution_drag
    return {
        "trade_id": x.trade_id,
        "gross_price_pnl": x.gross_pnl,
        "entry_slippage_pnl": x.entry_slippage_pnl,
        "exit_slippage_pnl": x.exit_slippage_pnl,
        "fees_pnl": -abs(x.fees),
        "funding_pnl": x.funding_pnl,
        "execution_drag_pnl": execution_drag,
        "net_reconstructed_pnl": net_pnl,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


def exit_quality_report(
    trades: Iterable[ExitEvidence],
) -> dict[str, object]:
    vals = [trade.validate() for trade in trades]
    groups: dict[str, list[ExitEvidence]] = {}
    for trade in vals:
        groups.setdefault(trade.exit_reason, []).append(trade)

    by_reason = {}
    for reason, rows in sorted(groups.items()):
        captures = [
            row.realized_r / row.mfe_r
            for row in rows
            if row.mfe_r > 0
        ]
        by_reason[reason] = {
            "n": len(rows),
            "average_realized_r": mean(row.realized_r for row in rows),
            "average_mfe_r": mean(row.mfe_r for row in rows),
            "average_mae_r": mean(row.mae_r for row in rows),
            "average_profit_capture_ratio": (
                mean(captures) if captures else None
            ),
        }

    return {
        "trades": len(vals),
        "by_exit_reason": by_reason,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }
