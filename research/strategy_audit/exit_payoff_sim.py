"""Deterministic LIVE exit-path simulator (research only, no I/O).

Uses the REAL ``bot.engine.Position`` with the REAL ``trailing_safety_hardening``
overlay. Exit rules are replicated from the LIVE-pilot runtime, in engine loop
order (engine.py run loop: partial -> trailing -> 2R):

* durable_partial_exit.check: when profit >= 1R + entry*0.0003 and
  not tp1_hit -> close 50 % of qty_original, pos.qty = remainder, SL -> entry.
* engine._apply_trailing_stops + trailing_safety_hardening.calc_trailing_sl;
  native_stop_repair.set_stops rejects a LONG stop >= reference price
  (invalid_trigger_side) -> state unchanged.
* confirmed_rr_exit.check: profit >= 2R -> close the remainder.
  1R is |entry - pos.sl| (current stop) in mode="before" (pre-Q-01) and the
  immutable initial risk in mode="after" (Q-01 fix).
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


def _legacy_trailing(pos, giveback):
    """Pre-Q-01 overlay formula: excursion = peak_pnl / qty (qty-dependent)."""
    if pos.pnl <= 0 or pos.qty <= 0:
        return None
    target = abs(pos.tp - pos.entry)
    if target <= 0 or pos.pnl < target * cfg.TRAILING_TRIGGER * pos.qty:
        return None
    return max(pos.entry + pos.peak_pnl / pos.qty * (1.0 - giveback), pos.sl)


def simulate(R, path_r, entry=100.0, risk=1.0, step=0.01, mode="after", monotonic_be=None,
             gap_segment=None, trail_next_cycle=False, restart_at=None,
             restart_restore=True):
    """LONG trade, 1R = ``risk`` price units; path_r = list of R waypoints.

    mode="before": pre-Q-01 rules (trailing from peak_pnl/qty, partial and 2R
    measured on the CURRENT stop distance). mode="after": production code
    (Position.calc_trailing_sl overlay + exit_geometry initial risk).
    """
    from bot.exit_geometry import initial_risk_per_unit
    if monotonic_be is None:          # pre-Q-01 code also had the unconditional BE
        monotonic_be = mode == "after"
    sig = Signal("SIM", "LONG", entry, entry - risk, entry + R * risk, 0.8)
    pos = Pos(sig, 1.0)
    pos._forensic_lineage = {"order_id": "sim-open-1"}
    restarted = False
    exchange_sl = entry - risk
    fills = []                       # (price, qty)
    events = []
    stats = {"invalid_trigger": 0, "trailing_moves": 0, "rr_exit": False, "partial": False,
             "stop_replacements": 0, "be_loosened_r": 0.0, "be_skipped": 0}
    prices = []
    giveback = max(0.0, min(1.0, float(cfg.TRAILING_LOCK)))
    gap_prices = set()
    for idx, (a, b) in enumerate(zip(path_r, path_r[1:])):
        # gap_segment: that segment is a single jump (gap / fast reversal);
        # a stop crossed by a gap fills at the gap price, not at its trigger.
        n = 1 if idx == gap_segment else max(1, int(abs(b - a) / step))
        seg = [entry + risk * (a + (b - a) * i / n) for i in range(1, n + 1)]
        if idx == gap_segment:
            gap_prices.update(seg)
        prices += seg
    for px in prices:
        remaining = pos.qty
        if remaining <= 0:
            break
        # Exchange-native protection first (it lives on the exchange).
        if px <= exchange_sl:
            fills.append((px if px in gap_prices else exchange_sl, remaining)); events.append(f"STOP@{(exchange_sl-entry)/risk:+.2f}R")
            pos.qty = 0; break
        if px >= pos.tp:
            fills.append((pos.tp, remaining)); events.append(f"TP@{(pos.tp-entry)/risk:+.2f}R")
            pos.qty = 0; break
        pos.update_pnl(px)
        profit = px - entry
        # Q-01B restart parity: crash + restart at the chosen moment. The new
        # Position is rebuilt like the startup loader (estimated sl/tp, no
        # initial risk, exchange stop as current stop) and the durable record
        # is applied exactly as on a real restart.
        if restart_at and not restarted and (
                (restart_at == "before_partial" and not pos.tp1_hit and profit >= 0.9 * risk)
                or (restart_at == "after_partial" and pos.tp1_hit)):
            import json as _json
            from bot import exit_geometry_durability as durability
            record = _json.loads(_json.dumps(durability.build_record(pos)))
            est = entry * 0.007
            fresh = Pos(Signal("SIM", "LONG", entry, entry - est * 1.5, entry + est * 3.0, 0.75), pos.qty)
            fresh._forensic_lineage = dict(pos._forensic_lineage)
            fresh.initial_sl = None
            fresh.sl = fresh.trailing_sl = exchange_sl
            fresh.update_pnl(px)
            assert durability.validate(record, symbol="SIM", direction="LONG",
                                       opening_order_id="sim-open-1", entry=entry) is None
            if restart_restore:          # False = pre-Q-01B (history lost)
                durability.apply_record(fresh, record)
            pos, restarted = fresh, True
            events.append(f"RESTART@{profit/risk:+.2f}R")
        one_r = abs(entry - pos.sl) if mode == "before" else (initial_risk_per_unit(pos) or 0.0)
        # durable_partial_exit
        if not pos.tp1_hit and one_r > 0 and profit >= one_r + entry * 0.0003:
            half = pos.qty_original * 0.5
            fills.append((px, half)); events.append(f"PARTIAL50@{profit/risk:+.2f}R")
            pos.qty -= half; pos.tp1_hit = True
            stats["partial"] = True
            # Q-01C: monotonic_be=True keeps a better (trailed) stop; False is
            # the pre-Q-01C unconditional set_sl(entry).
            if monotonic_be and exchange_sl >= entry:
                stats["be_skipped"] += 1
            else:
                if exchange_sl > entry:
                    stats["be_loosened_r"] = (exchange_sl - entry) / risk
                if exchange_sl != entry:
                    stats["stop_replacements"] += 1
                pos.sl = pos.trailing_sl = exchange_sl = entry
        # trailing (trail_next_cycle: not in the partial's own cycle, i.e. the
        # BE stop is what protects until the next loop iteration)
        if trail_next_cycle and stats["partial"] and pos.tp1_hit and not stats.get("_trailed_after"):
            stats["_trailed_after"] = True
            continue
        pos.update_pnl(px)
        new_sl = _legacy_trailing(pos, giveback) if mode == "before" else pos.calc_trailing_sl()
        if new_sl is not None and new_sl > pos.trailing_sl:
            if new_sl >= px:   # native_stop_repair: invalid_trigger_side
                stats["invalid_trigger"] += 1
                events.append(f"TRAIL_REJECTED({(new_sl-entry)/risk:+.2f}R>price)") if not any(
                    e.startswith("TRAIL_REJECTED") for e in events) else None
            else:
                pos.sl = pos.trailing_sl = exchange_sl = new_sl
                stats["trailing_moves"] += 1
                stats["stop_replacements"] += 1
                events.append(f"TRAIL->{(new_sl-entry)/risk:+.2f}R") if not any(
                    e.startswith("TRAIL->") for e in events[-1:]) else None
        # confirmed_rr_exit
        dist = abs(entry - pos.sl) if mode == "before" else (initial_risk_per_unit(pos) or 0.0)
        if dist > 0 and profit >= 2 * dist:
            fills.append((px, pos.qty)); events.append(f"RR_EXIT@{profit/risk:+.2f}R (dist={dist/risk:.2f}R)")
            stats["rr_exit"] = True
            pos.qty = 0; break
    if pos.qty > 0:
        fills.append((prices[-1], pos.qty)); events.append("OPEN_AT_END")
    gross = sum((p - entry) * q for p, q in fills) / risk
    costs = (entry * FEE + entry * SLIP) * 1.0 + sum((p * FEE + p * SLIP) * q for p, q in fills)
    stats["max_fill_r"] = max((p - entry) / risk for p, _ in fills)
    return gross, gross - costs / risk, events, stats


PATHS = {
    "straight to stop":        [0, -1.2],
    "+0.5R then stop":         [0, 0.5, -1.2],
    "+1R then back to entry":  [0, 1.05, -0.2],
    "+1.4R then reverse":      [0, 1.4, -0.2],
    "+1.8R then reverse":      [0, 1.8, -0.2],
    "straight to target":      [0, 3.2],
}


# Path A: trailing already at ~+0.75R, partial at +1.03R, then a fast reversal
# before the next trailing cycle. Path B: stop still below BE at the partial.
Q01C_CASES = (
    (2.0, "A: trail+0.75R, partial, gap +0.3R", [0, 1.031, 0.3, -1.2], True),
    (2.0, "+1R then back to entry", PATHS["+1R then back to entry"], False),
    (2.0, "+1.4R then reverse", PATHS["+1.4R then reverse"], False),
    (3.0, "B: stop<BE, partial, gap +0.3R", [0, 1.031, 0.3, -1.2], True),
    (3.0, "+1.8R then reverse", PATHS["+1.8R then reverse"], False),
)


def main():
    print(f"cfg TRAILING_TRIGGER={cfg.TRAILING_TRIGGER} TRAILING_LOCK={cfg.TRAILING_LOCK}; "
          f"costs per fill {FEE+SLIP:.4f} (fee+slip), stop distance = 1% of price\n")
    for R in (2.0, 3.0):
        print(f"== gross target R={R} ==")
        for name, path in PATHS.items():
            for mode in ("before", "after"):
                g, n, ev, st = simulate(R, path, mode=mode)
                print(f"  {name:24s} {mode:6s} gross={g:+.2f}R net={n:+.2f}R max_fill={st['max_fill_r']:+.2f}R "
                      f"trail_moves={st['trailing_moves']} invalid={st['invalid_trigger']} "
                      f"rr_exit={st['rr_exit']}  " + " ".join(e for e in ev if e))
        print()
    print("== Q-01C break-even monotonicity (exit geometry fixed in both) ==")
    for R, name, path, gap in Q01C_CASES:
        for mono in (False, True):
            g, _, ev, st = simulate(R, path, mode="after", monotonic_be=mono,
                                    gap_segment=1 if gap else None, trail_next_cycle=gap)
            print(f"  R={R} {name:34s} {'AFTER ' if mono else 'BEFORE'} gross={g:+.2f}R "
                  f"be_loosened={st['be_loosened_r']:.2f}R be_skipped={st['be_skipped']} "
                  f"stop_replacements={st['stop_replacements']}  " + " ".join(e for e in ev if e))

    print()
    print("== Q-01B restart parity (same path, crash+restart at different moments) ==")
    for R in (2.0, 3.0):
        for name, path in PATHS.items():
            row = []
            for at in (None, "before_partial", "after_partial"):
                g, _, ev, st = simulate(R, path, mode="after", restart_at=at)
                row.append(f"{(at or 'no_restart'):>14s}={g:+.2f}R")
            g_lost, _, _, _ = simulate(R, path, mode="after", restart_at="before_partial",
                                       restart_restore=False)
            row.append(f"pre-Q-01B(before_partial)={g_lost:+.2f}R")
            print(f"  R={R} {name:24s} " + "  ".join(row))

if __name__ == "__main__":
    main()
