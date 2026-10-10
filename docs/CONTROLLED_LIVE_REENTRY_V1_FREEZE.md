# NEXUS-7 Controlled LIVE Re-entry V1 — Architecture Freeze

Status: release contract for the final one-shot LIVE execution proof.

## Objective

Prove one natural, protected Binance USD-M entry end-to-end without rewriting
historical performance or weakening unrelated safety gates.

Expected execution chain:

Strategy -> NEXUS -> fresh capital -> stop-risk sizing -> pre-dispatch market/risk
checks -> durable one-shot authorization -> Binance submit -> ACK/fill -> SL/TP
protection -> reconciliation.

## Frozen safety contract

- Historical HWM is preserved.
- Global MAX_DRAWDOWN remains unchanged.
- The normal drawdown hard gate remains authoritative outside this one-shot path.
- The bridge applies only to the drawdown threshold and only while the exact
  controlled re-entry episode is explicitly armed.
- Maximum concurrent positions: 1.
- Maximum new submissions for the pilot: 1.
- No averaging down.
- No martingale.
- NEXUS thresholds, RR gates and EV gates are unchanged.
- Ownership, fencing, durable state, private-stream, market-data, CROSS stress,
  protection and reconciliation gates remain fail-closed.
- The episode is durably consumed before the exchange HTTP POST and is not
  automatically restored after an ambiguous/rejected dispatch.
- Any additional attempt requires a new episode id and a new explicit arm.

## Pilot loss envelope

Production configuration target for V1:

- absolute loss ceiling: 0.10 USDT;
- hard maximum risk percentage: 2.00% of freshly confirmed equity;
- effective risk percentage: min(0.10 / fresh_equity, 2.00%).

RiskManagerV3 remains the sizing authority. The absolute ceiling is rechecked
at final sizing and the fresh executable-price risk check remains authoritative
immediately before dispatch.

## Edge status

The current prospective OOS sample is EVIDENCE_FAIL. The approved cohort has
not demonstrated positive aggregate lift at 60m or 240m. Therefore:

- this release is an execution/operational proof, not proof of profitability;
- no RR/EV/NEXUS relaxation is justified by the current sample;
- no automatic promotion or scale-up is permitted from this release;
- position size, submission count or loss budget must not be increased because
  a single pilot trade wins.

## Freeze rule

After this release is merged and deployed, do not add strategy modules, new
research gates, threshold relaxations or sizing doctrines before the first
protected LIVE pilot lifecycle is reviewed. Allowed changes during the freeze
are limited to fixes required for correctness, fail-closed safety, protection,
reconciliation or factual observability.

The first real exchange submission remains a manual operator action: production
may be configured for the episode and budget, but the exact
CONTROLLED_LIVE_REENTRY_ARM token must not be set automatically by deployment.
