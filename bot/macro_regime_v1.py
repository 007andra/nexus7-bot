"""Macro context vector for research, never automatic trade direction."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class MacroZ:
    dxy: float
    treasury_yield: float
    vix: float
    nasdaq: float
    spx: float
    btc_dominance: float
    liquidity: float

    def validate(self) -> "MacroZ":
        if not all(
            math.isfinite(float(value))
            for value in (
                self.dxy,
                self.treasury_yield,
                self.vix,
                self.nasdaq,
                self.spx,
                self.btc_dominance,
                self.liquidity,
            )
        ):
            raise ValueError("macro z-scores must be finite")
        return self


def macro_context(snapshot: MacroZ) -> dict[str, object]:
    x = snapshot.validate()
    # Heuristic context pressure only; it is explicitly not a directional
    # authority and must be validated prospectively before model use.
    pressure = (
        -x.dxy
        - x.treasury_yield
        - x.vix
        + x.nasdaq
        + x.spx
        - x.btc_dominance
        + x.liquidity
    ) / 7.0
    label = (
        "RISK_ON_CONTEXT"
        if pressure >= 0.5
        else "RISK_OFF_CONTEXT"
        if pressure <= -0.5
        else "MIXED_CONTEXT"
    )
    return {
        "context_pressure": pressure,
        "context_label": label,
        "heuristic_only": True,
        "direction_authority": False,
        "promotion_authority": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }
