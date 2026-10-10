# Railway production promotion contract

> **Divergence observed 2026-09-26 (read-only check of the production service config):**
> source branch `migration/binance-usdm`, `checkSuites=false`, builder `RAILPACK`, and no
> pre-deploy command. So the procedure below (main + attestation + `bot.ci_deploy_gate`
> pre-deploy + Dockerfile) is **not** what production currently enforces. Deploys from
> `migration/binance-usdm` do not wait for CI. Operator action is required to restore the
> gate; nothing in the repository can change it.


## Invariant

Normal pushes and merges to `main` MUST NOT trigger a `nexus7-bot`
production deployment.

The Railway service watches exactly one path:

```
/.railway/promote.txt
```

That marker is intentionally absent from normal hardening PRs. A production
promotion is a separate, explicit, human-reviewed PR.

## Promotion sequence

1. Merge the intended release changes to `main`.
2. Wait for **Quality Check** on the exact current main SHA.
3. Wait for **Supply Chain Security** on that same SHA.
4. Verify **Publish CI Attestation** created
   `ci-attestations/passed/<main_sha>.txt` containing that exact SHA.
5. Create a new branch from that exact, still-current `main` SHA.
6. Change **only** `.railway/promote.txt` to contain the attested main SHA.
7. Open a dedicated promotion PR.
8. Verify the PR diff contains only `.railway/promote.txt`.
9. Merge the promotion PR.
10. Railway sees the watched-path change and starts the intentional deployment.
11. Railway still executes `python -m bot.ci_deploy_gate` before runtime deploy.
12. Verify production SHA and `/ready` after deployment.

## Promotion preconditions

A promotion PR MUST NOT be created or merged unless:

- target SHA is the current `main` HEAD;
- Quality is successful for that exact SHA;
- Security is successful for that exact SHA;
- exact CI attestation exists;
- no non-marker file is present in the promotion PR.

## Failure semantics

- A normal main merge: **Railway deployment SKIPPED**.
- Missing/failed Quality or Security: **no promotion**.
- Missing exact attestation: **no promotion**.
- Stale target SHA: **no promotion**.
- Promotion PR containing code changes: **do not merge**.
- Even after explicit promotion, missing deployment-SHA attestation causes
  `ci_deploy_gate` to fail closed in Railway pre-deploy.

## Why promotion is intentionally manual

The critical control is not convenience automation; it is separation between
code integration and production promotion. A small review-only marker PR is
easy to inspect, leaves an immutable GitHub audit trail, and avoids coupling
release authority to another workflow that could fail or accidentally broaden
its permissions.

## Non-scope

This mechanism does not change strategy, NEXUS, sizing, leverage, CROSS,
SL/TP, order routing, execution ownership, database state or trading policy.
