"""Closed-form analysis of the NEXUS entry gates (research only, no I/O).

Every formula below is copied from production code (file:line in comments) so
the tables show what the gates can and cannot discriminate. No market data is
used; ATR examples are labelled assumptions.

    python -S research/strategy_audit/gate_algebra.py
"""

# bot/nexus_ai.py:305-343  expected_value(): cost = 2*fee + 2*slippage
FEE, SLIP = 0.0006, 0.0005            # nexus_live_cost_calibration fallback (6 / 5 bps)
COST = 2 * FEE + 2 * SLIP             # 0.0022 round trip
# bot/strategy.py:126  TOTAL_COST = (TAKER+SLIPPAGE)*2 + FUNDING
STRAT_COST = (0.0006 + 0.0002) * 2 + 0.0001
# bot/nexus_ai.py:650  NEXUS_MIN_RR_NET default = MIN_RR_RATIO*0.8
MIN_RR_NET = 2.0 * 0.80
# bot/strategy.py:665-669 entry-type geometry
GEOMETRY = {"BOS_BREAK": (1.2, 3.6), "MOMENTUM": (1.5, 3.0), "PULLBACK": (2.0, 4.0)}


def p_win(conf):                       # bot/nexus_probability.py:17
    return min(0.75, 0.30 + conf / 100 * 0.45)


def rr_net(R, s, c=COST):              # bot/nexus_ai.py:327-333
    return (R * s - c) / (s + c)


def min_stop_for_rr_net(R, c=COST, k=MIN_RR_NET):
    # (R s - c)/(s + c) >= k  <=>  s >= c (1 + k) / (R - k)
    return c * (1 + k) / (R - k)


def min_conf_for_ev(R, s, c=COST):
    # p (R s - c) > (1-p)(s + c)  <=>  p > (s + c) / ((R + 1) s)
    p_star = (s + c) / ((R + 1) * s)
    if p_star >= 0.75:
        return None                    # unreachable: heuristic p is capped at 0.75
    return max(0.0, (p_star - 0.30) / 0.45 * 100)


def main():
    print("== Strategy R:R gate (bot/strategy.py:665-685) ==")
    for name, (sl_m, tp_m) in GEOMETRY.items():
        print(f"  {name:10s} sl_mult={sl_m} tp_mult={tp_m}  gross R:R={tp_m/sl_m:.2f} "
              f"-> MIN_RR_RATIO=2.0 {'always passes' if tp_m/sl_m >= 2 else 'always fails'}")

    print(f"\n== NEXUS net R:R gate rr_net >= {MIN_RR_NET} with round-trip cost {COST:.4f} ==")
    for name, (sl_m, tp_m) in GEOMETRY.items():
        R = tp_m / sl_m
        s_min = min_stop_for_rr_net(R)
        print(f"  {name:10s} R={R:.1f}: stop distance must be >= {s_min*100:.3f}% of price "
              f"=> ATR_eff >= {s_min/sl_m*100:.3f}% (ATR_eff = max(ATR15, 0.5*ATR1h))")

    print("\n== Strategy fee gate (strategy.py:688-693): move_to_tp >= 2 * TOTAL_COST ==")
    for name, (sl_m, tp_m) in GEOMETRY.items():
        R = tp_m / sl_m
        print(f"  {name:10s} requires stop >= {2*STRAT_COST*100/R:.3f}%  "
              f"(dominated by NEXUS rr_net minimum {min_stop_for_rr_net(R)*100:.3f}%)")

    print("\n== NEXUS EV gate: minimum ensemble confidence for EV>0 (p capped 0.75) ==")
    print("  stop%   " + "  ".join(f"R={R:.0f}" for R in (2, 3)))
    for s in (0.002, 0.003, 0.005, 0.0075, 0.01, 0.0143, 0.02, 0.03):
        row = []
        for R in (2, 3):
            m = min_conf_for_ev(R, s)
            row.append(" never " if m is None else f"{m:6.1f}")
        passes = ["rr_net PASS" if rr_net(R, s) >= MIN_RR_NET else "rr_net FAIL" for R in (2, 3)]
        print(f"  {s*100:5.2f}%  {row[0]}  {row[1]}   [{passes[0]} | {passes[1]}]")

    print("\n== Where both gates pass, is the EV gate ever binding? ==")
    for R in (2, 3):
        s = min_stop_for_rr_net(R)
        print(f"  R={R}: at the smallest admissible stop ({s*100:.3f}%) EV needs conf > "
              f"{min_conf_for_ev(R, s):.1f} (and less for wider stops)")

    print("\n== Illustrative ATR assumptions (NOT measured; replace with data) ==")
    for label, atr in (("BTC-like quiet 15m", 0.0015), ("BTC-like active 15m", 0.003),
                       ("alt active 15m", 0.006), ("alt volatile 15m", 0.01)):
        out = []
        for name, (sl_m, tp_m) in GEOMETRY.items():
            out.append(f"{name}={'PASS' if rr_net(tp_m/sl_m, sl_m*atr) >= MIN_RR_NET else 'block'}")
        print(f"  ATR_eff={atr*100:.2f}% ({label}): " + " ".join(out))


if __name__ == "__main__":
    main()
