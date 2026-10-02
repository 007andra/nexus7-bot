"""F-003 quantitative sizing audit — offline, deterministic, no exchange access.

Reconstructs the CURRENT LIVE-pilot sizing arithmetic exactly as composed in
production (see docs/F003_RISK_POLICY_AUDIT_2026-10-02.md for the call graph)
and evaluates candidate risk-based policies. Nothing here is imported by the
bot; it never places orders.

    python research/risk_policy/f003_sizing_audit.py [--section NAME] [--paths N]

Every function mirrors a named production function; tests/test_f003_sizing_
characterization.py asserts the mirror against the real composed callable.
"""
from __future__ import annotations

import argparse
import math
from decimal import Decimal, ROUND_FLOOR

# ── Production defaults (bot/config.py, bot/kucoin.py, env defaults) ──────────
LEVERAGE = 10                 # cfg.LEVERAGE
MAX_RISK_PCT = 0.01           # cfg.MAX_RISK_PCT (V3 gate only)
MAX_MARGIN_PCT = 0.10         # cfg.MAX_MARGIN_PCT (V3 collateral cap)
MARGIN_FRACTION = 0.50        # final_sizing_invariants.MARGIN_FRACTION
LOSS_LIMIT_OF_MARGIN = 0.50   # final_loss_budget.measure: limit = margin * 0.50
TAKER_FEE = 0.0006            # kucoin.TAKER_FEE
MAKER_FEE = 0.0002            # kucoin.MAKER_FEE (not used by market entries/exits)
EXEC_SLIP_MAJOR = 0.0005      # kucoin_execution_model.DEFAULT_SLIPPAGE (BTC/ETH/SOL)
EXEC_SLIP_ALT = 0.0010        # 2x base for other symbols
V3_SLIPPAGE = 0.001           # NEXUS_EXPECTED_SLIPPAGE_PCT default (one figure, all symbols)
MAX_POSITIONS = 2             # cfg.MAX_POSITIONS
DAILY_STOP_LOSS_PCT = 0.03    # cfg.DAILY_STOP_LOSS_PCT
MAX_DRAWDOWN = 0.10           # cfg.MAX_DRAWDOWN (V3.can_open)
DEFAULT_MMR = 0.004           # liquidation.DEFAULT_MMR (fallback when API MMR absent)
LIQUIDATION_FEE = 0.0006      # liquidation.LIQUIDATION_FEE
MIN_GAP_PCT = 0.30            # liquidation.MIN_GAP_PCT (percentage points)

# Contract metadata used by the repository's own fixtures (no web lookup).
# Prices are ILLUSTRATIVE reference prices, not market data.
SYMBOLS = {
    #          multiplier  lot  minQty  ref_price  source of multiplier
    "BTCUSDT": (0.001, 1, 1, 60000.0, "risk.py:345 / tests/test_emergency_flatten.py"),
    "ETHUSDT": (0.01, 1, 1, 3000.0, "tests/test_emergency_flatten.py"),
    "SOLUSDT": (0.1, 1, 1, 150.0, "tests/test_emergency_flatten.py"),
    "AVAXUSDT": (0.1, 1, 1, 30.0, "tests/test_native_stop_repair.py"),
    "XRPUSDT": (10.0, 1, 1, 0.60, "kucoin_fill_normalization.py:6"),
    "DOGEUSDT": (100.0, 1, 1, 0.15, "engine.py:1199 / tests/test_exec01_unit_normalization.py"),
}
MAJORS = ("BTC", "ETH", "SOL")


def exec_cost_fraction(symbol):
    """kucoin_execution_model.estimated_round_trip_cost_pct / 100 (2 fees + 2 slips)."""
    slip = EXEC_SLIP_MAJOR if any(m in symbol for m in MAJORS) else EXEC_SLIP_ALT
    return 2 * TAKER_FEE + 2 * slip


def v3_cost_fraction():
    """professional_risk.stop_risk_size: 2 fees + ONE slippage allowance."""
    return 2 * TAKER_FEE + V3_SLIPPAGE


def _floor_contracts(contracts, lot):
    c = Decimal(str(contracts))
    lot = Decimal(str(lot))
    return int((c / lot).to_integral_value(rounding=ROUND_FLOOR) * lot)


# ── CURRENT policy (exact mirror) ─────────────────────────────────────────────
def current_target_contracts(symbol, price, available, leverage=LEVERAGE):
    """final_sizing_invariants._operator_target_quantity, in contracts."""
    mult, lot, minimum, _ = SYMBOLS[symbol][:4]
    notional = Decimal(str(available)) * Decimal(str(MARGIN_FRACTION)) * Decimal(str(leverage))
    contracts = _floor_contracts(notional / (Decimal(str(price)) * Decimal(str(mult))), lot)
    return contracts if contracts >= minimum else 0


def v3_gate_contracts(symbol, price, stop_frac, equity, available, leverage=LEVERAGE,
                      risk_pct=MAX_RISK_PCT):
    """RiskManagerV3.size_for_stop -> professional_risk.stop_risk_size, in contracts."""
    mult, lot, minimum, _ = SYMBOLS[symbol][:4]
    per_unit = price * (stop_frac + v3_cost_fraction())
    qty_risk = equity * risk_pct / per_unit
    qty_margin = available * MAX_MARGIN_PCT * leverage / price
    contracts = _floor_contracts(min(qty_risk, qty_margin) / mult, lot)
    return contracts if contracts >= minimum else 0


def liquidation_move(leverage, mmr=DEFAULT_MMR, long=True):
    """liquidation.liquidation_price -> % move to liquidation (isolated-style formula)."""
    side = 1 if long else -1
    liq = (1 - side / leverage) / (1 - side * mmr - side * LIQUIDATION_FEE)
    return abs(1 - liq)


def current_policy(symbol, price, stop_frac, equity, available, leverage=LEVERAGE):
    """Full CURRENT decision: returns dict with result/blocker and loss figures."""
    mult = SYMBOLS[symbol][0]
    cost = exec_cost_fraction(symbol)
    out = {"policy": "CURRENT", "symbol": symbol, "equity": equity, "available": available,
           "price": price, "stop_frac": stop_frac, "leverage": leverage, "contracts": 0,
           "qty": 0.0, "notional": 0.0, "margin": 0.0, "gross_loss": 0.0, "cost_loss": 0.0,
           "loss": 0.0, "loss_pct_equity": 0.0, "result": "REJECT", "blocker": ""}
    # liquidation geometry (kucoin_contract_risk_hardening): stop must sit >= MIN_GAP before liq
    liq = liquidation_move(leverage)
    if stop_frac * 100 > liq * 100 - MIN_GAP_PCT:
        out["blocker"] = "liquidation_gap(compress_or_block)"
        return out
    target = current_target_contracts(symbol, price, available, leverage)
    if target <= 0:
        out["blocker"] = "target_below_min_contract"
        return out
    if v3_gate_contracts(symbol, price, stop_frac, equity, available, leverage) <= 0:
        out["blocker"] = "v3_gate(min_lot>risk_pct_or_margin_cap)"
        return out
    qty = target * mult
    notional = qty * price
    margin = notional / leverage
    if notional * (1 / leverage + TAKER_FEE) > available:
        out["blocker"] = "core_affordability"
        return out
    gross = qty * price * stop_frac
    costs = qty * price * cost
    if gross + costs > margin * LOSS_LIMIT_OF_MARGIN * (1 + 1e-12):
        out["blocker"] = "final_loss_budget(stop+cost>0.5/L)"
        return out
    out.update(contracts=target, qty=qty, notional=notional, margin=margin, gross_loss=gross,
               cost_loss=costs, loss=gross + costs, loss_pct_equity=(gross + costs) / equity,
               result="ACCEPT", blocker="-")
    return out


# ── CANDIDATE risk-based policy (proposal only) ───────────────────────────────
def risk_based_policy(symbol, price, stop_frac, equity, available, risk_pct,
                      leverage=LEVERAGE, margin_cap_frac=MARGIN_FRACTION):
    """risk_budget -> qty from stop distance + costs -> floor contracts -> margin cap.

    INV-RISK-SIZING-001 / INV-RISK-ROUNDING-001: loss after quantization <= budget;
    1 contract above budget -> NO TRADE. INV-RISK-LEVERAGE-001: leverage only
    enters the margin feasibility clamp, which can only reduce size.
    """
    mult, lot, minimum, _ = SYMBOLS[symbol][:4]
    cost = exec_cost_fraction(symbol)
    budget = equity * risk_pct
    per_contract_loss = mult * price * (stop_frac + cost)
    out = {"policy": f"RISK_{risk_pct * 100:g}%", "symbol": symbol, "equity": equity,
           "available": available, "price": price, "stop_frac": stop_frac,
           "leverage": leverage, "risk_pct": risk_pct, "budget": budget,
           "raw_contracts": budget / per_contract_loss, "contracts": 0, "qty": 0.0,
           "notional": 0.0, "margin": 0.0, "loss": 0.0, "loss_pct_equity": 0.0,
           "result": "REJECT", "blocker": "", "binding": ""}
    liq = liquidation_move(leverage)
    if stop_frac * 100 > liq * 100 - MIN_GAP_PCT:
        out["blocker"] = "liquidation_gap"
        return out
    by_risk = _floor_contracts(budget / per_contract_loss, lot)
    margin_cap = available * margin_cap_frac
    by_margin = _floor_contracts(margin_cap * leverage / (price * mult), lot)
    contracts = min(by_risk, by_margin)
    out["binding"] = "RISK_BUDGET" if by_risk <= by_margin else "MARGIN"
    if contracts < minimum:
        out["blocker"] = "one_contract_exceeds_budget" if by_risk < minimum else "margin_insufficient"
        return out
    qty = contracts * mult
    loss = contracts * per_contract_loss
    assert loss <= budget * (1 + 1e-12), "INV-RISK-ROUNDING-001"
    out.update(contracts=contracts, qty=qty, notional=qty * price,
               margin=qty * price / leverage, loss=loss, loss_pct_equity=loss / equity,
               result="ACCEPT", blocker="-")
    return out


def assert_projected_loss_within_budget(*, contracts, multiplier, entry, stop, cost_fraction,
                                        equity, risk_pct):
    """PROPOSED final pre-dispatch invariant (not wired into production)."""
    values = (contracts, multiplier, entry, stop, cost_fraction, equity, risk_pct)
    if any(isinstance(v, bool) or not math.isfinite(float(v)) for v in values):
        raise ValueError("nonfinite_input")
    if contracts <= 0 or multiplier <= 0 or entry <= 0 or stop <= 0 or equity <= 0:
        raise ValueError("invalid_input")
    if not 0 < risk_pct <= 0.05:
        raise ValueError("risk_pct_out_of_bounds")
    qty = contracts * multiplier
    projected = qty * (abs(entry - stop) + entry * cost_fraction)
    budget = equity * risk_pct
    if projected > budget * (1 + 1e-9):
        raise ValueError(f"projected_loss_exceeds_budget:{projected:.6f}>{budget:.6f}")
    return projected, budget


# ── Reporting helpers ─────────────────────────────────────────────────────────
def _table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _usd(v):
    return f"{v:,.2f}"


def _pct(v, d=2):
    return f"{v * 100:.{d}f}%"


def section_numeric_example():
    """§4 — equity=available=100, 50% margin, leverage sweep, stop sweep (BTC-agnostic)."""
    lines = ["### Continuous (unquantized) current policy, equity = available = 100 USDT",
             "Cost model: 2×taker 0.06% + 2×slippage 0.05% (majors) = 0.22% of notional.", ""]
    rows = []
    cost = exec_cost_fraction("BTCUSDT")
    for lev in (1, 5, 10, 20, 50):
        margin = 100 * MARGIN_FRACTION
        notional = margin * lev
        liq = liquidation_move(lev)
        budget_stop = LOSS_LIMIT_OF_MARGIN / lev - cost
        for s in (0.005, 0.01, 0.02, 0.03, 0.04, 0.05):
            gross = notional * s
            fees = notional * 2 * TAKER_FEE
            slip = notional * 2 * EXEC_SLIP_MAJOR
            total = gross + fees + slip
            allowed = s <= budget_stop and s * 100 <= liq * 100 - MIN_GAP_PCT
            rows.append((f"{lev}x", _usd(margin), _usd(notional), _pct(s, 1), _usd(gross),
                         _usd(fees), _usd(slip), _usd(total), _pct(total / 100),
                         "yes" if allowed else ("no: loss budget" if s > budget_stop else "no: liq gap")))
    lines.append(_table(["lev", "margin", "notional", "stop", "gross@SL", "fees", "slippage",
                         "total", "%equity", "admitted?"], rows))
    lines.append("")
    rows = []
    for lev in (1, 5, 10, 20, 50):
        budget_stop = LOSS_LIMIT_OF_MARGIN / lev - cost
        liq = liquidation_move(lev)
        max_stop = min(budget_stop, liq - MIN_GAP_PCT / 100)
        worst = 100 * MARGIN_FRACTION * lev * (max_stop + cost)
        rows.append((f"{lev}x", _pct(LOSS_LIMIT_OF_MARGIN / lev), _pct(liq), _pct(max(budget_stop, 0)),
                     _pct(max(max_stop, 0)), _usd(worst), _pct(worst / 100)))
    lines.append("### Worst admissible projected loss per trade (continuous)")
    lines.append(_table(["lev", "0.5/L", "liq move", "max stop by loss budget",
                         "max admissible stop", "worst loss USDT", "worst %equity"], rows))
    return "\n".join(lines)


def section_stop_sensitivity(equity=100.0):
    rows = []
    for s in (0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.03, 0.05):
        row = [_pct(s)]
        cur = current_policy("SOLUSDT", 150.0, s, equity, equity)
        row.append(f"{cur['contracts']}c / {_usd(cur['loss'])} ({_pct(cur['loss_pct_equity'])})"
                   if cur["result"] == "ACCEPT" else f"REJECT {cur['blocker']}")
        for r in (0.0025, 0.005, 0.01, 0.02):
            res = risk_based_policy("SOLUSDT", 150.0, s, equity, equity, r)
            row.append(f"{res['contracts']}c / {_usd(res['loss'])} ({_pct(res['loss_pct_equity'])})"
                       if res["result"] == "ACCEPT" else f"REJECT {res['blocker']}")
        rows.append(row)
    return ("### SOLUSDT @150, equity=available=100, L=10 (contracts / projected loss incl. costs)\n"
            + _table(["stop", "A CURRENT", "B 0.25%", "C 0.5%", "D 1.0%", "E 2.0%"], rows))


def section_equity_feasibility():
    lines = []
    stops = (0.005, 0.01, 0.02)
    for policy, r in (("CURRENT", None), ("RISK 0.5%", 0.005), ("RISK 1%", 0.01), ("RISK 2%", 0.02)):
        rows = []
        for sym in SYMBOLS:
            price = SYMBOLS[sym][3]
            row = [sym]
            for eq in (5, 10, 25, 50, 100, 500, 1000):
                cells = []
                for s in stops:
                    res = (current_policy(sym, price, s, eq, eq) if r is None
                           else risk_based_policy(sym, price, s, eq, eq, r))
                    cells.append(str(res["contracts"]) if res["result"] == "ACCEPT" else "×")
                row.append("/".join(cells))
            rows.append(row)
        lines.append(f"### {policy} — contracts at stop 0.5% / 1% / 2% (× = rejected), L=10")
        lines.append(_table(["symbol", "5", "10", "25", "50", "100", "500", "1000"], rows))
        lines.append("")
    return "\n".join(lines)


def min_contract_loss_table():
    rows = []
    for sym, (mult, _, _, price, src) in SYMBOLS.items():
        cost = exec_cost_fraction(sym)
        one = mult * price
        cells = [sym, mult, _usd(price), _usd(one), _pct(cost)]
        for s in (0.005, 0.01, 0.02):
            loss = one * (s + cost)
            cells.append(f"{loss:.4f}")
        # minimum equity for 1 contract within 0.5% / 1% budget at 1% stop
        loss1 = one * (0.01 + cost)
        cells += [_usd(loss1 / 0.005), _usd(loss1 / 0.01)]
        rows.append(cells)
    return _table(["symbol", "multiplier", "ref price", "1-contract notional", "RT cost",
                   "1c loss @0.5%", "@1%", "@2%", "min equity 0.5% risk @1% stop",
                   "min equity 1% risk @1% stop"], rows)


def section_leverage_invariance(equity=100.0):
    rows = []
    for lev in (5, 10, 20, 50):
        cur = current_policy("SOLUSDT", 150.0, 0.005, equity, equity, lev)
        rb = risk_based_policy("SOLUSDT", 150.0, 0.005, equity, equity, 0.01, lev)
        rows.append((f"{lev}x",
                     f"{cur['contracts']}c / {_usd(cur['loss'])}" if cur["result"] == "ACCEPT"
                     else f"REJECT {cur['blocker']}",
                     _usd(cur["margin"]),
                     f"{rb['contracts']}c / {_usd(rb['loss'])}" if rb["result"] == "ACCEPT"
                     else f"REJECT {rb['blocker']}",
                     _usd(rb["margin"])))
    return ("### SOLUSDT @150, stop 0.5%, equity=available=100\n"
            + _table(["leverage", "CURRENT contracts / loss", "CURRENT margin",
                      "RISK 1% contracts / loss", "RISK 1% margin"], rows))


def section_aggregate(equity=100.0, lev=LEVERAGE):
    """Sequential entries: second position is sized on the REMAINING available collateral."""
    cost = exec_cost_fraction("BTCUSDT")
    max_stop = LOSS_LIMIT_OF_MARGIN / lev - cost
    rows = []
    avail, total = equity, 0.0
    for n in (1, 2, 3):
        margin = avail * MARGIN_FRACTION
        loss = margin * lev * (max_stop + cost)
        total += loss
        rows.append((n, _usd(avail), _usd(margin), _usd(margin * lev), _usd(loss), _usd(total),
                     _pct(total / equity), f"{total / (equity * DAILY_STOP_LOSS_PCT):.1f}×",
                     f"{total / (equity * MAX_DRAWDOWN):.1f}×"))
        avail -= margin
    return _table(["position #", "available at entry", "margin", "notional", "worst loss",
                   "cumulative", "% equity", "× daily stop (3%)", "× max DD (10%)"], rows)


def section_consecutive():
    rows = []
    for r in (0.005, 0.01, 0.02, 0.05, 0.10, 0.25):
        rows.append([_pct(r, 1)] + [_pct((1 - r) ** n, 1) for n in (1, 3, 5, 10, 15)])
    return _table(["risk/trade", "1", "3", "5", "10", "15"], rows)


def section_monte_carlo(paths=10000, trades=200, seed=20261002):
    import numpy as np
    rng = np.random.default_rng(seed)
    rows = []
    for p in (0.35, 0.45, 0.50, 0.55):
        for b in (1.0, 1.5, 2.0):
            for r in (0.0025, 0.005, 0.01, 0.02, 0.05, 0.10, 0.25):
                wins = rng.random((paths, trades)) < p
                factor = np.where(wins, 1 + r * b, 1 - r)
                eq = np.cumprod(factor, axis=1)
                peak = np.maximum.accumulate(np.concatenate([np.ones((paths, 1)), eq], axis=1), axis=1)[:, 1:]
                dd = 1 - eq / peak
                mdd = dd.max(axis=1)
                rows.append((_pct(p, 0), f"{b:g}R", _pct(r), _pct(float(np.median(mdd)), 1),
                             _pct(float(np.percentile(mdd, 95)), 1),
                             _pct(float((mdd >= 0.20).mean()), 1), _pct(float((mdd >= 0.50).mean()), 1),
                             _pct(float((mdd >= 0.90).mean()), 1),
                             f"{p * b - (1 - p):+.2f}R"))
    return _table(["win%", "payoff", "risk/trade", "median maxDD", "p95 maxDD", "P(DD≥20%)",
                   "P(DD≥50%)", "P(DD≥90% ruin-like)", "expectancy/trade"], rows)


def test_vectors():
    vecs = [
        ("BTCUSDT", 100, 0.005, 10, None), ("BTCUSDT", 100, 0.01, 10, None),
        ("BTCUSDT", 100, 0.02, 10, None), ("BTCUSDT", 100, 0.01, 10, 0.01),
        ("BTCUSDT", 1000, 0.01, 10, 0.005), ("ETHUSDT", 100, 0.01, 10, None),
        ("ETHUSDT", 100, 0.01, 10, 0.005), ("ETHUSDT", 25, 0.01, 10, 0.01),
        ("SOLUSDT", 100, 0.005, 10, None), ("SOLUSDT", 100, 0.04, 10, None),
        ("SOLUSDT", 100, 0.01, 10, 0.01), ("SOLUSDT", 10, 0.01, 10, 0.005),
        ("SOLUSDT", 100, 0.005, 50, None), ("SOLUSDT", 100, 0.005, 50, 0.01),
        ("AVAXUSDT", 100, 0.02, 10, None), ("AVAXUSDT", 100, 0.02, 10, 0.005),
        ("XRPUSDT", 100, 0.01, 10, None), ("XRPUSDT", 5, 0.01, 10, 0.01),
        ("DOGEUSDT", 100, 0.03, 10, None), ("DOGEUSDT", 100, 0.03, 10, 0.02),
        ("DOGEUSDT", 50, 0.01, 20, 0.005), ("BTCUSDT", 500, 0.0025, 10, 0.0025),
    ]
    rows = []
    for sym, eq, s, lev, r in vecs:
        price = SYMBOLS[sym][3]
        stop = price * (1 - s)
        res = (current_policy(sym, price, s, eq, eq, lev) if r is None
               else risk_based_policy(sym, price, s, eq, eq, r, lev))
        raw = (current_target_contracts(sym, price, eq, lev) if r is None
               else f"{res['raw_contracts']:.3f}")
        rows.append((sym, eq, price, f"{stop:.6g}", f"{lev}x", "CURRENT" if r is None else _pct(r),
                     raw, res["contracts"], _usd(res["notional"]), _usd(res["margin"]),
                     f"{res['loss']:.4f}", _pct(res["loss_pct_equity"]),
                     res["result"] if res["result"] == "ACCEPT" else f"REJECT ({res['blocker']})"))
    return _table(["symbol", "equity", "entry", "stop (LONG)", "lev", "risk_pct", "raw contracts",
                   "contracts", "notional", "margin", "projected loss", "loss %eq", "decision"], rows)


SECTIONS = {
    "numeric": section_numeric_example, "stops": section_stop_sensitivity,
    "equity": section_equity_feasibility, "mincontract": min_contract_loss_table,
    "leverage": section_leverage_invariance, "aggregate": section_aggregate,
    "consecutive": section_consecutive, "montecarlo": section_monte_carlo,
    "vectors": test_vectors,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--section", choices=sorted(SECTIONS), action="append")
    parser.add_argument("--paths", type=int, default=10000)
    args = parser.parse_args()
    for name in args.section or SECTIONS:
        print(f"\n<!-- section:{name} -->")
        fn = SECTIONS[name]
        print(fn(paths=args.paths) if name == "montecarlo" else fn())


if __name__ == "__main__":
    main()
