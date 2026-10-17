"""PAPER execution helper.

PAPER must exercise the same decision gates as production. It may simulate
exchange mutations, but it must not turn a production rejection into an
approval. The only PAPER-specific adjustment here is a fee-aware sizing cap so
a simulated order never assumes more cash than the observed account balance.

LIVE behavior is untouched.
"""
from __future__ import annotations


def install(log):
    from bot.risk import RiskManager
    from bot.config import cfg
    from bot.exchange import PAPER_TRADE, TAKER_FEE

    if getattr(RiskManager, "_paper_e2e_size_patched", False):
        return

    original_size = RiskManager.size

    def size_with_paper_fee_cap(self, symbol, entry, instruments,
                                size_mult=1.0, open_positions=None):
        qty = original_size(
            self, symbol, entry, instruments,
            size_mult=size_mult, open_positions=open_positions,
        )
        if not PAPER_TRADE or qty <= 0 or entry <= 0 or self.balance <= 0:
            return qty

        # Include opening taker fee in the cash requirement. This is a cap, not
        # a bypass: every upstream score/risk/liquidation decision remains
        # exactly as returned by the production implementation.
        per_unit_cash = entry * (1.0 / cfg.LEVERAGE + TAKER_FEE)
        if per_unit_cash <= 0:
            return qty

        max_affordable = self.balance / per_unit_cash
        if qty <= max_affordable:
            return qty

        info = instruments.get(symbol, {}) or {}
        multiplier = float(info.get("multiplier", 1) or 1)
        lot = float(info.get("minQty", 1) or 1)
        step = multiplier * lot
        if step > 0:
            from decimal import Decimal, ROUND_FLOOR
            q = Decimal(str(max_affordable))
            s = Decimal(str(step))
            max_affordable = float(
                (q / s).to_integral_value(rounding=ROUND_FLOOR) * s
            )

        capped = max(0.0, min(qty, max_affordable))
        log.info(
            "[PAPER] %s sizing cap fee-aware: %.8f -> %.8f "
            "(saldo=%.4f; gates de produção preservados)",
            symbol, qty, capped, self.balance,
        )
        return capped

    RiskManager.size = size_with_paper_fee_cap
    RiskManager._paper_e2e_size_patched = True

    if PAPER_TRADE:
        log.info(
            "🧪 PAPER fidelity: score/risk/liquidation usam os mesmos gates de "
            "produção; apenas execução na exchange é simulada"
        )
