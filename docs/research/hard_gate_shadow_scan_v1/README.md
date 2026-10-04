# NEXUS Hard-Gate Shadow Research Scan v1

RESEARCH_ONLY=true
SHADOW_ONLY=true
LIVE_ENTRIES_REMAIN_BLOCKED=true
decision_effect=NONE
execution_effect=NONE
live_authority_unchanged=true

Base: `43c6fe92cd45895f54014e252247c215dc437ca0`, production branch
`migration/binance-usdm`. Independent of PRs #492, #493 and #494.
No merge, deployment, recovery renewal or Railway variable changes are included.

## WHY THIS CANNOT REOPEN LIVE TRADING

The original skip is in `TradingEngine.run`, before `_scan_all_and_enter`:
`not self.active` skips entry processing, and otherwise `risk.can_open()` must
pass. The drawdown balance wrapper can preserve an inactive engine; the risk
wrapper rejects a breached threshold without override/valid recovery.

A separate awaited `scan_if_enabled` hook runs before those existing branches.
It never changes either predicate, and it never calls the LIVE scanner, `_open`,
`_nexus_validate`, private capital reader, sizing authority or dispatch. The
reviewed `operator_runtime_policy.py` still owns the final runtime `run` callable.
Existing position management remains in the original loop before this hook.

The shadow path is:

1. Flag enabled AND read-only hard-gate predicate confirms the block.
2. Per-engine single-flight registry outside engine state.
3. Deep copies of cached 15m/1h/4h candles (200/100/120 requested), with minimum
   60/40/20. Missing cache skips the symbol; no network fallback.
4. Research-owned Analyzer using the composed production strategy/pullback math.
5. Contextual pullback capture, including blocked candidates for outcome study.
6. Read-only session/regime/positive-PnL math, counterfactual minimum-order proof.
7. Direct `nexus_ai.decide` with context-local fallback costs. Approval remains a
   research result and is never returned to the LIVE candidate funnel.
8. Independent append-only candidate/outcome tables and pure Champion ×
   Challenger record builder. Optional already-loaded BBO pure comparison with a copied
   snapshot supplied by the BBO owner.

All candidate records carry `shadow_only=true`, `population=HARD_GATE_SHADOW`,
`live_entries_blocked=true`, `live_block_reason=DRAWDOWN_HARD_GATE`,
`live_eligible=false`, `live_candidate=false`, `decision_effect=NONE`,
`execution_effect=NONE`, `live_authority_unchanged=true`.

Dynamic tests force NEXUS approval and instrument actual engine/adapter/risk/
order-registry/private-transport boundaries. The tests repeat against the final
bootstrap graph and compare capital, HWM, cooldown, duplicate-related data,
positions, ownership/fencing and durable-engine state before/after. The actual
engine-loop tests exercise both original hard-gate skip branches.

## Configuration and fidelity

`HARD_GATE_SHADOW_SCAN=false` by default. Missing/false means no cache access,
no analyzer, no research DB access and no new task. Existing LIVE behavior and
non-shadow observer semantics are retained. This PR does not enable the flag.

`HARD_GATE_SHADOW_PUBLIC_DERIVATIVES` is deliberately not implemented: even if
someone supplies that name, it grants no network capability. Additional REST
calls per scan are zero. Funding, open interest, OI delta, fresh news score and
private fee calibration are unavailable; every record has
`evaluation_fidelity=DEGRADED` and explicit `missing_features`. Cached prices and
fallback fees/slippage are research assumptions, not fresh LIVE economics.

MIN_ORDER uses the cached confirmed capital snapshot and the normal configured
risk percentage (or configured post-target percentage), **not** the hard-gate
LIVE risk authorization. It reuses pure `sizing_decomposition.decompose`, without
calling final sizing. `counterfactual=true`, `counterfactual_risk_pct`,
`risk_budget`, `min_valid_qty`, `risk_at_min_qty`, `binding` and
`live_risk_authority=BLOCKED_BY_DRAWDOWN_HARD_GATE` are retained. Missing capital
is UNKNOWN. There is no `live_executable=true` field. NEXUS may be evaluated for
research even when this hypothetical minimum-order test fails.

## Population isolation, persistence and outcomes

Only `hard_gate_shadow_candidates_v1` and `hard_gate_shadow_outcomes_v1` are
written. Candidate IDs start with `HARD_GATE_SHADOW:` and include symbol, side,
setup and formation bucket. Repeated IDs are immutable (`ON CONFLICT ... DO
NOTHING`). The Champion × Challenger pure builder is reused, but its main
persistence API rejects this population; its observer and existing general
shadow capture also refuse to enroll these records in the main dataset.

Therefore main-study `candidate_population`, `known_outcomes`,
`decision_disagreements`, readiness and promotion counters receive zero input.
There is no automatic promotion/import into LIVE on restart or replay. BBO
comparisons, if #494 is loaded independently, are also stored only inside this
separate candidate payload, not in its shared BBO table. The scan never reads
the BBO cache directly. `scan(..., bbo_views=...)` accepts copied snapshots from
the owner for compatibility/research callers. The default engine hook supplies
no BBO views, so that observation is unavailable until a separately reviewed
owner-side handoff is wired; this PR does not modify or stack #494.

Outcomes use cache-only complete closed 15m paths at 60/240 minutes. Sampling
starts at the next 15m boundary to exclude pre-capture high/low observations.
`future_return`, `MFE`, `MAE` are **gross hypothetical-entry** fractions; no fills,
trade ledger, simulated stop/target ordering or realized PnL is claimed. Missing
bars are explicitly `UNKNOWN_CACHE_GAP`, not wins/losses. Observations resume
from the independent tables after restart, only while the hard gate remains
active. Outcome DB reads are bounded to 200 pending rows per horizon per scan.

## Context and logging isolation

A ContextVar suppresses analyzer research observers only within this scope:
score-floor/session-penalty observers would otherwise reach LIVE validation;
other observers/score/geometry/volume/triage caches are also kept separate.
Runtime-truth analyzer hooks bypass LIVE population capture in this context.
Outside it, original call/return semantics remain intact.

The application logger has a fixed context-routing implementation installed at
module initialization. Inside this context, details go to a separate static
logger/sink without LIVE funnel/terminal/Telegram handlers. Structured research
records use that dedicated sink directly. No scan modifies logger levels,
handlers, filters or propagation. No global INFO-level window is used. A normal
concurrent task retains normal logging semantics throughout the shadow scan.

## Gate changes and cancellation

The threshold/override/recovery predicate is checked between stages and after
awaits, including persistence boundaries. If it clears:
`shadow_scan_aborted_reason=LIVE_HARD_GATE_CLEARED`. Previously persisted rows
remain research only; pending candidates are never transferred to LIVE. The
normal LIVE loop makes its own subsequent decisions through its existing gates.
Summary gate fields describe the initial blocked context; the abort reason
reports a later transition.

A concurrent scan for the same engine exits with
`shadow_scan_skipped_reason=PREVIOUS_SCAN_RUNNING`. Cancellation retains that
slot until an already-running computation thread exits, then releases it.
Research IO is bounded to one second per candidate persistence and one second
for the outcome pass. Exceptions are contained; no risk/recovery state changes.

## Reproducible verification

Use installed requirements plus Ruff/Pyflakes; no exchange credentials.

```
python -m compileall -q bot/ tests/ main.py main_hardened.py
ruff check bot/ tests/ main.py main_hardened.py --select E9,F63,F7,F82
python -m pyflakes bot/ main.py main_hardened.py  # CI gates undefined names
python -m bot.selfcheck
python -m tests.run_offline
python -m bot.release_proof
python -m tests.run_offline tests.test_hard_gate_shadow_scan tests.test_hard_gate_shadow_runtime tests.test_hard_gate_shadow_composed_isolation
python -m tests.benchmark_hard_gate_shadow
```

`tests.run_offline` uses clean child processes and rejects external sockets.
The full suite includes runtime contracts, startup fail-closed, PAPER/SHADOW
E2E, durable restart/idempotency, sizing, Binance dispatch/CROSS/protection,
market-data bridge and Champion × Challenger. Real PostgreSQL proof requires
`TEST_POSTGRES_DSN`; the repository CI provisions its isolated PostgreSQL service.

Benchmark reports 20 warm samples plus a discarded warmup, 25 symbols, composed
analyzer and a signal-heavy workload with real NEXUS decisions. Inputs are
synthetic cached candles and in-memory SQLite. It measures local research cost,
not Railway production latency, and explicitly exercises five concurrent scan
requests. The signal-heavy case forces strategy signals only, not NEXUS results.

For each peer, create a disposable worktree at the final candidate HEAD, merge
ONLY the pinned peer HEAD locally with `--no-commit`, run the full offline suite
and `python -m tests.hard_gate_shadow_compatibility <PR-number>`. None of those
merge trees/commits belongs on the publication branch.

Pinned peer heads:
- #492 `43c9844ed2fa10d58efd19544a94b1a71174d238`
- #493 `a175a634fd3dde017faf464ee9b935b408e5c624`
- #494 `fba2bac5047e076e0063aea799bebc15a562ecf5`

## Residual limits

Research evidence is degraded, has a separate selection population, and cannot
support LIVE readiness claims. Cache gaps lose outcome coverage. Synthetic
benchmarks do not establish production timing. The scan is awaited by the
engine loop, so research computation/IO adds latency before its next iteration;
this is bounded per IO operation but not a global worst-case deadline. Enablement
requires a separate operator action after review; it is not part of this PR.
