"""Correct trailing-stop lock geometry.

The legacy implementation multiplied peak price by TRAILING_LOCK*0.1, which
turned a 25% lock setting into a 2.5% price offset. At 50x that is economically
very different from locking a fraction of the favorable excursion.

This hardening interprets TRAILING_LOCK as the fraction of peak favorable
excursion that may be given back. With 0.25, the stop retains 75% of MFE.
Native protective SL/TP and exchange routing are untouched.
"""
from __future__ import annotations


def install(Position, cfg, log) -> None:
    if getattr(Position, "_trailing_safety_hardening_installed", False):
        return

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
        # Q-01: favorable excursion in PRICE units from the trade's best price.
        # peak_pnl / qty inflated it after a partial exit (qty halves, the
        # full-size peak_pnl stays) and proposed stops beyond the market.
        from bot.exit_geometry import log_geometry, peak_excursion, stop_on_valid_side
        favorable_excursion = peak_excursion(self)
        retained_excursion = favorable_excursion * (1.0 - giveback)

        if self.direction == "LONG":
            new_sl = max(self.entry + retained_excursion, self.sl)
        else:
            new_sl = min(self.entry - retained_excursion, self.sl)
        # INV-TRAILING-VALIDITY-001 (defense in depth): never propose a stop on
        # the invalid side of the market; the existing stop stays in force.
        price = getattr(self, "current_price", None)
        if not price:
            # pnl is measured on the held qty, so it fixes the current price.
            move = self.pnl / self.qty
            price = self.entry + move if self.direction == "LONG" else self.entry - move
        if not stop_on_valid_side(self.direction, new_sl, float(price)):
            log_geometry(self, "TRAILING_REJECTED_INVALID_SIDE", candidate_sl=new_sl,
                         action="keep_existing_stop")
            return None
        return new_sl

    Position.calc_trailing_sl = calc_trailing_sl
    Position._trailing_safety_hardening_installed = True
    log.warning(
        "[TRAILING_SAFETY] installed semantics=peak_excursion_giveback "
        "native_sl_tp_unchanged=true thresholds_unchanged=true leverage_unchanged=true"
    )
