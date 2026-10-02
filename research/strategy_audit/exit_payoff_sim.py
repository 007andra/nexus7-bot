"""Deterministic LIVE exit-path simulator (research only, no I/O).

Uses the REAL ``bot.engine.Position`` with the REAL ``trailing_safety_hardening``
overlay. Exit rules are replicated from the LIVE-pilot runtime, in engine loop
order (engine.py run loop: partial -> trailing -> 2R):

* durable_partial_exit.check: when profit >= |entry - pos.sl| + entry*0.0003 and
  not tp1_hit -> close 50 % of qty_original, pos.qty = remainder, SL -> entry.
* engine._apply_trailing_stops + trailing_safety_hardening.calc_trailing_sl;
  native_stop_repair.set_stops rejects a LONG stop >= reference price
  (invalid_trigger_side) -> state unchanged.
* confirmed_rr_exit.check: profit >= 2*|entry - pos.sl| (current pos.sl) and
  distance > 0 -> close the remainder.
* operator_loss_policy: stagnation/CHoCH/regime exits disabled in LIVE pilot.
* Native TP (strategy TP) and the current exchange stop close the remainder.

Price paths are scripted (no market data). Results are per-path outcomes, NOT
performance estimates.

    PAPER_TRADE=true python -S research/strategy_audit/exit_payoff_sim.py
"""
import sys
import sysconfig

sys.path.insert(0, ".")
sys.path.append(sysconfig.get_paths()["purelib"])

from types import SimpleNamespace  # noqa: E402

from bot import engine as core  # noqa: E402
from bot import trailing_safety_hardening as ts  # noqa: E402
from bot.config import cfg  # noqa: E402
from bot.strategy import Signal  # noqa: E402

FEE, SLIP = 0.0006, 0.0005


class Pos(core.Position):
    pass


ts.install(Pos, cfg, SimpleNamespace(warning=lambda *a, **k: None))


def simulate(R, path_r, entry=100.0, risk=1.0, step=0.01):
    """LONG trade, 1R = ``risk`` price units; path_r = list of R waypoints."""
    sig = Signal("SIM", "LONG", entry, entry - risk, entry + R * risk, 0.8)
    pos = Pos(sig, 1.0)
    exchange_sl = entry - risk
    fills = []                       # (price, qty)
    events = []
    prices = []
    for a, b in zip(path_r, path_r[1:]):
        n = max(1, int(abs(b - a) / step))
        prices += [entry + risk * (a + (b - a) * i / n) for i in range(1, n + 1)]
    for px in prices:
        remaining = pos.qty
        if remaining <= 0:
            break
        # Exchange-native protection first (it lives on the exchange).
        if px <= exchange_sl:
            fills.append((exchange_sl, remaining)); events.append(f"STOP@{(exchange_sl-entry)/risk:+.2f}R")
            pos.qty = 0; break
        if px >= pos.tp:
            fills.append((pos.tp, remaining)); events.append(f"TP@{(pos.tp-entry)/risk:+.2f}R")
            pos.qty = 0; break
        pos.update_pnl(px)
        profit = px - entry
        # durable_partial_exit
        if not pos.tp1_hit and profit >= abs(entry - pos.sl) + entry * 0.0003:
            half = pos.qty_original * 0.5
            fills.append((px, half)); events.append(f"PARTIAL50@{profit/risk:+.2f}R")
            pos.qty -= half; pos.tp1_hit = True
            pos.sl = pos.trailing_sl = exchange_sl = entry
        # trailing
        pos.update_pnl(px)
        new_sl = pos.calc_trailing_sl()
        if new_sl is not None and new_sl > pos.trailing_sl:
            if new_sl >= px:
                events.append(f"TRAIL_REJECTED({(new_sl-entry)/risk:+.2f}R>price)") if not any(
                    e.startswith("TRAIL_REJECTED") for e in events) else None
            else:
                pos.sl = pos.trailing_sl = exchange_sl = new_sl
                events.append(f"TRAIL->{(new_sl-entry)/risk:+.2f}R") if not any(
                    e.startswith("TRAIL->") for e in events[-1:]) else None
        # confirmed_rr_exit
        dist = abs(entry - pos.sl)
        if dist > 0 and profit >= 2 * dist:
            fills.append((px, pos.qty)); events.append(f"RR_EXIT@{profit/risk:+.2f}R (dist={dist/risk:.2f}R)")
            pos.qty = 0; break
    if pos.qty > 0:
        fills.append((prices[-1], pos.qty)); events.append("OPEN_AT_END")
    gross = sum((p - entry) * q for p, q in fills) / risk
    costs = (entry * FEE + entry * SLIP) * 1.0 + sum((p * FEE + p * SLIP) * q for p, q in fills)
    return gross, gross - costs / risk, events


PATHS = {
    "straight to stop":        [0, -1.2],
    "+0.5R then stop":         [0, 0.5, -1.2],
    "+1R then back to entry":  [0, 1.05, -0.2],
    "+1.4R then reverse":      [0, 1.4, -0.2],
    "+1.8R then reverse":      [0, 1.8, -0.2],
    "straight to target":      [0, 3.2],
}


def main():
    print(f"cfg TRAILING_TRIGGER={cfg.TRAILING_TRIGGER} TRAILING_LOCK={cfg.TRAILING_LOCK}; "
          f"costs per fill {FEE+SLIP:.4f} (fee+slip), stop distance = 1% of price\n")
    for R in (2.0, 3.0):
        print(f"== gross target R={R} ==")
        for name, path in PATHS.items():
            g, n, ev = simulate(R, path)
            print(f"  {name:24s} gross={g:+.2f}R net={n:+.2f}R  " + " ".join(e for e in ev if e))
        print()


if __name__ == "__main__":
    main()
