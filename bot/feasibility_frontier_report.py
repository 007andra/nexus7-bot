"""Render the NEXUS Feasibility Frontier v1 report (RESEARCH ONLY).

Usage: python -m bot.feasibility_frontier_report DATASET.json OUT.md

Reads a dataset of exchange filters + observed geometry (with provenance) and
evaluates every symbol x R x stop x equity cell with ``bot.feasibility_frontier``.
No network, exchange, database or runtime state is touched.
"""
from __future__ import annotations

import json
import sys

from bot.feasibility_frontier import (
    CostModel, Instrument, evaluate, min_equity_for, min_stop_pct_for_ev,
    min_stop_pct_for_rr, nexus_min_rr_net,
)

R_GRID = (1.5, 2.0, 2.5, 3.0, 3.5, 4.0)
STOP_GRID = (0.0025, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.02, 0.025, 0.03)
# Win probability = heuristic_win_probability(confidence). 0.45 <-> confidence 33.3;
# 0.356 <-> confidence 12.4 (observed on the SOL candidates vetoed in production).
P_BASE = 0.45
P_OBSERVED_LOW = 0.30 + 0.45 * 12.4 / 100


def _instruments(sym: str, row: dict) -> list[tuple[str, Instrument]]:
    if row["provenance"] != "ASSUMED":
        return [("", Instrument(sym, row["step"], row["min_qty"], row["min_notional"], row["provenance"]))]
    return [("N5", Instrument(sym, "0.001", "0.001", "5", "ASSUMED")),
            ("N20", Instrument(sym, "0.001", "0.001", "20", "ASSUMED"))]


def _pct(x, digits=2):
    return "NA" if x is None else f"{x * 100:.{digits}f}%"


def build(dataset: dict) -> dict:
    eq0 = float(dataset["equity_current"])
    risk = float(dataset["risk_pct_effective"])
    equities = (eq0, 10.0, 15.0, 20.0, 25.0, 50.0)
    floor = nexus_min_rr_net()
    out = {"floor": floor, "equities": equities, "symbols": {}}
    for sym, row in sorted(dataset["symbols"].items()):
        cost = CostModel.static_for(sym)
        price, s_obs, r_obs = float(row["price"]), row["stop_med"] / 100.0, round(row["rr_med"], 2)
        variants = {}
        for tag, ins in _instruments(sym, row):
            obs = evaluate(ins, price=price, stop_pct=s_obs, gross_rr=r_obs, equity=eq0,
                           risk_pct=risk, cost=cost, win_prob=P_BASE)
            grid = {}
            for eq in equities:
                for r in R_GRID:
                    for s in STOP_GRID:
                        cell = evaluate(ins, price=price, stop_pct=s, gross_rr=r, equity=eq,
                                        risk_pct=risk, cost=cost, win_prob=P_BASE)
                        grid[(eq, r, s)] = cell
            min_eq_obs = min_equity_for(ins, price=price, stop_pct=s_obs, risk_pct=risk, cost=cost)
            variants[tag] = {"ins": ins, "obs": obs, "grid": grid, "min_eq_obs": min_eq_obs}
        r_min_obs = floor + cost.round_trip * (1 + floor) / s_obs
        out["symbols"][sym] = {"row": row, "cost": cost, "price": price, "s_obs": s_obs,
                               "r_obs": r_obs, "variants": variants, "r_min_obs": r_min_obs}
    return out


def render(dataset: dict, res: dict) -> str:
    eq0 = float(dataset["equity_current"])
    floor = res["floor"]
    L = []
    w = L.append
    w("# NEXUS Feasibility Frontier v1")
    w("")
    w("RESEARCH / SHADOW ONLY. decision_effect=NONE execution_effect=NONE. No threshold, risk, "
      "recovery, leverage, score, EV, R:R or sizing value is changed; equity counterfactuals are "
      "analytical and authorize nothing.")
    w("")
    w(f"- Source: {dataset['generated_from']}")
    w(f"- Equity {eq0} USDT, effective stop-risk {dataset['risk_pct_effective']*100:.2f}%, "
      f"leverage {dataset['leverage']}x, margin cap {dataset['max_margin_pct']*100:.0f}% of available")
    w(f"- NEXUS net R:R floor {floor:.2f}; EV evaluated at win_prob={P_BASE} (confidence 33.3); "
      f"observed low-confidence case p={P_OBSERVED_LOW:.3f} (confidence 12.4)")
    w("- Costs = live runtime static model: taker 5 bps/side; slippage 5 bps/side majors "
      "(BTC/ETH/SOL), 10 bps/side alts; MIN_ORDER sizing slippage floor NEXUS_EXPECTED_SLIPPAGE_PCT=0.10%")
    w("- Filters provenance: OBSERVED = full filters in [SIZING_DECOMPOSITION]; INFERRED = "
      "[MIN_ORDER_FEASIBILITY] min_valid_qty+binding+price; ASSUMED = no evidence (N5/N20 scenarios)")
    w("")
    w("## 1. Per-symbol model at current equity, observed median stop and observed median gross R:R")
    w("")
    w("| symbol | prov | price | minQty | step | minNotional | min valid qty | min notional | fee/side | slip/side | RT cost | stop obs | R obs | R net | EV % | risk@min | budget | margin@min 50x | max stop MIN_ORDER | min stop NEXUS R:R | MIN_ORDER | NEXUS R:R | NEXUS EV | intersection |")
    w("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for sym, d in res["symbols"].items():
        for tag, v in d["variants"].items():
            o, ins, c = v["obs"], v["ins"], d["cost"]
            label = f"{sym}{'('+tag+')' if tag else ''}"
            w(f"| {label} | {ins.provenance} | {d['price']:g} | {ins.min_qty} | {ins.step} | {ins.min_notional} | "
              f"{o.min_valid_qty:g} | {o.min_valid_notional:.2f} | {c.taker_fee*1e4:.0f}bp | {c.slippage_per_side*1e4:.0f}bp | "
              f"{_pct(c.round_trip)} | {_pct(d['s_obs'],3)} | {d['r_obs']:.2f} | {o.rr_net:.2f} | {o.ev_pct:+.3f} | "
              f"{o.risk_at_min_qty:.5f} | {o.risk_budget:.5f} | {o.margin_at_min_qty:.4f} | "
              f"{_pct(o.max_stop_pct_min_order,3)} | {_pct(o.min_stop_pct_nexus_rr,3)} | "
              f"{str(o.min_order_ok).lower()} | {str(o.nexus_rr_ok).lower()} | {str(o.nexus_ev_ok).lower()} | "
              f"**{str(o.all_ok).lower()}** |")
    w("")
    w("## 2. Frontier: passing (R, stop) cells per equity (all five gates)")
    w("")
    w("Cell = gross R:R x stop. Count of passing cells out of 54 and the passing set at current equity.")
    w("")
    head = "| symbol | " + " | ".join(f"E={e:g}" for e in res["equities"]) + " | passing cells at current equity |"
    w(head)
    w("|---|" + "---|" * (len(res["equities"]) + 1))
    for sym, d in res["symbols"].items():
        for tag, v in d["variants"].items():
            counts, cur = [], []
            for e in res["equities"]:
                ok = [(r, s) for (eq, r, s), c in v["grid"].items() if eq == e and c.all_ok]
                counts.append(str(len(ok)))
                if e == res["equities"][0]:
                    cur = ok
            cur_txt = ", ".join(f"R{r:g}/{s*100:g}%" for r, s in sorted(cur)) or "none"
            w(f"| {sym}{'('+tag+')' if tag else ''} | " + " | ".join(counts) + f" | {cur_txt} |")
    w("")
    w("## 3. Gate-only frontier (independent of symbol): min stop required by NEXUS net R:R")
    w("")
    w("| R gross | majors (RT 0.20%) | alts (RT 0.30%) | EV min stop p=0.45 majors | EV min stop p=0.45 alts | EV min stop p=0.356 alts |")
    w("|---|---|---|---|---|---|")
    for r in R_GRID:
        w(f"| {r} | {_pct(min_stop_pct_for_rr(r, 0.002, floor))} | {_pct(min_stop_pct_for_rr(r, 0.003, floor))} | "
          f"{_pct(min_stop_pct_for_ev(r, 0.002, P_BASE))} | {_pct(min_stop_pct_for_ev(r, 0.003, P_BASE))} | "
          f"{_pct(min_stop_pct_for_ev(r, 0.003, P_OBSERVED_LOW))} |")
    w("")
    w("## 4. Minimum gross R:R and minimum equity per symbol (current policies)")
    w("")
    w("| symbol | stop obs (median) | share of signals with R>=3 | min R for NEXUS R:R at stop obs | min equity for MIN_ORDER at stop obs | NEXUS passes at R=2 / R=3 (stop obs) |")
    w("|---|---|---|---|---|---|")
    for sym, d in res["symbols"].items():
        for tag, v in d["variants"].items():
            g = v["grid"]
            r2 = evaluate(v["ins"], price=d["price"], stop_pct=d["s_obs"], gross_rr=2.0, equity=1e6,
                          risk_pct=float(dataset["risk_pct_effective"]), cost=d["cost"], win_prob=P_BASE)
            r3 = evaluate(v["ins"], price=d["price"], stop_pct=d["s_obs"], gross_rr=3.0, equity=1e6,
                          risk_pct=float(dataset["risk_pct_effective"]), cost=d["cost"], win_prob=P_BASE)
            del g
            share = d["row"].get("rr3_share")
            w(f"| {sym}{'('+tag+')' if tag else ''} | {_pct(d['s_obs'],3)} | "
              f"{'NA' if share is None else f'{share*100:.0f}%'} | {d['r_min_obs']:.2f} | {v['min_eq_obs']:.2f} USDT | "
              f"{'yes' if r2.nexus_rr_ok and r2.nexus_ev_ok else 'no'} / {'yes' if r3.nexus_rr_ok and r3.nexus_ev_ok else 'no'} |")
    w("")
    w("## 5. Empirical replay: real observed signals through all five gates")
    w("")
    w("Each unique production setup ([STRATEGY_STOP_GEOMETRY]) with its own stop and gross R:R, "
      "symbol filters as above (ASSUMED symbols use the optimistic N5 scenario), win_prob as stated.")
    w("")
    sigs = dataset.get("signals", [])
    risk = float(dataset["risk_pct_effective"])
    cols = res["equities"]
    w("| win_prob | " + " | ".join(f"E={e:g}" for e in cols) + " |")
    w("|---|" + "---|" * len(cols))
    for p in (P_BASE, P_OBSERVED_LOW):
        cells = []
        for e in cols:
            ok = 0
            for sym, stop, rr in sigs:
                d = res["symbols"][sym]
                v = d["variants"].get("") or d["variants"]["N5"]
                c = evaluate(v["ins"], price=d["price"], stop_pct=float(stop), gross_rr=float(rr),
                             equity=e, risk_pct=risk, cost=d["cost"], win_prob=p)
                ok += c.all_ok
            cells.append(f"{ok}/{len(sigs)} ({ok/len(sigs)*100:.1f}%)")
        w(f"| {p:.3f} | " + " | ".join(cells) + " |")
    w("")
    w("Gate attribution at current equity (win_prob 0.45): first failing gate per signal.")
    w("")
    order = ("min_order_ok", "nexus_rr_ok", "nexus_ev_ok", "final_sizing_ok", "margin_ok")
    tally = {k: 0 for k in order}
    tally["pass"] = 0
    for sym, stop, rr in sigs:
        d = res["symbols"][sym]
        v = d["variants"].get("") or d["variants"]["N5"]
        c = evaluate(v["ins"], price=d["price"], stop_pct=float(stop), gross_rr=float(rr),
                     equity=cols[0], risk_pct=risk, cost=d["cost"], win_prob=P_BASE)
        failed = next((k for k in order if not getattr(c, k)), "pass")
        tally[failed] += 1
    w("| first failing gate | signals |")
    w("|---|---|")
    for k, n in tally.items():
        w(f"| {k} | {n} |")
    w("")
    w("## 6. Cost sensitivity: STATIC_COST vs LIVE_BBO_COST (analytical, shadow only)")
    w("")
    w("Live per-side slippage = half-spread + runtime impact floor (1 bp majors / 2 bp alts); "
      "spread = k x inferred tick (k=1 is a hard lower bound, k=3 conservative). Top-of-book depth "
      "assumed >= order notional (5-20 USDT orders). MIN_ORDER keeps the runtime sizing floor "
      "NEXUS_EXPECTED_SLIPPAGE_PCT. Signals: same replay as section 5, win_prob 0.45.")
    w("")
    w("| cost scenario | " + " | ".join(f"E={e:g}" for e in cols) + " | NEXUS-only pass |")
    w("|---|" + "---|" * (len(cols) + 1))
    for label, k in (("STATIC (runtime today)", None), ("LIVE_BBO 3 ticks", 3.0), ("LIVE_BBO 1 tick", 1.0)):
        cells, nexus_only = [], 0
        for i, e in enumerate(cols):
            ok = 0
            for sym, stop, rr in sigs:
                d = res["symbols"][sym]
                v = d["variants"].get("") or d["variants"]["N5"]
                cost = d["cost"]
                if k is not None:
                    spread = float(d["row"]["one_tick_spread_bps"]) * k / 1e4
                    floor_ = 0.0001 if any(m in sym for m in ("BTC", "ETH", "SOL")) else 0.0002
                    cost = CostModel(cost.taker_fee, spread / 2.0 + floor_)
                c = evaluate(v["ins"], price=d["price"], stop_pct=float(stop), gross_rr=float(rr),
                             equity=e, risk_pct=risk, cost=cost, win_prob=P_BASE)
                ok += c.all_ok
                if i == 0:
                    nexus_only += c.nexus_rr_ok and c.nexus_ev_ok
            cells.append(f"{ok}")
        w(f"| {label} | " + " | ".join(cells) + f" | {nexus_only}/{len(sigs)} |")
    w("")
    return "\n".join(L) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    dataset = json.load(open(argv[1], encoding="utf-8"))
    report = render(dataset, build(dataset))
    with open(argv[2], "w", encoding="utf-8") as fh:
        fh.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
