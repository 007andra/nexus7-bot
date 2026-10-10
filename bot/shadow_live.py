"""Read-only SHADOW LIVE execution path for NEXUS-7.

Exercises real market/account reads, NEXUS AI, RiskManagerV3 stop-risk sizing,
pre-trade scoring, microstructure, liquidation and final exposure checks while
guaranteeing execution_effect=NONE.
"""
import asyncio
import os
import time

from bot.config import cfg
from bot.logger import log
from bot import score as scoring
from bot import liquidation as liq
from bot import shadow_balance_semantics as balance_semantics
from bot.exchange import TAKER_FEE
from bot.quantity import validate_base_quantity
from bot.nexus_types import decision_validation_error
from bot.account_capital_reader import read_account_capital
from bot.risk_manager_v3 import RiskManagerV3
from bot.pre_dispatch_guard import MicrostructureLimits, evaluate_microstructure
from bot.core_execution_risk import final_read_only_dispatch_recheck


def _shadow_ai_reason(nx_dec, validation_reason, approved):
    if approved:
        return "approved"
    if validation_reason:
        return str(validation_reason)
    if nx_dec is None:
        return "no_decision"
    reasoning = getattr(nx_dec, "reasoning", None)
    if isinstance(reasoning, list):
        for item in reasoning:
            if isinstance(item, str) and item.strip():
                return item.strip()[:240]
    warnings = getattr(nx_dec, "warnings", None)
    if isinstance(warnings, list):
        for item in warnings:
            if isinstance(item, str) and item.strip():
                return item.strip()[:240]
    return "nexus_ai_veto"


def _gate_block(symbol, stage, reason, **fields):
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    log.info(
        "[SHADOW_GATE] symbol=%s stage=%s result=BLOCK reason=%s%s execution_effect=NONE",
        symbol, stage, reason, f" {extra}" if extra else "",
    )


def _gate_pass(symbol, stage, **fields):
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    log.info(
        "[SHADOW_GATE] symbol=%s stage=%s result=PASS%s execution_effect=NONE",
        symbol, stage, f" {extra}" if extra else "",
    )


def _shadow_risk_v3(engine) -> RiskManagerV3:
    risk = getattr(engine, "_shadow_risk_v3", None)
    if not isinstance(risk, RiskManagerV3):
        risk = RiskManagerV3()
        engine._shadow_risk_v3 = risk
    return risk


def _normalize_orderbook_base_units(engine, symbol, raw):
    """Convert KuCoin contract depth into base-asset units for execution gates."""
    if not isinstance(raw, dict):
        return None
    info = engine.instruments.get(symbol, {}) or {}
    try:
        multiplier = float(info.get("multiplier", 0) or 0)
    except (TypeError, ValueError):
        multiplier = 0.0
    if multiplier <= 0:
        return None

    def levels(key):
        out = []
        for row in raw.get(key, []) or []:
            try:
                if isinstance(row, dict):
                    price = float(row.get("price", 0) or 0)
                    size_contracts = float(row.get("size", row.get("qty", 0)) or 0)
                else:
                    price = float(row[0])
                    size_contracts = float(row[1])
                if price > 0 and size_contracts >= 0:
                    out.append([price, size_contracts * multiplier])
            except (TypeError, ValueError, IndexError):
                continue
        return out

    return {"bids": levels("b"), "asks": levels("a")}


async def connect_readonly(engine) -> bool:
    try:
        ping_ok = await engine.client.ping()
        if not ping_ok:
            log.warning("[SHADOW_LIVE] exchange ping failed; continuing with REST read-only checks")
        state = await balance_semantics.refresh_shadow_risk(engine)
        if state["equity"] < 0 or state["available"] < 0:
            engine.connected = False
            engine.active = False
            return False
        # Prime explicit V3 capital semantics from the authenticated account overview.
        capital_snapshot = await read_account_capital(engine.client)
        _shadow_risk_v3(engine).update_capital(capital_snapshot.capital)
        await engine.client.load_instruments()
        engine.instruments = engine.client.get_instruments()
        await engine._filter_viable_symbols()
        if not engine.viable_symbols:
            engine.connected = False
            engine.active = False
            return False
        try:
            await asyncio.wait_for(
                engine.client.start_websocket(
                    engine.viable_symbols[:30], intervals=["15", "60", "240"]
                ),
                timeout=10,
            )
        except asyncio.TimeoutError:
            log.warning("[SHADOW_LIVE] public websocket startup timeout; REST fallback remains available")
        except Exception as exc:
            log.warning("[SHADOW_LIVE] public websocket unavailable: %s", type(exc).__name__)
        engine.connected = True
        engine.active = True
        log.warning(
            "[SHADOW_LIVE] READY balance_read=true capital_v3=true viable_symbols=%s "
            "private_ws=false execution_effect=NONE",
            len(engine.viable_symbols),
        )
        return True
    except Exception as exc:
        engine.connected = False
        engine.active = False
        log.error("[SHADOW_LIVE] connect failed: %s", type(exc).__name__)
        return False


async def evaluate_candidate(engine, sig):
    """Evaluate a real-market candidate without granting execution authority."""
    try:
        if sig.symbol not in engine.viable_symbols:
            _gate_block(sig.symbol, "VIABILITY", "not_viable")
            return None
        _gate_pass(sig.symbol, "VIABILITY")

        if getattr(engine, "_durable_state_enforced", False):
            from bot import durable_execution as durable
            from bot import shadow_state_isolation as isolation
            if not durable.can_open(engine):
                hard = isolation.durable_analysis_blocker(engine)
                if hard:
                    _gate_block(sig.symbol, "DURABLE_STATE", "hard_fault", faults=",".join(hard))
                    return None
                log.warning(
                    "[SHADOW_STATE_ISOLATION] execution state divergent but hypothetical "
                    "analysis continues; execution_effect=NONE"
                )
        _gate_pass(sig.symbol, "DURABLE_STATE")

        from bot import engine as engine_mod
        nx_dec = None
        reason = "ai_disabled"
        decision_source = "validation_failure"
        if getattr(engine_mod, "_NEXUS_ENABLED", True):
            try:
                nx_dec = await asyncio.wait_for(
                    engine._nexus_validate(sig),
                    timeout=float(getattr(engine_mod, "_NEXUS_TIMEOUT_S", 10.0)),
                )
                reason = decision_validation_error(
                    nx_dec, sig.symbol, sig.direction, sig.entry, sig.sl, sig.tp
                )
                decision_source = "validation_failure" if reason else "nexus_ai"
            except asyncio.TimeoutError:
                decision_source, reason = "timeout", "ai_timeout"
            except Exception as exc:
                decision_source, reason = "exception", type(exc).__name__
        approved = bool(reason is None and nx_dec is not None and nx_dec.execution_allowed is True)
        observed_reason = _shadow_ai_reason(nx_dec, reason, approved)
        log.info(
            "[SHADOW_AI] symbol=%s side=%s decision=%s approved=%s source=%s "
            "reason=%s execution_effect=NONE",
            sig.symbol, sig.direction, "APPROVE" if approved else "REJECT",
            approved, decision_source, observed_reason,
        )
        if not approved:
            _gate_block(sig.symbol, "NEXUS_AI", observed_reason, source=decision_source)
            return None
        _gate_pass(sig.symbol, "NEXUS_AI", source=decision_source)

        # Keep legacy shadow balance telemetry, but make V3 the authority for
        # risk budget, drawdown and sizing semantics in this validation path.
        legacy_state = await balance_semantics.refresh_shadow_risk(engine)
        capital_snapshot = await read_account_capital(engine.client)
        risk_v3 = _shadow_risk_v3(engine)
        risk_snapshot = risk_v3.update_capital(capital_snapshot.capital)
        capital = risk_snapshot.capital
        if capital.equity <= 0:
            _gate_block(sig.symbol, "CAPITAL_HEALTH", "non_positive_equity", equity=capital.equity)
            return None
        if capital.available_collateral <= 0:
            _gate_block(
                sig.symbol, "CAPITAL_HEALTH", "non_positive_available_collateral",
                equity=f"{capital.equity:.4f}", available=f"{capital.available_collateral:.4f}",
            )
            return None
        if risk_snapshot.drawdown >= cfg.MAX_DRAWDOWN:
            _gate_block(
                sig.symbol, "CAPITAL_HEALTH", "drawdown_limit",
                drawdown=f"{risk_snapshot.drawdown:.6f}", maximum=f"{cfg.MAX_DRAWDOWN:.6f}",
            )
            return None
        _gate_pass(
            sig.symbol, "CAPITAL_HEALTH",
            equity=f"{capital.equity:.4f}", available=f"{capital.available_collateral:.4f}",
            position_margin=f"{capital.position_margin:.4f}",
            order_margin=f"{capital.order_margin:.4f}",
            drawdown=f"{risk_snapshot.drawdown:.6f}", legacy_available=f"{legacy_state['available']:.4f}",
        )

        expected_slippage = float(os.environ.get("NEXUS_EXPECTED_SLIPPAGE_PCT", "0.001"))
        sizing = risk_v3.size_for_stop(
            symbol=sig.symbol,
            entry=float(sig.entry),
            stop=float(sig.sl),
            instruments=engine.instruments,
            risk_pct=float(engine._effective_risk_pct()),
            leverage=float(cfg.LEVERAGE),
            fee_rate_per_side=float(TAKER_FEE),
            expected_slippage_pct=expected_slippage,
        )
        qty = float(sizing.qty)
        if qty <= 0:
            _gate_block(
                sig.symbol, "SIZING_V3", "non_positive_qty",
                binding=sizing.binding_constraint,
                risk_budget=f"{sizing.risk_budget:.6f}",
                stop_distance_pct=f"{sizing.stop_distance_pct:.6f}",
            )
            return None
        _gate_pass(
            sig.symbol, "SIZING_V3",
            qty=f"{qty:.12g}", risk_budget=f"{sizing.risk_budget:.6f}",
            projected_stop_loss=f"{sizing.projected_stop_loss:.6f}",
            stop_distance_pct=f"{sizing.stop_distance_pct:.6f}",
            required_margin=f"{sizing.required_margin:.6f}",
            binding=sizing.binding_constraint,
        )

        # Required margin is already capped by V3 against available collateral.
        if sizing.required_margin > capital.available_collateral:
            _gate_block(
                sig.symbol, "COLLATERAL", "insufficient_available_collateral",
                required=f"{sizing.required_margin:.4f}",
                available=f"{capital.available_collateral:.4f}",
            )
            return None
        _gate_pass(
            sig.symbol, "COLLATERAL",
            required=f"{sizing.required_margin:.4f}",
            available=f"{capital.available_collateral:.4f}",
        )

        # Real top-of-book and visible depth are mandatory for the new execution-quality gate.
        ticker = engine.client.get_cached_ticker(sig.symbol) or {}
        if not ticker or float(ticker.get("bid", 0) or 0) <= 0 or float(ticker.get("ask", 0) or 0) <= 0:
            ticker = await engine.client.get_ticker(sig.symbol)
        raw_ob = await engine.client.get_orderbook(sig.symbol, depth=20)
        orderbook = _normalize_orderbook_base_units(engine, sig.symbol, raw_ob)
        limits = MicrostructureLimits(
            max_spread_bps=float(os.environ.get("NEXUS_MAX_SPREAD_BPS", "12")),
            max_signal_drift_bps=float(os.environ.get("NEXUS_MAX_SIGNAL_DRIFT_BPS", "20")),
            min_depth_multiple=float(os.environ.get("NEXUS_MIN_DEPTH_MULTIPLE", "3")),
        )
        micro = evaluate_microstructure(
            signal_entry=float(sig.entry),
            side="BUY" if sig.direction == "LONG" else "SELL",
            qty=qty,
            ticker=ticker,
            orderbook=orderbook,
            limits=limits,
        )
        if not micro.allowed:
            _gate_block(
                sig.symbol, "MICROSTRUCTURE", "+".join(micro.blockers) or "blocked",
                spread_bps=f"{micro.metrics.get('spread_bps', 0):.4f}",
                drift_bps=f"{micro.metrics.get('signal_drift_bps', 0):.4f}",
                depth_multiple=f"{micro.metrics.get('depth_multiple', 0):.4f}",
            )
            return None
        _gate_pass(
            sig.symbol, "MICROSTRUCTURE",
            spread_bps=f"{micro.metrics.get('spread_bps', 0):.4f}",
            drift_bps=f"{micro.metrics.get('signal_drift_bps', 0):.4f}",
            depth_multiple=f"{micro.metrics.get('depth_multiple', 0):.4f}",
        )

        kl = engine.client.get_cached_klines(sig.symbol, "15", 50)
        if len(kl) < 20:
            try:
                kl = await engine.client.get_klines(sig.symbol, "15", 50)
            except Exception as exc:
                _gate_block(sig.symbol, "PRETRADE_DATA", "kline_fetch_error", error=type(exc).__name__)
                kl = []
        if len(kl) <= 20:
            _gate_block(sig.symbol, "PRETRADE_DATA", "insufficient_klines", count=len(kl), minimum=21)
            return None
        _gate_pass(sig.symbol, "PRETRADE_DATA", count=len(kl))

        c = [float(k.get("c", sig.entry)) for k in kl]
        h = [float(k.get("h", sig.entry)) for k in kl]
        l = [float(k.get("l", sig.entry)) for k in kl]
        v = [float(k.get("v", 1000.0)) for k in kl]
        pre_score = await scoring.calculate(sig.symbol, sig.direction, c, h, l, v, engine.client)
        if not pre_score.get("aprovado"):
            _gate_block(
                sig.symbol, "PRETRADE_SCORE", "score_below_gate",
                score=pre_score.get("total"), minimum=scoring.MIN_SCORE,
            )
            return None
        _gate_pass(sig.symbol, "PRETRADE_SCORE", score=pre_score.get("total"), minimum=scoring.MIN_SCORE)

        try:
            validate_base_quantity(qty, engine.instruments.get(sig.symbol, {}), sig.entry)
        except Exception as exc:
            _gate_block(sig.symbol, "QUANTITY_VALIDATION", type(exc).__name__)
            return None
        _gate_pass(sig.symbol, "QUANTITY_VALIDATION", qty=f"{qty:.12g}")

        if sig.sl <= 0 or sig.tp <= 0:
            _gate_block(sig.symbol, "PROTECTIVE_LEVELS", "non_positive_sl_or_tp", sl=sig.sl, tp=sig.tp)
            return None
        if sig.direction == "LONG" and (sig.sl >= sig.entry or sig.tp <= sig.entry):
            _gate_block(sig.symbol, "PROTECTIVE_LEVELS", "invalid_long_geometry", entry=sig.entry, sl=sig.sl, tp=sig.tp)
            return None
        if sig.direction == "SHORT" and (sig.sl <= sig.entry or sig.tp >= sig.entry):
            _gate_block(sig.symbol, "PROTECTIVE_LEVELS", "invalid_short_geometry", entry=sig.entry, sl=sig.sl, tp=sig.tp)
            return None
        _gate_pass(sig.symbol, "PROTECTIVE_LEVELS")

        liq_result = liq.analyze(
            entry=sig.entry, stop=sig.sl, leverage=cfg.LEVERAGE,
            is_long=(sig.direction == "LONG"), symbol=sig.symbol, n_open_positions=1,
        )
        if not liq_result.stop_effective:
            _gate_block(sig.symbol, "LIQUIDATION_GUARD", "stop_not_effective")
            return None
        _gate_pass(sig.symbol, "LIQUIDATION_GUARD")

        # Re-read positions + active orders at the final boundary. Read errors and
        # any exposure are fail-closed; this call performs no mutation.
        final_exposure = await final_read_only_dispatch_recheck(engine.client, sig.symbol)
        if not final_exposure.allowed:
            _gate_block(
                sig.symbol, "FINAL_ACCOUNT_EXPOSURE",
                "+".join(final_exposure.blockers) or "blocked",
            )
            return None
        _gate_pass(sig.symbol, "FINAL_ACCOUNT_EXPOSURE")

        try:
            pilot_blockers = engine.pilot.evaluate(engine, engine.client, sig.symbol, nx_dec)
        except Exception as exc:
            pilot_blockers = [f"PILOT_EVAL_ERROR:{type(exc).__name__}"]
        _gate_pass(sig.symbol, "PILOT_OBSERVABILITY", blockers=len(pilot_blockers))

        record = {
            "symbol": sig.symbol,
            "side": sig.direction,
            "entry": float(sig.entry),
            "sl": float(sig.sl),
            "tp": float(sig.tp),
            "qty": qty,
            "notional": float(qty * sig.entry),
            "risk_budget": float(sizing.risk_budget),
            "projected_stop_loss": float(sizing.projected_stop_loss),
            "required_margin": float(sizing.required_margin),
            "sizing_binding": sizing.binding_constraint,
            "spread_bps": float(micro.metrics.get("spread_bps", 0.0)),
            "signal_drift_bps": float(micro.metrics.get("signal_drift_bps", 0.0)),
            "depth_multiple": float(micro.metrics.get("depth_multiple", 0.0)),
            "score": int(sig.score),
            "pretrade": int(pre_score.get("total", 0)),
            "ai_approved": True,
            "pilot_blockers": list(pilot_blockers),
            "ts": int(time.time()),
            "execution_effect": "NONE",
        }
        log.warning(
            "[SHADOW_LIVE] WOULD_SUBMIT symbol=%s side=%s entry=%.8f sl=%.8f tp=%.8f "
            "qty=%.12g notional=%.4f risk_budget=%.4f projected_stop_loss=%.4f "
            "required_margin=%.4f sizing_binding=%s spread_bps=%.3f drift_bps=%.3f "
            "depth_multiple=%.3f score=%s pretrade=%s pilot_blockers=%s execution_effect=NONE",
            sig.symbol, sig.direction, sig.entry, sig.sl, sig.tp,
            qty, qty * sig.entry, sizing.risk_budget, sizing.projected_stop_loss,
            sizing.required_margin, sizing.binding_constraint,
            micro.metrics.get("spread_bps", 0.0),
            micro.metrics.get("signal_drift_bps", 0.0),
            micro.metrics.get("depth_multiple", 0.0),
            sig.score, pre_score.get("total", 0), len(pilot_blockers),
        )
        return record
    except Exception as exc:
        log.error(
            "[SHADOW_LIVE] %s evaluation failed: %s",
            getattr(sig, "symbol", "?"), type(exc).__name__,
        )
        return None
