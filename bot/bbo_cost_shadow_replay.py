"""Replay of the BBO cost shadow over the audited production population (RESEARCH ONLY).

Usage: python -m bot.bbo_cost_shadow_replay POPULATION.json OUT.md

Never imported by the runtime. Strictly separates:
* OBSERVED  - real bookTicker quotes recorded at evaluation time (none exist for
              the audited population: production never subscribed bookTicker);
* COUNTERFACTUAL - spread = k x tick around the logged entry reference, top
              quantity assumed sufficient. Hypothesis, never evidence.

"NEXUS would change its EV/R:R gate" is reported separately from "the order
would be executable" (MIN_ORDER) and "would reach final sizing".
decision_effect=NONE execution_effect=NONE
"""
from __future__ import annotations

from decimal import Decimal
import json
import statistics
import sys

from bot import bbo_cost_shadow_v1 as model
from bot.nexus_probability import heuristic_win_probability
from bot.sizing_decomposition import decompose

SCENARIOS = (("COUNTERFACTUAL_3_TICKS", 3.0), ("COUNTERFACTUAL_1_TICK", 1.0))


def _pct(values, q):
    if not values:
        return None
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))
    return vals[idx]


def _min_order(inst: dict, *, equity, risk, entry, stop, taker, slippage_allowance, leverage, mmp):
    info = {"quantityUnit": "BASE_ASSET", "qtyStep": inst["step"], "minQty": inst["min_qty"],
            "minNotional": inst["min_notional"], "multiplier": 1.0, "tickSize": inst["tick"]}
    return decompose(info=info, equity=equity, available=equity, entry=Decimal(str(entry)),
                     stop=Decimal(str(stop)), risk_pct=risk, leverage=leverage, max_margin_pct=mmp,
                     fee_rate_per_side=taker, slippage_pct=slippage_allowance)


def evaluate(pop: dict, floor: float) -> dict:
    floor_slip = model.sizing_slippage_floor()
    out = {"total": 0, "observed": {"valid": 0, "invalid": 0, "stale": 0, "missing": 0},
           "parity_ok": 0, "static_cost": [], "scenarios": {}}
    for name, _k in SCENARIOS:
        out["scenarios"][name] = {"spread_bps": [], "live_cost": [], "delta": [],
                                  "gate_changes": 0, "gate_changes_unique": set(),
                                  "min_order_static_ok": 0, "min_order_cf_ok": 0,
                                  "final_sizing_cf": 0}
    for ev in pop["evaluations"]:
        out["total"] += 1
        out["observed"]["missing"] += 1        # no recorded bookTicker for this population
        inst = pop["instruments"][ev["symbol"]]
        p = heuristic_win_probability(ev["confidence"])
        static_rt = (2 * ev["taker_fee"] + ev["entry_slippage"] + ev["exit_slippage"]) * 1e4
        g_s = model.nexus_ev_rr_gate(entry=ev["entry"], stop=ev["sl"], target=ev["tp"], win_prob=p,
                                     round_trip_cost_bps=static_rt, rr_floor=floor)
        out["parity_ok"] += abs(g_s.rr_net - ev["logged_rr_net"]) < 0.01
        out["static_cost"].append(static_rt)
        for name, k in SCENARIOS:
            sc = out["scenarios"][name]
            tick = float(inst["tick"])
            book = model.book_metrics(ev["entry"] - k * tick / 2, ev["entry"] + k * tick / 2)
            costs = model.cost_breakdown(symbol=ev["symbol"], taker_fee=ev["taker_fee"],
                                         entry_slippage=ev["entry_slippage"],
                                         exit_slippage=ev["exit_slippage"], book=book)
            live = costs.live_plus_static_impact_cost_bps
            g_l = model.nexus_ev_rr_gate(entry=ev["entry"], stop=ev["sl"], target=ev["tp"], win_prob=p,
                                         round_trip_cost_bps=live, rr_floor=floor)
            sc["spread_bps"].append(book.spread_bps)
            sc["live_cost"].append(live)
            sc["delta"].append(live - static_rt)
            changed = g_l.allowed != g_s.allowed
            sc["gate_changes"] += changed
            if changed:
                sc["gate_changes_unique"].add(ev["candidate_id"])
            common = dict(equity=pop["equity"], risk=pop["risk_pct_effective"], entry=ev["entry"],
                          stop=ev["sl"], taker=ev["taker_fee"], leverage=pop["leverage"],
                          mmp=pop["max_margin_pct"])
            mo_static = _min_order(inst, slippage_allowance=max(floor_slip, ev["entry_slippage"] + ev["exit_slippage"]), **common)
            live_slip_allow = (live / 1e4) - 2 * ev["taker_fee"]
            mo_cf = _min_order(inst, slippage_allowance=max(floor_slip, live_slip_allow), **common)
            sc["min_order_static_ok"] += mo_static["result"] == "PASS"
            sc["min_order_cf_ok"] += mo_cf["result"] == "PASS"
            if g_l.allowed and mo_cf["result"] == "PASS":
                sc["final_sizing_cf"] += 1
    return out


def render(pop: dict, res: dict, floor: float) -> str:
    uniq = sorted({e["candidate_id"] for e in pop["evaluations"]})
    L = ["# BBO cost shadow v1: replay over the audited production population", "",
         "RESEARCH ONLY. decision_effect=NONE execution_effect=NONE. Evidence class: **UNPROVEN**.", "",
         f"- Source: {pop['source']}",
         f"- Population: {res['total']} NEXUS evaluations, {len(uniq)} unique candidates ({', '.join(uniq)})",
         f"- Static net R:R reproduces the logged rr_net in {res['parity_ok']}/{res['total']} evaluations",
         f"- NEXUS net R:R floor {floor:.2f}; win_prob = runtime heuristic of the logged fusion confidence", "",
         "## OBSERVED (real bookTicker at evaluation time)", "",
         "| total | BBO valid | BBO invalid | BBO stale | BBO missing | spread median | p90 | p95 |",
         "|---|---|---|---|---|---|---|---|",
         f"| {res['total']} | {res['observed']['valid']} | {res['observed']['invalid']} | "
         f"{res['observed']['stale']} | {res['observed']['missing']} | NA | NA | NA |", "",
         f"{pop['recorded_bbo_note']}. Observed spread statistics will exist only after the shadow "
         "runs prospectively in production.", "",
         "## COUNTERFACTUAL (hypothesis: spread = k x tick, top-of-book qty sufficient)", "",
         "| scenario | spread median bps | p90 | p95 | static cost median bps | live cost median bps | "
         "delta median bps | EV_RR gate changes (evals / unique) | MIN_ORDER ok (static sizing) | "
         "MIN_ORDER ok (cf sizing) | would reach final sizing (cf) |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, _k in SCENARIOS:
        sc = res["scenarios"][name]
        L.append(
            f"| {name} | {statistics.median(sc['spread_bps']):.3f} | {_pct(sc['spread_bps'], .9):.3f} | "
            f"{_pct(sc['spread_bps'], .95):.3f} | {statistics.median(res['static_cost']):.1f} | "
            f"{statistics.median(sc['live_cost']):.2f} | {statistics.median(sc['delta']):.2f} | "
            f"{sc['gate_changes']} / {len(sc['gate_changes_unique'])} | "
            f"{sc['min_order_static_ok']}/{res['total']} | {sc['min_order_cf_ok']}/{res['total']} | "
            f"{sc['final_sizing_cf']}/{res['total']} |")
    L += ["", "\"EV_RR gate changes\" is a counterfactual of one NEXUS gate only; later NEXUS stages "
          "are not re-run. It is NOT \"order would be executable\": executability needs MIN_ORDER "
          "and final sizing, reported in their own columns (MIN_ORDER sizing keeps the runtime "
          "NEXUS_EXPECTED_SLIPPAGE_PCT floor).", ""]
    return "\n".join(L) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    from bot.bbo_cost_shadow_runtime import rr_floor
    pop = json.load(open(argv[1], encoding="utf-8"))
    floor = rr_floor()
    text = render(pop, evaluate(pop, floor), floor)
    with open(argv[2], "w", encoding="utf-8") as fh:
        fh.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
