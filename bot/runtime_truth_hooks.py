"""Runtime truth instrumentation hooks.

All hooks are installed only when BGX_RUNTIME_TRUTH_ENABLED=true. They return
production results unchanged and perform no exchange mutations. Transport
capture observes aiohttp/websockets objects without storing connection tokens.
"""
from __future__ import annotations

import contextvars
import copy
import time

from bot import runtime_truth as truth

_SIGNAL_EVALUATIONS: dict[int, tuple[str, str]] = {}
_ACTIVE_CLIENT = contextvars.ContextVar("bgx_truth_active_client", default=None)
_LAST_WS_EVENT_ID = contextvars.ContextVar("bgx_truth_last_ws_event_id", default=None)
_LAST_REST_EVENT_ID = contextvars.ContextVar("bgx_truth_last_rest_event_id", default=None)
_PREVIOUS_WS_SESSION = None


def _remember_signal(result):
    if result is None:
        return
    evaluation_id, scope = truth.current_evaluation()
    if evaluation_id:
        _SIGNAL_EVALUATIONS[id(result)] = (evaluation_id, scope or "POLICY_A_STRATEGY")
        if len(_SIGNAL_EVALUATIONS) > 10000:
            for key in list(_SIGNAL_EVALUATIONS)[:1000]:
                _SIGNAL_EVALUATIONS.pop(key, None)


def _stage_wrapper(Analyzer, flag: str, stage: str, authority: str):
    if not truth.enabled() or getattr(Analyzer, flag, False):
        return
    original = Analyzer.analyze_mtf

    def wrapped(self, symbol, k15, k1h, k4h, *args, **kwargs):
        from bot.hard_gate_shadow_context import active
        if active():
            return original(self, symbol, k15, k1h, k4h, *args, **kwargs)
        result = original(self, symbol, k15, k1h, k4h, *args, **kwargs)
        truth.emit_stage_result(stage, authority, result, symbol=symbol)
        _remember_signal(result)
        return result

    Analyzer.analyze_mtf = wrapped
    setattr(Analyzer, flag, True)


def install_canonical_stage(Analyzer):
    _stage_wrapper(Analyzer, "_runtime_truth_canonical_stage", "CANONICAL_ANALYZER_RESULT", "STRATEGY_SIGNAL")


def install_adaptive_stage(Analyzer):
    _stage_wrapper(Analyzer, "_runtime_truth_adaptive_stage", "ADAPTIVE_MTF_RESULT", "STRATEGY_SIGNAL")


def install_pullback_stage(Analyzer):
    _stage_wrapper(Analyzer, "_runtime_truth_pullback_stage", "PULLBACK_CONFIRMATION_RESULT", "POST_CONFIRMATION_SIGNAL")


def install_analysis_authority_inner(Analyzer):
    """Install before market_data_integrity so it receives prepared series."""
    if not truth.enabled() or getattr(Analyzer, "_runtime_truth_analysis_inner", False):
        return
    original = Analyzer.analyze_mtf

    def wrapped(self, symbol, k15, k1h, k4h, *args, **kwargs):
        from bot.hard_gate_shadow_context import active
        if active():
            return original(self, symbol, k15, k1h, k4h, *args, **kwargs)
        meta = truth.analysis_cache_meta()
        raw = meta.get("_raw_inputs", {"15": k15, "60": k1h, "240": k4h})
        truth.capture_analysis_input(
            symbol,
            {"15": list(raw.get("15", [])), "60": list(raw.get("60", [])), "240": list(raw.get("240", []))},
            {"15": list(k15), "60": list(k1h), "240": list(k4h)},
            int(time.time() * 1000),
        )
        return original(self, symbol, k15, k1h, k4h, *args, **kwargs)

    Analyzer.analyze_mtf = wrapped
    Analyzer._runtime_truth_analysis_inner = True


def install_marketdata_outer(Analyzer, TradingEngine):
    if not truth.enabled() or getattr(Analyzer, "_runtime_truth_marketdata_outer", False):
        return
    original = Analyzer.analyze_mtf

    def wrapped(self, symbol, k15, k1h, k4h, *args, **kwargs):
        from bot.hard_gate_shadow_context import active
        if active():
            return original(self, symbol, k15, k1h, k4h, *args, **kwargs)
        evaluation_id = truth.new_evaluation_id("POLICY_A_STRATEGY", symbol)
        client = _ACTIVE_CLIENT.get()
        raw_inputs = {"15": list(k15), "60": list(k1h), "240": list(k4h)}
        meta = truth.snapshot_cache_meta(client, symbol, raw_inputs) if client is not None else {}
        meta["_raw_inputs"] = raw_inputs
        with truth.evaluation_context(evaluation_id, "POLICY_A_STRATEGY", meta):
            result = original(self, symbol, k15, k1h, k4h, *args, **kwargs)
            _remember_signal(result)
            return result

    Analyzer.analyze_mtf = wrapped
    Analyzer._runtime_truth_marketdata_outer = True

    original_scan = TradingEngine._scan_all_and_enter

    async def scan_with_truth(self, *args, **kwargs):
        client_token = _ACTIVE_CLIENT.set(self.client)
        try:
            with truth.rest_purpose("REST_FALLBACK"):
                return await original_scan(self, *args, **kwargs)
        finally:
            _ACTIVE_CLIENT.reset(client_token)

    TradingEngine._scan_all_and_enter = scan_with_truth


def install_engine_and_downstream(TradingEngine, KuCoinClient, scoring, nexus_ai):
    if not truth.enabled() or getattr(TradingEngine, "_runtime_truth_downstream_installed", False):
        return
    from bot import runtime_truth_exporter

    original_run = TradingEngine.run

    async def run_with_truth(self, *args, **kwargs):
        runtime_truth_exporter.start()
        try:
            return await original_run(self, *args, **kwargs)
        finally:
            await runtime_truth_exporter.stop()

    TradingEngine.run = run_with_truth

    original_open = TradingEngine._open

    async def open_with_truth(self, sig, *args, **kwargs):
        evaluation_id, scope = _SIGNAL_EVALUATIONS.get(
            id(sig),
            (truth.new_evaluation_id("ENGINE_CANDIDATE", getattr(sig, "symbol", None)), "POLICY_A_STRATEGY"),
        )
        with truth.evaluation_context(evaluation_id, scope, {}):
            truth.emit_stage_result(
                "ENGINE_CANDIDATE_RESULT", "ENGINE_CANDIDATE", sig,
                symbol=getattr(sig, "symbol", None),
            )
            return await original_open(self, sig, *args, **kwargs)

    TradingEngine._open = open_with_truth

    original_nexus_validate = TradingEngine._nexus_validate

    async def nexus_validate_with_truth(self, sig, *args, **kwargs):
        evaluation_id = truth.new_evaluation_id("NEXUS", getattr(sig, "symbol", None))
        client_token = _ACTIVE_CLIENT.set(self.client)
        try:
            with truth.evaluation_context(evaluation_id, "NEXUS", {}), truth.rest_purpose("REST_FALLBACK"):
                result = await original_nexus_validate(self, sig, *args, **kwargs)
                allowed = bool(getattr(result, "execution_allowed", False)) if result is not None else False
                truth.emit("MARKET_ANALYSIS_RESULT", symbol=getattr(sig, "symbol", None), payload={
                    "evaluation_id": evaluation_id,
                    "evaluation_scope": "NEXUS",
                    "stage": "NEXUS_RESULT",
                    "authority": "NEXUS_APPROVED" if allowed else "NEXUS_REJECTED",
                    "execution_allowed": allowed,
                    "decision": getattr(result, "decision", None),
                    "confidence": getattr(result, "confidence", None),
                    "setup_quality": getattr(result, "setup_quality", None),
                })
                return result
        finally:
            _ACTIVE_CLIENT.reset(client_token)

    TradingEngine._nexus_validate = nexus_validate_with_truth

    original_decide = nexus_ai.decide

    def decide_with_truth(*args, **kwargs):
        symbol = kwargs.get("symbol") or (args[0] if args else None)
        evaluation_id, scope = truth.current_evaluation()
        if scope == "NEXUS":
            tfs = {}
            for tf, name in (("15", "k15"), ("60", "k1h"), ("240", "k4h")):
                rows = list(kwargs.get(name) or [])
                tfs[tf] = {
                    "NEXUS_RAW_INPUT_HASH": truth.cache_data_hash(rows),
                    "row_count": len(rows),
                    "last_timestamp": rows[-1].get("ts") if rows else None,
                }
            truth.emit("MARKET_ANALYSIS_INPUT", symbol=symbol, payload={
                "evaluation_id": evaluation_id,
                "evaluation_scope": "NEXUS",
                "timeframes": tfs,
            })
        return original_decide(*args, **kwargs)

    nexus_ai.decide = decide_with_truth

    original_calculate = scoring.calculate

    async def calculate_with_truth(*args, **kwargs):
        result = await original_calculate(*args, **kwargs)
        evaluation_id, scope = truth.current_evaluation()
        symbol = args[0] if args else kwargs.get("symbol")
        if evaluation_id:
            approved = bool(result.get("aprovado", False)) if isinstance(result, dict) else False
            truth.emit("MARKET_ANALYSIS_RESULT", symbol=symbol, payload={
                "evaluation_id": evaluation_id,
                "evaluation_scope": scope,
                "stage": "RISK_PRETRADE_RESULT",
                "authority": "PRETRADE_APPROVED" if approved else "PRETRADE_REJECTED",
                "result": copy.deepcopy(result) if isinstance(result, dict) else str(result),
            })
        return result

    scoring.calculate = calculate_with_truth

    original_place_order = KuCoinClient.place_order

    async def place_order_with_truth(self, *args, **kwargs):
        result = await original_place_order(self, *args, **kwargs)
        evaluation_id, scope = truth.current_evaluation()
        symbol = kwargs.get("symbol") or (args[0] if args else None)
        if evaluation_id:
            dispatched = bool(isinstance(result, dict) and result.get("orderId"))
            truth.emit("MARKET_ANALYSIS_RESULT", symbol=symbol, payload={
                "evaluation_id": evaluation_id,
                "evaluation_scope": scope,
                "stage": "EXCHANGE_DISPATCH_RESULT",
                "authority": "EXCHANGE_DISPATCHED" if dispatched else "NOT_DISPATCHED",
                "reduce_only": bool(kwargs.get("reduce_only", False)),
                "order_id_present": dispatched,
            })
        return result

    KuCoinClient.place_order = place_order_with_truth

    original_wait = KuCoinClient.wait_for_fill

    async def wait_with_truth(self, order_id, *args, **kwargs):
        result = await original_wait(self, order_id, *args, **kwargs)
        evaluation_id, scope = truth.current_evaluation()
        if evaluation_id:
            filled = bool(result.get("filled", False)) if isinstance(result, dict) else False
            truth.emit("MARKET_ANALYSIS_RESULT", payload={
                "evaluation_id": evaluation_id,
                "evaluation_scope": scope,
                "stage": "FILL_RESULT",
                "authority": "FILLED" if filled else "NOT_FILLED",
                "filled": filled,
                "timed_out": result.get("timed_out") if isinstance(result, dict) else None,
            })
        return result

    KuCoinClient.wait_for_fill = wait_with_truth
    TradingEngine._runtime_truth_downstream_installed = True
