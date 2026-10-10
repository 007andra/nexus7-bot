# NEXUS-7 / BGX Capital — trading bot

Automated trading for **Binance USD-M perpetual futures** (the production venue).
KuCoin Futures remains selectable (`EXCHANGE=kucoin`) for compatibility.

> Earlier versions of this README described Bybit. That text was obsolete and
> has been replaced. For the migration history see `KUCOIN_MIGRATION.md` (historical).

## Runtime in one paragraph

`main_hardened:app` (FastAPI) boots through `sitecustomize` → `bot.runtime_bootstrap`,
which installs the hardening stack and ends with `bot.runtime_contract_guard` (startup
is refused if the execution chain drifts).

The trading loop scans the configured universe (`cfg.SYMBOLS`, 25 symbols). Each
symbol goes through these steps:
1. `strategy.Analyzer` (4H/1H/15M confluence score, closed candles only).
2. Pullback confirmation.
3. **NEXUS**, a rule-based heuristic ensemble with EV and net R:R gates. It is
   *not* a trained ML model.
4. RiskManagerV3 stop-risk sizing capped by the operator margin policy.
5. Pre-dispatch gates: drawdown, liquidation/cross stress, microstructure and pilot.
6. Fenced, idempotent Binance order dispatch.
7. Mandatory SL/TP protection and durable reconciliation.

## Where the truth lives

- `docs/audit/CANONICAL_AUTHORITIES.md`: one owner per concept (equity, drawdown, sizing, costs, release, ...).
- `docs/audit/FULL_CODEBASE_AUDIT_2026-09-26.md`: latest full audit, with findings and their status.
- `docs/audit/FULL_CODEBASE_INVENTORY_2026-09-26.md`: every versioned file with its role and reachability.

## Safety model (summary)

- LIVE requires the explicit operator tokens checked by `bot/pilot_release_control.py`, plus `BINANCE_LIVE_MIGRATION_READY=true`. Anything missing keeps the validation lock (read-only SHADOW).
- New entries fail closed on any missing or unreadable input. Reduce-only/closePosition exits stay available.
- Leverage, risk %, drawdown, daily stop, score and R:R thresholds come from environment variables. `bot/config.py` holds the conservative defaults.

## Development

```
pip install -r requirements.txt
python -m tests.run_offline        # full offline suite (no network)
python -m bot.selfcheck
python -m bot.release_proof
```

CI (`.github/workflows/quality.yml`) runs the same gates on PRs into `main` and `migration/binance-usdm`.
