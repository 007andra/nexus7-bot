# Controlled integration: LIVE observability and hard-gate shadow research

OBSERVABILITY_ONLY=true
RESEARCH_ONLY=true
LIVE_ENTRIES_REMAIN_BLOCKED=true
decision_effect=NONE
execution_effect=NONE
live_authority_unchanged=true

Base: `43c6fe92cd45895f54014e252247c215dc437ca0` (`migration/binance-usdm`).
Branch: `integration/shadow-observability-hardgate-v1`.
No production branch writes, PR merge, deployment, Railway changes or recovery renewal.

## Composition provenance

Local cherry-picks, in order, all conflict-free:

| Source | Exact source HEAD | HEAD after integration | Smoke |
|---|---|---|---|
| #492 | `43c9844ed2fa10d58efd19544a94b1a71174d238` | `b391aeecdb44afcf22b55c871d667761c1d4c9e8` | candidate audit 20/20 |
| #494 | `fba2bac5047e076e0063aea799bebc15a562ecf5` | `db3c06b8e171e190150fd127be3eb4547f38aa9b` | BBO + audit 47/47 |
| #496 | `c703d56364d725a2b4e4d0b17b57cd188da28789` | `0742734dd4bb75b278d2c7bf43d8e1ec16893e3f` | shadow + BBO + audit 121/121 |

#493 (`a175a634fd3dde017faf464ee9b935b408e5c624`) is excluded from ancestry.
Its seven added files, including `feasibility_frontier.py`, its report/dataset
and `bbo_cost_shadow.py`, are absent. The BBO implementation included here is
#494's `bbo_cost_shadow_v1.py`, not #493's offline model.

## Minimal integration changes

- Correct #494's previously true flag defaults to false.
- Add default-off switches for #492's new terminal and restored matrix output.
- BBO owner exports immutable `snapshot_for_research`, read after NEXUS so a
  reconnect during evaluation invalidates the old generation. No cache access
  outside its owner, private transport or REST fallback.
- Hard-gate scan calls `nexus_ai.decide` once. It never enters the wrapped LIVE
  `_nexus_validate`. BBO computes passive cost comparisons from that decision;
  it does not call NEXUS. Terminal/Matrix format the existing structured row.
- Explicit HARD_GATE_SHADOW terminal and counterfactual matrix classification.
  The LIVE terminal collector rejects this population, even if a record is
  accidentally routed to it. Existing LIVE stage decisions remain unchanged.
- Reuse persisted candidate IDs before NEXUS. Healthy storage makes duplicate
  scans and restart idempotent for decisions, observers and dataset rows.
- Isolate BBO and telemetry errors so research persistence can continue.

## Defaults

| Flag | Default |
|---|---|
| HARD_GATE_SHADOW_SCAN | false |
| NEXUS_BBO_COST_SHADOW | false |
| NEXUS_BBO_COST_SHADOW_PERSIST | false |
| CANDIDATE_TERMINAL_TELEMETRY | false |
| MIN_ORDER_FEASIBILITY_MATRIX | false |

No environment values are changed by this integration. Installation-time
BBO/terminal switches require restart after a separately authorized enablement.
Existing latency-parser fixes from #492 reuse the baseline observer; they add
no decision authority or independently enabled feature.

## Composed flow and safety

Cached market data -> strategy/pullback -> counterfactual MIN_ORDER -> one
NEXUS decision -> immutable BBO snapshot / passive comparison -> structured
shadow candidate, terminal and matrix -> separate candidate/outcome dataset.

All applicable records carry the same candidate ID, population=HARD_GATE_SHADOW,
shadow_only=true, live_eligible=false, live_block_reason=DRAWDOWN_HARD_GATE,
decision_effect=NONE, execution_effect=NONE and live_authority_unchanged=true.
Fidelity is DEGRADED (including absent funding/OI). Main Champion × Challenger
candidate/outcome/disagreement/readiness/promotion populations receive no input.

The original DRAWDOWN_HARD_GATE remains authoritative. No LIVE `_open`, sizing,
CROSS, predispatch, intent, ManagedOrder mutation, order, cancel, SL/TP, position,
submission commit or private API is reachable from the research path.
Tests force NEXUS and BBO approval and instrument 35 actual boundaries; every
counter is zero. Tests also execute real NEXUS with passive observers.

BBO uses the separate public connection from #494; trading `/market` and its
protected handler remain untouched. Missing/stale/error/reconnect BBO changes
only the research observation. Snapshot generation/freshness checks remain in
its owner. Default extra REST calls = 0.

Single-flight, cancellation retention, mid-scan gate-clear abort and task-local
logger isolation remain tested. A cleared gate discards the in-progress
candidate; it never transfers it to LIVE. Existing normal cycles decide anew.

Outcomes use complete cached closed-bar paths, gross hypothetical returns,
MFE/MAE and separate 60/240-minute records. No fills or trade ledger are created.

## Verification

```
python -m compileall -q bot/ tests/ main.py main_hardened.py
ruff check bot/ tests/ main.py main_hardened.py --select E9,F63,F7,F82
python -m pyflakes bot/ main.py main_hardened.py
python -m bot.selfcheck
python -m tests.run_offline
python -m bot.release_proof
python -m tests.run_offline tests.test_shadow_observability_integration
python -m tests.benchmark_shadow_observability_integration
```

The Pyflakes CI criterion is zero undefined names. Full-suite/release local
counts include two/one PostgreSQL-dependent skips without TEST_POSTGRES_DSN;
the existing runner prints inaccurate skip totals, so CI PostgreSQL evidence
must be checked explicitly. Final results and exact SHA are recorded in the PR.

The 42 composed tests include inherited hard-gate adversarial tests plus real
BBO/terminal/matrix/outcomes, one-decision counting, all-off defaults, failures,
reconnect during NEXUS, gate clear during BBO, duplicate/restart and real NEXUS.
Full suites additionally cover runtime contracts, startup fail-closed,
PAPER/SHADOW deterministic execution, durable restart/idempotency, Binance
CROSS dispatch, sizing/protection, market-data bridge and BBO feed races.

## Performance and residual limits

Benchmark: 25 symbols, 20 samples after one warmup. Synthetic strategy signals,
real pullback math, counterfactual sizing, NEXUS, BBO model, telemetry and
in-memory SQLite. Each sample uses new formation IDs and fresh seeded BBO so
all 500 sampled candidates traverse the full composition. Timers separately
measure BBO (including snapshot/log), terminal (format+emit) and DB awaited
operations. Overheads are included in scan time, not incremental estimates
against a second run. They exclude production network/database latency.

No new runtime deadline or optimization is introduced. Existing candidate-write
and outcome timeouts remain; the new dedup read uses the database adapter's
existing behavior. There is no global scan deadline. The awaited scan adds
loop latency. Degraded features and cache gaps constrain research usefulness.
Dedup relies on successful research storage; a DB failure may lead to repeated
research decisions/logs on retry. No exactly-once delivery guarantee is claimed
across crash, DB failure or multiple processes. Single-flight is per engine.
The flag remains off; a production enablement requires a separate task.
