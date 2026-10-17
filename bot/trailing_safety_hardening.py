"""Correct trailing-stop lock geometry.

The legacy implementation multiplied peak price by TRAILING_LOCK*0.1, which
turned a 25% lock setting into a 2.5% price offset. At 50x that is economically
very different from locking a fraction of the favorable excursion.

This hardening interprets TRAILING_LOCK as the fraction of peak favorable
excursion that may be given back. With 0.25, the stop retains 75% of MFE.
Native protective SL/TP and exchange routing are untouched.

The excursion is tracked per unit of position. ``peak_pnl`` is in quote
currency at whatever qty was open when the peak happened; after a partial TP
or an exchange qty reconcile shrinks ``qty``, ``peak_pnl / qty`` overstated
the excursion (2x after a 50% partial) and placed the trailing stop beyond the
market, at or past TP.
"""
from __future__ import annotations


def install(Position, cfg, log) -> None:
    if getattr(Position, "_trailing_safety_hardening_installed", False):
        return

    original_update_pnl = getattr(Position, "update_pnl", None)

    def update_pnl(self, current_price):
        original_update_pnl(self, current_price)
        if self.qty > 0:
            # First update after construction or a durable restore seeds the
            # tracker from peak_pnl, so a pre-restart peak is not forgotten.
            self._peak_unit_excursion = max(_peak_excursion(self),
                                            self.pnl / self.qty)

    def _peak_excursion(self):
        tracked = getattr(self, "_peak_unit_excursion", None)
        if tracked is not None:
            return tracked
        # Restored or legacy object: peak_pnl may have accrued on a larger
        # qty than the current one. The larger base can only understate the
        # excursion, which keeps the stop on the protective side.
        base = max(self.qty, float(getattr(self, "qty_original", 0) or 0))
        return max(self.pnl / self.qty, self.peak_pnl / base)

    def calc_trailing_sl(self):
        if self.pnl <= 0 or self.tp == self.entry or self.qty <= 0:
            return None

        target = abs(self.tp - self.entry)
        if target <= 0:
            return None

        trigger_pnl = target * cfg.TRAILING_TRIGGER * self.qty
        if self.pnl < trigger_pnl:
            return None

        self.trailing_active = True
        giveback = max(0.0, min(1.0, float(cfg.TRAILING_LOCK)))
        favorable_excursion = _peak_excursion(self)
        retained_excursion = favorable_excursion * (1.0 - giveback)

        if self.direction == "LONG":
            new_sl = self.entry + retained_excursion
            return max(new_sl, self.sl)

        new_sl = self.entry - retained_excursion
        return min(new_sl, self.sl)

    if original_update_pnl is not None:
        Position.update_pnl = update_pnl
    Position.calc_trailing_sl = calc_trailing_sl
    Position._trailing_safety_hardening_installed = True
    log.warning(
        "[TRAILING_SAFETY] installed semantics=peak_excursion_giveback "
        "excursion_basis=per_unit "
        "native_sl_tp_unchanged=true thresholds_unchanged=true leverage_unchanged=true"
    )
