# Binance LIVE sizing vs minimum order — forensic audit

- Base: `migration/binance-usdm` @ `73dfc7e8157dc2c1d1172ced010f5f810000eeae`
- Branch: `audit/binance-live-sizing-minimum-order`
- Scope: read-only. No order, position, leverage, margin mode, Railway, env or
  secret was touched. No risk parameter, threshold, SL/TP, score or NEXUS rule
  was changed.

## EXECUTIVE SUMMARY

**RESULT = A — `CORRECT_FAIL_CLOSED_CAPITAL_INSUFFICIENT`.**

`stop_risk_qty = 0` for UNIUSDT, FILUSDT, APTUSDT and NEARUSDT is the correct,
fail-closed answer. With equity 5.8827 USDT and `MAX_RISK_PCT = 1%` the risk
budget is 0.058827 USDT, and the smallest Binance-valid order of each symbol
would lose 1.6×–3.0× that budget at the stop (fees and slippage included).
The runtime floors the risk quantity to the step, compares it to the smallest
valid order (`max(minQty, minNotional/price)` rounded **up** to the step), and
refuses to trade instead of rounding up. That is the documented contract.

No unit error, float-rounding error, wrong filter, wrong leverage use or
double-counting was found. The only defects are observability:

1. the engine message `qty=0 — saldo insuficiente ($5.88)` (engine.py) names
   the wrong cause — balance/margin is sufficient; the **risk budget** is
   what is insufficient for the exchange minimum;
2. `binding=MINIMUM_ORDER` does not say whether `minQty` or `minNotional` sets
   the minimum.

This PR adds one read-only log line, `[SIZING_DECOMPOSITION]`, emitted only
when the risk quantity is 0, plus 26 tests and this document. Sizing results
are byte-for-byte unchanged (proved by running the new tests against base
code: 25/26 pass there; the only failure is the new log line itself).

Independent second gate: even with unlimited capital these four candidates
would be blocked by `FINAL_LOSS_BUDGET` at 50x, because that gate requires
`stop% + 0.32% stress costs ≤ 1%` (stop ≤ 0.68%). See ROOT CAUSE.

## RUNTIME PIPELINE

Verified in a LIVE-shaped composition (`main_hardened` → `sitecustomize` →
`bot.runtime_bootstrap`, `ExchangeClient()` → `TradingEngine(client)`,
network guard on): `engine.risk` is
`bot.professional_risk_adapter.ProfessionalRiskAdapter` and
`bot.engine.minimum_base_quantity` is
`bot.final_sizing_invariants.install.<locals>._final_operator_authoritative_quantity`.

```
engine._open(symbol, side, entry, stop, ...)
 └─ minimum_base_quantity(...)              # final hook (final_sizing_invariants)
     ├─ _operator_target_quantity            # available × 0.50 × L / price, floor step
     ├─ engine.risk.size(symbol, entry, instruments)
     │    = ProfessionalRiskAdapter.size
     │      ├─ cost snapshot: taker_fee, slippage_allowance (execution_cost)
     │      └─ RiskManagerV3.size_for_stop
     │           ├─ quantity.quantity_rules(info)      # step, minQty, minNotional (fail closed)
     │           ├─ quantity.minimum_base_quantity(info, entry)
     │           └─ professional_risk.stop_risk_size   # the canonical sizing formula
     ├─ _select_final_quantity = min(stop_risk_qty, operator_cap_qty)
     ├─ margin-cap assertion
     └─ final_loss_budget.validate           # independent loss/margin gate
 → qty == 0  → engine logs "qty=0 — saldo insuficiente" and skips the entry
```

Canonical authorities:

| Concern | Authority |
|---|---|
| Stop-risk quantity | `bot/professional_risk.py::stop_risk_size` |
| Exchange quantity rules | `bot/quantity.py::quantity_rules`, `minimum_base_quantity` |
| Filter loading | `bot/binance.py` (exchangeInfo → `qtyStep`, `minQty`, `minNotional`, `tickSize`) |
| Costs used in sizing | `ProfessionalRiskAdapter.size` (cost snapshot + `NEXUS_EXPECTED_SLIPPAGE_PCT`) |
| Final quantity | `bot/final_sizing_invariants.py` (`min(stop_risk_qty, operator cap)`) |
| Final loss gate | `bot/final_loss_budget.py` |

## FORMULAS

Runtime (`stop_risk_size`), exactly:

```
risk_budget            = equity × risk_pct
risk_per_unit          = |entry − stop|
loss_per_unit          = risk_per_unit + entry × taker_fee × 2 + entry × slippage
qty_by_risk            = risk_budget / loss_per_unit
qty_by_margin          = available × max_margin_pct × leverage / entry
raw_qty                = min(qty_by_risk, qty_by_margin)
qty                    = floor(raw_qty / step) × step          # Decimal ROUND_FLOOR
min_valid_qty          = ceil(max(minQty, minNotional / entry) / step) × step
if qty < min_valid_qty → qty = 0, binding = MINIMUM_ORDER
assert qty × loss_per_unit ≤ risk_budget × 1.000001
assert qty × entry / leverage ≤ available × max_margin_pct
```

Then `final_qty = min(stop_risk_qty, floor(available × 0.50 × L / entry))`.

Production costs (from `[RISK_V3_CORE] taker_bps=5.000 slippage_allowance_bps=20.000`):
taker 0.05 % per side, slippage 0.20 % → costs = 0.30 % of entry per unit.

The conceptual formula `qty = equity × risk% / |entry − stop|` is the same
formula without costs; the runtime is strictly more conservative.

## DIMENSIONAL ANALYSIS

| Quantity | Unit |
|---|---|
| equity, available, risk_budget, min_notional, margin | USDT |
| entry, stop, risk_per_unit, loss_per_unit | USDT / base unit |
| taker_fee, slippage, risk_pct, max_margin_pct | dimensionless |
| leverage | dimensionless (notional / margin) |
| qty, step, minQty | base-asset units (Binance USD-M linear: `quantityUnit=BASE_ASSET`, multiplier 1) |

`qty_by_risk = USDT / (USDT/base) = base` ✔; `notional = base × USDT/base = USDT` ✔;
`margin = USDT / 1 = USDT` ✔; `minNotional / entry = USDT / (USDT/base) = base` ✔.
No contract/coin mixing: `quantity_rules` rejects non-`BASE_ASSET` units for Binance.

## BINANCE FILTERS

`bot/binance.py` reads `exchangeInfo`:

- `MARKET_LOT_SIZE` is preferred over `LOT_SIZE` for `stepSize`/`minQty`
  (entries are MARKET orders — correct);
- `MIN_NOTIONAL` (`notional` or `minNotional`) → `minNotional`;
- `PRICE_FILTER.tickSize` → `tickSize`.

Stored as floats but every rule decision re-enters `Decimal(str(x))`:
`_floor_step`, `quantity_rules` (minQty must be an integer multiple of step,
else fail closed), `minimum_base_quantity` (ceil), `validate_base_quantity`.
`maxQty` is not loaded; it is irrelevant at this capital (qty ≪ maxQty) —
listed as residual. `quantityPrecision` is not used; step is authoritative
(precision is derived from step — correct).

Filter values used in the reproduction (USD-M perpetuals):

| Symbol | stepSize | minQty | minNotional |
|---|---|---|---|
| UNIUSDT | 1 | 1 | 5 |
| FILUSDT | 0.1 | 0.1 | 5 |
| APTUSDT | 0.1 | 0.1 | 5 |
| NEARUSDT | 1 | 1 | 5 |

`fapi.binance.com` was egress-blocked from the audit sandbox (proxy 403), so
live `exchangeInfo` could not be fetched here. The values above are
consistent with production logs: the operator caps logged in production are
exact multiples of these steps (FIL `127.5`, APT `170`, NEAR `26`), and the
ADAUSDT PASS at 12:45:20 (qty 23, projected stop loss 0.057006, required
margin 0.119370) is reproduced exactly by the same code path with
`step=1, minQty=1, minNotional=5` (test_13). Re-verify with a read-only
`exchangeInfo` query from production before any threshold decision.

## ROUNDING POLICY

- Risk quantity: always **floor** to step (`ROUND_FLOOR` in Decimal) — never up.
- Minimum valid quantity: **ceil** to step (a floored minimum could be below
  `minNotional`).
- Below-minimum → 0. There is no path that replaces 0 by `minQty`.
- Edge tests: exact boundary passes (`2.0` stays `2.0`), `99.999` → `1.0`,
  `0.1 + 0.2` → `0.3`, `2.9999999999999996` → `2.0`, `0.0999999999` → `0`
  (test_14, test_15); 3 000-case property test: every non-zero result is an
  integral number of steps, ≥ minQty, and loses ≤ budget (test_18_23_24_26).

## LEVERAGE SEMANTICS

Leverage enters only `required_margin = notional / L` and the collateral cap
`qty_by_margin`. It never enters `risk_budget` or `loss_per_unit`.
test_16_17: for L ∈ {1, 10, 50, 125} with ample collateral the quantity and
stop loss are identical; with scarce collateral higher leverage only raises
the margin ceiling and never exceeds the risk quantity. The configured 50x
cannot create risk capacity. In `FINAL_LOSS_BUDGET` higher leverage makes the
gate **stricter** (limit = 50 % of entry margin = notional / (2L)).

## SYMBOL-BY-SYMBOL REPRODUCTION

Inputs: equity = available = 5.8827, risk 1 %, budget 0.058827, L = 50,
costs 0.30 %, Decimal arithmetic (`bot.sizing_decomposition.decompose`).
`OPERATOR_CAP_QTY` = `floor(available × 0.50 × 50 / entry)` at the audited
prices (production values at their own timestamps: FIL 127.5, APT 170, NEAR 26).

| SYMBOL | EQUITY | RISK_BUDGET | ENTRY | SL | STOP_% | RAW_QTY | STEP_SIZE | MIN_QTY | MIN_NOTIONAL | MIN_VALID_QTY | RISK_AT_MIN_QTY | MARGIN_AT_MIN_QTY | STOP_RISK_QTY | OPERATOR_CAP_QTY | FINAL_QTY | BINDING_CONSTRAINT | RESULT |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| UNIUSDT | 5.8827 | 0.058827 | 10.130000 | 10.013261 | 1.1524 % | 0.399833 | 1 | 1 | 5 | 1 | 0.147129 | 0.202600 | 0 | 14 | 0 | MIN_QTY_BINDING | BLOCK (INSUFFICIENT_RISK_BUDGET) |
| FILUSDT | 5.8827 | 0.058827 | 1.1438 | 1.121739 | 1.9287 % | 2.307629 | 0.1 | 0.1 | 5 | 4.4 | 0.112167 | 0.100654 | 0 | 128.5 | 0 | MIN_NOTIONAL_BINDING | BLOCK (INSUFFICIENT_RISK_BUDGET) |
| APTUSDT | 5.8827 | 0.058827 | 0.8704 | 0.856937 | 1.5468 % | 3.659716 | 0.1 | 0.1 | 5 | 5.8 | 0.093230 | 0.100966 | 0 | 168.9 | 0 | MIN_NOTIONAL_BINDING | BLOCK (INSUFFICIENT_RISK_BUDGET) |
| NEARUSDT | 5.8827 | 0.058827 | 5.334 | 5.174647 | 2.9875 % | 0.335474 | 1 | 1 | 5 | 1 | 0.175355 | 0.106680 | 0 | 27 | 0 | MIN_QTY_BINDING | BLOCK (INSUFFICIENT_RISK_BUDGET) |

UNIUSDT in detail (Decimal):

```
risk_budget   = 5.8827 × 0.01                       = 0.058827 USDT
risk_per_unit = 10.130000 − 10.013261               = 0.116739 USDT/UNI
costs         = 10.13 × (0.0005×2 + 0.002)          = 0.030390 USDT/UNI
loss_per_unit = 0.116739 + 0.030390                 = 0.147129 USDT/UNI
raw_qty       = 0.058827 / 0.147129                 = 0.399833 UNI
floor(step=1)                                        = 0 UNI
min_valid_qty = ceil(max(1, 5/10.13 = 0.4936)) = 1 UNI   (minQty binds)
risk at 1 UNI = 0.147129 USDT = 2.50 × budget        → BLOCK
(price-only, zero-cost: 0.116739 = 1.98 × budget     → still BLOCK)
```

FIL and APT are not "rounding to zero": the floored risk quantity is 2.3 FIL /
3.6 APT, but `minNotional = 5 USDT` requires ≥ 4.4 FIL / ≥ 5.8 APT, which
would lose 0.1122 / 0.0932 USDT (1.91× / 1.58× the budget).

## MINIMUM REQUIRED EQUITY

`equity_min = risk_at_min_valid_qty / 1 %` (same stop, same costs):

| Symbol | Equity required | Current | Gap |
|---|---|---|---|
| UNIUSDT | 14.7129 | 5.8827 | ×2.50 |
| FILUSDT | 11.2167 | 5.8827 | ×1.91 |
| APTUSDT | 9.3230 | 5.8827 | ×1.58 |
| NEARUSDT | 17.5355 | 5.8827 | ×2.98 |

Reaching these by raising `risk_pct` would put 2.5–3 % of equity at risk per
trade while the drawdown headroom is ≈1 % (below). That is not recommended.

## MINIMUM REQUIRED MARGIN

Margin for the minimum order at 50x: UNI 0.2026, FIL 0.1007, APT 0.1010,
NEAR 0.1067 USDT. Available required under the operator cap (50 %):
UNI 0.4052, FIL 0.2013, APT 0.2019, NEAR 0.2134 USDT; under
`max_margin_pct = 10 %`: UNI 2.026, FIL 1.007, APT 1.010, NEAR 1.067 USDT.
All are satisfied by available = 5.8827. **Margin is not the constraint** —
hence "saldo insuficiente" is a misleading label.

## DRAWDOWN HEADROOM

```
peak = 6.4680, equity = 5.8827
drawdown  = (6.4680 − 5.8827) / 6.4680 = 9.049 %
hard stop = 6.4680 × 0.90 = 5.82120
headroom  = 5.8827 − 5.8212 = 0.0615 USDT ≈ 1.045 × current risk budget
```

One full stop-out at the current 1 % budget leaves equity at ≈5.8239, just
above the 10 % limit; any trade sized to a symbol minimum (1.6–3 % of equity)
would breach the drawdown limit on a single stop.

## ROOT CAUSE

1. **Primary (sizing):** capital × 1 % is smaller than the stop loss of the
   smallest Binance-valid order. UNI/NEAR: `minQty = 1` binds; FIL/APT:
   `minNotional = 5 USDT` binds. The runtime correctly refuses.
2. **Secondary (independent gate):** at L = 50, `FINAL_LOSS_BUDGET` limits the
   projected loss to 50 % of entry margin = notional / 100, i.e. requires
   `stop% + 0.32 %` stress costs ≤ 1 %. UNI (1.15 %), FIL (1.93 %),
   APT (1.55 %), NEAR (2.99 %) fail this at any equity (ATOMUSDT was blocked
   by exactly this in production: `projected_loss_exceeds_50pct_entry_margin`).
   So raising equity alone would unblock the sizing gate but not these setups.
3. **Observability:** "saldo insuficiente" and `MINIMUM_ORDER` hide the real
   cause; fixed by `[SIZING_DECOMPOSITION]` (decision_effect=NONE).

## BUG OR CORRECT BLOCK

**Correct block.** `stop_risk_qty = 0` is the right answer for all four
symbols. Nothing in formula, units, filters, rounding or leverage handling is
wrong. No sizing code was changed.

## TEST EVIDENCE

`tests/test_binance_live_sizing_minimum_order.py` — 26 tests (they cover the
30 mandated scenarios; several scenarios share one test):

- incident reproduction through the real path (adapter → `RiskManagerV3` →
  quantity rules → final hook), asserting `place_order` is never awaited:
  UNI, FIL, APT, NEAR → 0 with the expected binding; ADA production PASS
  reproduced exactly (qty 23); minimum-equity threshold flips BLOCK→PASS
  exactly at `required_equity`; FINAL_LOSS_BUDGET blocks at 50x with equity 1000;
- venue metadata: BTC/ETH steps, integer lot, below-one-step, below-minQty,
  minNotional binding, margin-cap binding, malformed/missing metadata,
  zero stop distance, invalid equity — all fail closed;
- rounding and leverage: exact boundary, Decimal epsilon, leverage
  invariance, 3 000-case property test, PASS path ≥ minNotional and ≤ cap;
- observability: the decomposition line is emitted on qty=0, carries no
  secret-like field, and a crashing decomposition cannot change the result;
  thresholds (`MARGIN_FRACTION = 0.50`, `MAX_RISK_PCT = 0.01`) unchanged.

Base-fail/fix-pass: running the new file against base code (with only the
pure `bot/sizing_decomposition.py` copied in) → 25 pass, 1 fails
(`test_decomposition_log_is_emitted_and_never_changes_quantity`, the new
log). This proves every sizing assertion already held on base.

Full offline suite: base 1845 tests / 1 failing suite, head 1871 / 1 failing
suite; the failing suite is `tests.test_research_process` on both
(sandbox-only, pre-existing). `bot.release_proof` PASS, `bot.selfcheck` no
critical, ruff/pyflakes clean.

## RESIDUAL RISKS

- Live `exchangeInfo` could not be fetched from the audit sandbox; filters
  are evidenced by production logs and the exact ADA reproduction. Confirm
  them read-only from production.
- `maxQty` / `MARKET_LOT_SIZE.maxQty` is not loaded (irrelevant at current
  capital; relevant only for much larger accounts).
- The engine message "saldo insuficiente" is still emitted by `engine.py`; the
  new decomposition line appears next to it with the true cause. Changing the
  engine string was left out to keep this PR free of engine edits.
- With equity ≈5.88 USDT and ≈0.06 USDT drawdown headroom, the account cannot
  take any trade on symbols whose minimum order risks > 1 % of equity. Options
  that keep risk discipline: add capital, restrict the universe to symbols
  whose `min_valid_qty × loss_per_unit ≤ risk_budget` (e.g. ADAUSDT-like
  price/step geometry), or accept tighter structural stops — each is an
  operator decision, not a code fix.
