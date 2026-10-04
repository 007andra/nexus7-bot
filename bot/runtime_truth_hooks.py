"""Runtime truth instrumentation hooks.

All hooks are installed only when BGX_RUNTIME_TRUTH_ENABLED=true. They return
production results unchanged and perform no exchange mutations. Transport
capture observes aiohttp/websockets objects without storing connection tokens.
"""
from __future__ import annotations

import contextvars
import copy
import time
import uuid

from bot import runtime_truth as truth
from bot import runtime_truth_rest as truth_rest

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


def _checkpoint_snapshot(client) -> dict:
    cache_state = {}
    provenance = {}
    cache_hashes = {}
    provenance_hashes = {}
    for (symbol, timeframe), rows_obj in list((getattr(client, "_kline_cache", {}) or {}).items()):
        rows = [dict(row) for row in list(rows_obj)]
        key = f"{symbol}|{timeframe}"
        cache_state[key] = rows
        prov = truth.RECORDER.provenance_for(symbol, str(timeframe), rows)
        provenance[key] = prov
        cache_hashes[key] = truth.cache_data_hash(rows)
        provenance_hashes[key] = truth.provenance_hash(prov)
    return {
        "cache_state": cache_state,
        "provenance": provenance,
        "cache_data_hashes": cache_hashes,
        "provenance_hashes": provenance_hashes,
    }


def _raw_ws_fields(msg: dict) -> tuple[str | None, str | None, int | None, dict]:
    topic = str(msg.get("topic", "")) if isinstance(msg, dict) else ""
    data = msg.get("data", {}) if isinstance(msg, dict) else {}
    candles = data.get("candles") if isinstance(data, dict) else None
    if "andle" not in topic or not isinstance(candles, (list, tuple)) or len(candles) < 7:
        return None, None, None, {}
    tail = topic.split(":")[-1]
    parts = tail.split("_")
    kc_symbol = "_".join(parts[:-1])
    ws_iv = parts[-1]
    try:
        from bot.kucoin import to_standard, WS_INTERVAL_MAP_REV
        symbol = to_standard(kc_symbol)
        timeframe = str(WS_INTERVAL_MAP_REV.get(ws_iv, ws_iv))
    except Exception:
        symbol, timeframe = kc_symbol, ws_iv
    ts = int(float(candles[0]))
    ts = ts * 1000 if ts < 100_000_000_000 else ts
    return symbol, timeframe, ts, {
        "ws_index_5_volume": candles[5],
        "ws_index_6_amount": candles[6],
    }


def _mutation_action(before: list[dict], after: list[dict], candle_ts: int | None) -> str:
    if candle_ts is None:
        return "OTHER"
    before_ts = [int(x.get("ts", -1)) for x in before]
    after_ts = [int(x.get("ts", -1)) for x in after]
    if candle_ts in before_ts and candle_ts in after_ts:
        return "REPLACE_EXISTING"
    if after_ts and candle_ts == after_ts[-1]:
        return "APPEND"
    return "OUT_OF_ORDER_INSERT"


def _register_series_provenance(
    symbol: str,
    timeframe: str,
    rows: list[dict],
    source: str,
    raw_event_id: str | None,
    raw_volume_fields: dict | None = None,
):
    for row in rows:
        if row.get("ts") is None:
            continue
        truth.RECORDER.register_provenance(symbol, timeframe, int(row["ts"]), {
            "candle_ts": int(row["ts"]),
            "source": source,
            "raw_event_id": raw_event_id,
            "raw_volume_fields": dict(raw_volume_fields or {}),
            "normalized_activity": row.get("v"),
        })


def install_transport_and_cache(KuCoinClient):
    if not truth.enabled() or getattr(KuCoinClient, "_runtime_truth_transport_installed", False):
        return
    import aiohttp
    import websockets

    original_response_json = aiohttp.ClientResponse.json

    async def response_json_with_truth(self, *args, **kwargs):
        try:
            if getattr(self.url, "path", "") == "/api/v1/kline/query":
                # Explicit class dispatch keeps the existing aiohttp response body
                # cache semantics while remaining statically provable by selfcheck.
                raw = await aiohttp.ClientResponse.read(self)
                truth_rest.remember_raw_kline_fields(raw)
                event_id = truth.capture_rest_response(
                    "/api/v1/kline/query", dict(self.url.query), int(self.status), raw
                )
                _LAST_REST_EVENT_ID.set(event_id)
        except Exception:
            pass
        return await original_response_json(self, *args, **kwargs)

    aiohttp.ClientResponse.json = response_json_with_truth

    original_connect = websockets.connect

    class WSProxy:
        def __init__(self, ws, session_id: str, capture: bool):
            self._ws = ws
            self._session_id = session_id
            self._capture = capture
            self._seen_data = False

        def __getattr__(self, name):
            return getattr(self._ws, name)

        def __aiter__(self):
            return self

        async def __anext__(self):
            raw = await self._ws.__anext__()
            if self._capture:
                try:
                    event_id = truth.capture_ws_application_payload(raw, self._session_id)
                    _LAST_WS_EVENT_ID.set(event_id)
                    if not self._seen_data:
                        self._seen_data = True
                        truth.request_checkpoint("WS_RECONNECT_STABILIZED")
                except Exception:
                    pass
            return raw

    class ConnectProxy:
        def __init__(self, inner, capture: bool):
            self._inner = inner
            self._capture = capture
            self._session_id = str(uuid.uuid4())
            self._ws = None

        async def __aenter__(self):
            global _PREVIOUS_WS_SESSION
            ws = await self._inner.__aenter__()
            self._ws = WSProxy(ws, self._session_id, self._capture)
            if self._capture:
                truth.emit("MARKET_WS_RECONNECT", payload={
                    "previous_connection_session_id": _PREVIOUS_WS_SESSION,
                    "new_connection_session_id": self._session_id,
                    "reconnect_timestamp": time.time(),
                    "source": "CONNECT",
                })
                _PREVIOUS_WS_SESSION = self._session_id
            return self._ws

        async def __aexit__(self, *args):
            return await self._inner.__aexit__(*args)

    def connect_with_truth(uri, *args, **kwargs):
        text = str(uri)
        capture = "connectId=bgx-" in text and "bgx7-priv" not in text
        return ConnectProxy(original_connect(uri, *args, **kwargs), capture)

    websockets.connect = connect_with_truth

    original_init = KuCoinClient.__init__

    def init_with_truth(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        truth.register_checkpoint_provider(lambda: _checkpoint_snapshot(self))

    KuCoinClient.__init__ = init_with_truth

    original_seed = KuCoinClient._seed_kline_cache

    async def seed_with_truth(self, symbols, intervals):
        with truth.rest_purpose("STARTUP_SEED"):
            result = await original_seed(self, symbols, intervals)
        truth.emit("MARKET_CACHE_RESEED", payload={
            "reason": "STARTUP_SEED_COMPLETE",
            "series_count": len(getattr(self, "_kline_cache", {})),
            "sequence_watermark": truth.RECORDER.current_sequence,
        })
        truth.request_checkpoint("STARTUP_SEED_COMPLETE")
        return result

    KuCoinClient._seed_kline_cache = seed_with_truth

    original_get_klines = KuCoinClient.get_klines

    async def get_klines_with_truth(self, symbol, interval, limit=200):
        key = (symbol, str(interval))
        before = [dict(x) for x in list((getattr(self, "_kline_cache", {}) or {}).get(key, []))]
        result = await original_get_klines(self, symbol, interval, limit)
        after = [dict(x) for x in list((getattr(self, "_kline_cache", {}) or {}).get(key, []))]
        source = truth.current_rest_purpose()
        raw_id = _LAST_REST_EVENT_ID.get()
        raw_fields_by_ts = truth_rest.current_raw_kline_fields()
        returned_rows = [dict(x) for x in list(result or [])]
        returned_raw_fields = []
        for row in returned_rows:
            candle_ts = int(row.get("ts", 0) or 0)
            raw_fields = dict(raw_fields_by_ts.get(candle_ts, {"candle_ts": candle_ts}))
            returned_raw_fields.append(raw_fields)
            truth.RECORDER.register_provenance(symbol, str(interval), candle_ts, {
                "candle_ts": candle_ts,
                "source": source,
                "raw_event_id": raw_id,
                "raw_volume_fields": raw_fields,
                "normalized_activity": row.get("v"),
            })
        truth.emit("MARKET_CACHE_MUTATION", symbol=symbol, timeframe=str(interval), payload={
            "cache_key": f"{symbol}|{interval}",
            "CACHE_BEFORE_HASH": truth.cache_data_hash(before),
            "CACHE_AFTER_HASH": truth.cache_data_hash(after),
            "mutation_action": "CACHE_EXTEND",
            "source": source,
            "raw_event_id": raw_id,
            "normalized_row_before": before[-1] if before else None,
            "normalized_row_after": after[-1] if after else None,
            "series_after": after,
            "raw_volume_fields": {
                "source": "REST_RAW_EVENT",
                "raw_event_id": raw_id,
                "rows": returned_raw_fields,
            },
            "normalized_activity_field": None if not after else {"name": "v", "value": after[-1].get("v")},
        })
        return result

    KuCoinClient.get_klines = get_klines_with_truth

    original_ws_handler = KuCoinClient._handle_ws_message

    async def ws_handler_with_truth(self, msg):
        symbol, timeframe, candle_ts, raw_fields = _raw_ws_fields(msg)
        key = (symbol, str(timeframe)) if symbol and timeframe else None
        before = [dict(x) for x in list((getattr(self, "_kline_cache", {}) or {}).get(key, []))] if key else []
        result = await original_ws_handler(self, msg)
        if key:
            after = [dict(x) for x in list((getattr(self, "_kline_cache", {}) or {}).get(key, []))]
            row_after = next((dict(r) for r in after if int(r.get("ts", -1)) == candle_ts), None)
            row_before = next((dict(r) for r in before if int(r.get("ts", -1)) == candle_ts), None)
            raw_id = _LAST_WS_EVENT_ID.get()
            action = _mutation_action(before, after, candle_ts)
            truth.capture_cache_mutation(
                symbol=symbol,
                timeframe=str(timeframe),
                before_rows=before,
                after_rows=after,
                mutation_action=action,
                source="WS_UPDATE",
                raw_event_id=raw_id,
                normalized_before=row_before,
                normalized_after=row_after,
                raw_volume_fields=raw_fields,
            )
        return result

    KuCoinClient._handle_ws_message = ws_handler_with_truth
    KuCoinClient._runtime_truth_transport_installed = True


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
