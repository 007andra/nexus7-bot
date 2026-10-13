"""BGX Capital — Quant optimizer.

Integrity guarantees:
- Optuna fits only TRAIN.
- VALIDATION is a promotion gate, never the objective.
- TEST is touched only after candidate freeze.
- Only parameters with verified replay effect are optimized.
- HTF context is filtered by real closed-candle timestamps.
- TRAIN/VALIDATION/TEST share the same KuCoin fill/fee/funding model.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import numpy as np

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    OPTUNA_AVAILABLE = True
except ImportError:
    optuna = None
    OPTUNA_AVAILABLE = False

from bot import database as _db
from bot.backtest import _calc_metrics, _ts_ms, fetch_history, run_strategy_public
from bot.execution_model import slippage_rate_for_symbol
from bot.kucoin_execution_model import (
    fetch_actual_taker_fee,
    fetch_public_funding_history,
)
from bot.logger import log

PARAMS_FILE = Path(__file__).parent / "params_optimized.json"
DEFAULT_PARAMS = {"min_score": 65, "min_rr": 2.0}


def load_optimized_params() -> dict:
    """Load a research candidate; this function does not mutate runtime config."""
    try:
        if PARAMS_FILE.exists():
            with open(PARAMS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return dict(data.get("best_params") or DEFAULT_PARAMS)
    except Exception as exc:
        log.warning(f"load_optimized_params (arquivo): {exc}")

    try:
        async def _load_from_db():
            value = await _db.load_key_value("optimizer_params")
            return json.loads(value).get("best_params") if value else None

        loop = asyncio.get_event_loop()
        if not loop.is_running():
            params = loop.run_until_complete(_load_from_db())
            if params:
                return dict(params)
    except Exception as exc:
        log.warning(f"load_optimized_params (DB): {exc}")
    return DEFAULT_PARAMS.copy()


def save_optimized_params(params: dict, metadata: dict | None = None) -> None:
    payload = {
        "best_params": dict(params),
        "metadata": metadata or {},
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": "2.2-kucoin-execution-parity",
        "runtime_applied": False,
    }
    try:
        with open(PARAMS_FILE, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
    except Exception as exc:
        log.warning(f"save_optimized_params (arquivo): {exc}")

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_db.save_key_value("optimizer_params", json.dumps(payload)))
    except Exception as exc:
        log.warning(f"save_optimized_params (DB): {type(exc).__name__}: {exc}")


def _run_strategy_with_params(
    k15, k1h, k4h, params: dict, symbol: str = "", execution_context: dict | None = None
) -> list:
    return run_strategy_public(
        k15,
        k1h,
        k4h,
        min_score=int(params.get("min_score", DEFAULT_PARAMS["min_score"])),
        min_rr=float(params.get("min_rr", DEFAULT_PARAMS["min_rr"])),
        symbol=symbol,
        execution_context=execution_context,
    )


def _sample_params(trial) -> dict:
    return {
        "min_score": trial.suggest_int("min_score", 55, 80),
        "min_rr": trial.suggest_float("min_rr", 1.5, 3.0),
    }


def _objective(
    trial, k15_train, k1h_train, k4h_train, symbol: str = "",
    execution_context: dict | None = None,
) -> float:
    """TRAIN-only objective. No validation/test leakage."""
    params = _sample_params(trial)
    trades = _run_strategy_with_params(
        k15_train, k1h_train, k4h_train, params, symbol, execution_context
    )
    if len(trades) < 20:
        return -999.0
    metrics = _calc_metrics(trades, "optimizer_train")
    sharpe = float(metrics.get("sharpe_ratio", 0) or 0)
    pf = float(metrics.get("profit_factor", 0) or 0)
    expectancy = float(metrics.get("expectancy_pct", 0) or 0)
    max_dd = float(metrics.get("max_drawdown_pct", 0) or 0)
    return sharpe + min(max(pf - 1.0, -1.0), 2.0) * 0.15 + expectancy * 0.02 - max_dd * 0.002


def _split_by_time(k15: list, k1h: list, k4h: list) -> dict:
    """Chronological 60/20/20 split with timestamp-safe shared HTF context."""
    n = len(k15)
    train_end = int(n * 0.60)
    val_end = int(n * 0.80)
    return {
        "train": (k15[:train_end], k1h, k4h),
        "validation": (k15[train_end:val_end], k1h, k4h),
        "test": (k15[val_end:], k1h, k4h),
    }


def _holdout_gate(validation_metrics: dict, test_metrics: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    for label, metrics in (("validation", validation_metrics), ("test", test_metrics)):
        trades = int(metrics.get("total_trades", 0) or 0)
        pf = float(metrics.get("profit_factor", 0) or 0)
        expectancy = float(metrics.get("expectancy_pct", 0) or 0)
        sharpe = float(metrics.get("sharpe_ratio", 0) or 0)
        if trades < 10:
            reasons.append(f"{label}: insufficient trades ({trades}<10)")
        if pf <= 1.0:
            reasons.append(f"{label}: PF {pf:.2f} <= 1.0")
        if expectancy <= 0:
            reasons.append(f"{label}: expectancy {expectancy:.4f}% <= 0")
        if sharpe <= 0:
            reasons.append(f"{label}: Sharpe {sharpe:.3f} <= 0")
    return not reasons, reasons


async def _build_execution_context(client, symbol: str, k15: list) -> tuple[dict, bool]:
    start_ms = _ts_ms(k15[0])
    end_ms = _ts_ms(k15[-1]) + 15 * 60 * 1000
    fee_rate, fee_source = await fetch_actual_taker_fee(client, symbol)
    funding = await fetch_public_funding_history(client, symbol, start_ms, end_ms)
    funding_required = (end_ms - start_ms) > 8 * 60 * 60 * 1000
    complete = bool(funding) or not funding_required
    return {
        "taker_fee_rate": fee_rate,
        "fee_source": fee_source,
        "slippage_rate": slippage_rate_for_symbol(symbol),
        "funding_events": funding,
    }, complete


def optimize_snapshot(k15, k1h, k4h, symbol, n_trials, execution_context):
    """CPU-only research entry point, executed in a credential-free child."""
    t0 = time.time()
    splits = _split_by_time(k15, k1h, k4h)
    k15_train, k1h_train, k4h_train = splits["train"]
    k15_val, k1h_val, k4h_val = splits["validation"]
    k15_test, k1h_test, k4h_test = splits["test"]

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(n_warmup_steps=20),
    )
    study.optimize(
        lambda trial: _objective(
            trial, k15_train, k1h_train, k4h_train, symbol, execution_context
        ),
        n_trials=n_trials,
        show_progress_bar=False,
    )

    best_params = dict(study.best_params)
    train_trades = _run_strategy_with_params(
        k15_train, k1h_train, k4h_train, best_params, symbol, execution_context
    )
    val_trades = _run_strategy_with_params(
        k15_val, k1h_val, k4h_val, best_params, symbol, execution_context
    )
    test_trades = _run_strategy_with_params(
        k15_test, k1h_test, k4h_test, best_params, symbol, execution_context
    )

    train_metrics = _calc_metrics(train_trades, "train") if train_trades else {}
    val_metrics = _calc_metrics(val_trades, "validation") if val_trades else {}
    test_metrics = _calc_metrics(test_trades, "test") if test_trades else {}
    promote, gate_reasons = _holdout_gate(val_metrics, test_metrics)

    metadata = {
        "symbol": symbol,
        "n_trials": n_trials,
        "objective_train": round(float(study.best_value), 6),
        "train_metrics": train_metrics,
        "validation_metrics": val_metrics,
        "test_metrics": test_metrics,
        "promotion_allowed": promote,
        "promotion_block_reasons": gate_reasons,
        "split": "60_train_20_validation_20_test",
        "test_evaluated_after_candidate_freeze": True,
        "runtime_applied": False,
        "execution_model": "KUCOIN_MARKET_PROXY_V1",
        "taker_fee_rate": execution_context["taker_fee_rate"],
        "fee_source": execution_context["fee_source"],
        "slippage_rate": execution_context["slippage_rate"],
        "funding_events_loaded": len(execution_context["funding_events"]),
        "elapsed_s": round(time.time() - t0, 1),
    }

    return {'best_params': best_params, 'metadata': metadata}


async def run_optimization(client, symbol: str = "BTCUSDT", n_trials: int = 300) -> dict:
    if not OPTUNA_AVAILABLE:
        log.error("Optuna não instalado no runtime")
        return {"error": "optuna not installed"}

    log.info(f"🔬 Iniciando otimização sem leakage — {symbol} | {n_trials} trials")
    k15 = await fetch_history(client, symbol, "15", 8640)
    k1h = await fetch_history(client, symbol, "60", 2160)
    k4h = await fetch_history(client, symbol, "240", 540)
    if len(k15) < 500 or len(k1h) < 100 or len(k4h) < 30:
        return {"error": "Dados insuficientes para otimização institucional"}

    execution_context, cost_data_complete = await _build_execution_context(client, symbol, k15)
    if not cost_data_complete:
        log.error(f"[OPTIMIZER] {symbol}: funding history unavailable; promotion blocked")
        return {
            "error": "execution cost data incomplete",
            "promoted": False,
            "runtime_applied": False,
        }

    from bot.research_process import run_snapshot
    result = await run_snapshot(dict(k15=k15, k1h=k1h, k4h=k4h,
        symbol=symbol, n_trials=n_trials, execution_context=execution_context))
    best_params, metadata = result['best_params'], result['metadata']
    promote = metadata['promotion_allowed']
    gate_reasons = metadata['promotion_block_reasons']
    val_metrics, test_metrics = metadata['validation_metrics'], metadata['test_metrics']

    if promote:
        save_optimized_params(best_params, metadata)
        log.info(
            f"✅ Research candidate passed holdouts {symbol}: "
            f"VAL PF={val_metrics.get('profit_factor')} TEST PF={test_metrics.get('profit_factor')}"
        )
    else:
        log.warning(f"🚫 Research candidate blocked {symbol}: " + "; ".join(gate_reasons[:8]))
    return {"best_params": best_params, "metadata": metadata, "promoted": promote}


_optimization_lock = asyncio.Lock()


async def weekly_optimization_loop(client):
    while True:
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            if now.weekday() == 0 and now.hour == 2 and now.minute < 5:
                if not _optimization_lock.locked():
                    async with _optimization_lock:
                        for sym in ["BTCUSDT", "ETHUSDT"]:
                            try:
                                await run_optimization(client, sym, n_trials=500)
                            except Exception as exc:
                                log.error(f"weekly_optimization {sym}: {exc}")
                await asyncio.sleep(3600)
        except Exception as exc:
            log.error(f"weekly_optimization_loop: {exc}")
        await asyncio.sleep(60)


def test_parameter_robustness(
    k15: list,
    k1h: list,
    k4h: list,
    best_params: dict,
    perturbation: float = 0.10,
    symbol: str = "",
    execution_context: dict | None = None,
) -> dict:
    base_trades = _run_strategy_with_params(
        k15, k1h, k4h, best_params, symbol, execution_context
    )
    base_metrics = _calc_metrics(base_trades) if base_trades else {}
    base_sharpe = float(base_metrics.get("sharpe_ratio", 0) or 0)
    base_pf = float(base_metrics.get("profit_factor", 0) or 0)
    if not base_trades or base_sharpe <= 0:
        return {"robust": False, "reason": "Base sem trades ou Sharpe não positivo"}

    results = []
    for param in ("min_score", "min_rr"):
        if param not in best_params:
            continue
        base_val = best_params[param]
        for factor in (1 + perturbation, 1 - perturbation):
            perturbed = dict(best_params)
            perturbed[param] = int(base_val * factor) if param == "min_score" else round(float(base_val) * factor, 4)
            trades = _run_strategy_with_params(
                k15, k1h, k4h, perturbed, symbol, execution_context
            )
            metrics = _calc_metrics(trades) if trades else {}
            sharpe = float(metrics.get("sharpe_ratio", 0) or 0)
            pf = float(metrics.get("profit_factor", 0) or 0)
            results.append({
                "param": param,
                "factor": factor,
                "value": perturbed[param],
                "sharpe": round(sharpe, 3),
                "pf": round(pf, 2),
                "sharpe_degradation_pct": round((base_sharpe - sharpe) / max(abs(base_sharpe), 0.01) * 100, 1),
                "pf_degradation_pct": round((base_pf - pf) / max(abs(base_pf), 0.01) * 100, 1),
            })

    avg_deg = float(np.mean([abs(r["sharpe_degradation_pct"]) for r in results])) if results else 0.0
    return {
        "robust": avg_deg < 30,
        "avg_degradation_pct": round(avg_deg, 1),
        "details": results,
        "verdict": "ROBUSTO" if avg_deg < 30 else "OVERFITADO",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--trials", type=int, default=300)
    parser.parse_args()
    raise SystemExit(
        "Use run_optimization(client, ...) from the BGX runtime so authenticated market data is supplied safely."
    )
