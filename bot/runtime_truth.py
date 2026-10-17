"""Fail-open, observability-only runtime truth producer.

This module has no trading/exchange imports. When BGX_RUNTIME_TRUTH_ENABLED is
false (the default), all public capture operations are no-ops and no exporter
or network activity is started.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

SCHEMA_VERSION = "bgx-runtime-truth-event-v1"
HASH_VERSION = "BGX_CACHE_HASH_V1"
PROVENANCE_HASH_VERSION = "BGX_PROVENANCE_HASH_V1"
ROW_FIELDS = ("ts", "o", "h", "l", "c", "v")

_ENABLED = os.environ.get("BGX_RUNTIME_TRUTH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
_MAX_EVENTS = int(os.environ.get("BGX_TRUTH_QUEUE_MAX_EVENTS", "4096"))
_MAX_BYTES = int(os.environ.get("BGX_TRUTH_QUEUE_MAX_BYTES", "16777216"))
_MAX_EVENT_BYTES = int(os.environ.get("BGX_TRUTH_MAX_EVENT_BYTES", "262144"))
_DEPLOYMENT_SHA = os.environ.get("RAILWAY_GIT_COMMIT_SHA", os.environ.get("BGX_DEPLOYMENT_SHA", ""))
if len(_DEPLOYMENT_SHA) != 40 or any(c not in "0123456789abcdefABCDEF" for c in _DEPLOYMENT_SHA):
    _DEPLOYMENT_SHA = "0" * 40
_RUNTIME_INSTANCE_ID = os.environ.get("BGX_RUNTIME_INSTANCE_ID", "") or str(uuid.uuid4())

_current_evaluation_id = contextvars.ContextVar("bgx_truth_evaluation_id", default=None)
_current_evaluation_scope = contextvars.ContextVar("bgx_truth_evaluation_scope", default=None)
_current_analysis_cache_meta = contextvars.ContextVar("bgx_truth_analysis_cache_meta", default=None)
_rest_purpose = contextvars.ContextVar("bgx_truth_rest_purpose", default="OTHER")


def enabled() -> bool:
    from bot.hard_gate_shadow_context import active
    return _ENABLED and not active()


def runtime_instance_id() -> str:
    return _RUNTIME_INSTANCE_ID


def deployment_sha() -> str:
    return _DEPLOYMENT_SHA


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _num(value: Any) -> dict:
    if isinstance(value, bool):
        raise TypeError("boolean is not a candle numeric value")
    if isinstance(value, int):
        return {"i": value}
    return {"f64": float(value).hex()}


def canonical_strategy_row(row: dict) -> list:
    return [_num(row[name]) for name in ROW_FIELDS]


def cache_data_hash(rows: list[dict] | tuple[dict, ...]) -> str:
    ordered = sorted(list(rows or []), key=lambda row: int(row["ts"]))
    payload = canonical_json_bytes([canonical_strategy_row(row) for row in ordered])
    return hashlib.sha256((HASH_VERSION + "\n").encode() + payload).hexdigest()


def provenance_hash(items: list[dict]) -> str:
    return hashlib.sha256((PROVENANCE_HASH_VERSION + "\n").encode() + canonical_json_bytes(items)).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class QueueItem:
    event: dict
    size: int


class Recorder:
    def __init__(self):
        self.enabled = _ENABLED
        self.runtime_instance_id = _RUNTIME_INSTANCE_ID
        self.deployment_sha = _DEPLOYMENT_SHA
        self.max_events = max(1, _MAX_EVENTS)
        self.max_bytes = max(1024, _MAX_BYTES)
        self.max_event_bytes = max(1024, _MAX_EVENT_BYTES)
        self._queue: deque[QueueItem] = deque()
        self._queue_bytes = 0
        self._lock = threading.Lock()
        self._sequence = 0
        self.telemetry_dropped_total = 0
        self.export_dropped_total = 0
        self.oversized_total = 0
        self._provenance: dict[tuple[str, str, int], dict] = {}
        self._checkpoint_provider: Callable[[], dict] | None = None
        self._checkpoint_requests: deque[str] = deque(maxlen=32)
        self._enqueue_ns: deque[int] = deque(maxlen=10000)
        self._serialization_ns: deque[int] = deque(maxlen=10000)

    def next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    @property
    def current_sequence(self) -> int:
        with self._lock:
            return self._sequence

    def emit(self, event_type: str, *, symbol: str | None = None,
             timeframe: str | None = None, payload: dict | None = None) -> str | None:
        if not self.enabled:
            return None
        seq = self.next_sequence()  # intentionally before enqueue attempt
        t0 = time.perf_counter_ns()
        now_ns = time.time_ns()
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_type": str(event_type),
            "event_id": f"{self.runtime_instance_id}:{seq}",
            "timestamp_utc": _utc_now(),
            "timestamp_unix_ns": now_ns,
            "monotonic_ns": time.monotonic_ns(),
            "monotonic_sequence": seq,
            "runtime_instance_id": self.runtime_instance_id,
            "deployment_sha": self.deployment_sha,
            "symbol": symbol,
            "timeframe": None if timeframe is None else str(timeframe),
            "payload": dict(payload or {}),
        }
        raw = canonical_json_bytes(event)
        event["event_sha256"] = hashlib.sha256(raw).hexdigest()
        serialized = canonical_json_bytes(event)
        self._serialization_ns.append(time.perf_counter_ns() - t0)
        size = len(serialized)
        enqueue_start = time.perf_counter_ns()
        with self._lock:
            if size > self.max_event_bytes:
                self.oversized_total += 1
                self.telemetry_dropped_total += 1
                self._enqueue_ns.append(time.perf_counter_ns() - enqueue_start)
                return event["event_id"]
            if len(self._queue) >= self.max_events or self._queue_bytes + size > self.max_bytes:
                self.telemetry_dropped_total += 1
                self._enqueue_ns.append(time.perf_counter_ns() - enqueue_start)
                return event["event_id"]
            self._queue.append(QueueItem(event=event, size=size))
            self._queue_bytes += size
        self._enqueue_ns.append(time.perf_counter_ns() - enqueue_start)
        return event["event_id"]

    def drain(self, max_events: int, max_bytes: int) -> list[dict]:
        if not self.enabled:
            return []
        out: list[dict] = []
        used = 0
        with self._lock:
            while self._queue and len(out) < max_events:
                item = self._queue[0]
                if out and used + item.size > max_bytes:
                    break
                self._queue.popleft()
                self._queue_bytes -= item.size
                out.append(item.event)
                used += item.size
        return out

    def mark_export_drop(self, count: int) -> None:
        with self._lock:
            self.export_dropped_total += max(0, int(count))

    def reported_dropped_total(self) -> int:
        with self._lock:
            return self.telemetry_dropped_total + self.export_dropped_total

    def register_provenance(self, symbol: str, timeframe: str, candle_ts: int, metadata: dict) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._provenance[(str(symbol), str(timeframe), int(candle_ts))] = dict(metadata)
            if len(self._provenance) > 100000:
                for key in list(self._provenance)[:10000]:
                    self._provenance.pop(key, None)

    def provenance_for(self, symbol: str, timeframe: str, rows: list[dict]) -> list[dict]:
        if not self.enabled:
            return []
        with self._lock:
            return [dict(self._provenance.get((str(symbol), str(timeframe), int(row["ts"])), {"candle_ts": int(row["ts"]), "source": "UNKNOWN"})) for row in rows]

    def register_checkpoint_provider(self, provider: Callable[[], dict]) -> None:
        if self.enabled:
            self._checkpoint_provider = provider

    def checkpoint_provider(self):
        return self._checkpoint_provider

    def request_checkpoint(self, reason: str) -> None:
        if self.enabled:
            with self._lock:
                self._checkpoint_requests.append(str(reason))

    def pop_checkpoint_request(self) -> str | None:
        with self._lock:
            return self._checkpoint_requests.popleft() if self._checkpoint_requests else None

    def metrics(self) -> dict:
        with self._lock:
            return {
                "queue_depth": len(self._queue), "queue_bytes": self._queue_bytes,
                "telemetry_dropped_total": self.telemetry_dropped_total,
                "export_dropped_total": self.export_dropped_total,
                "oversized_total": self.oversized_total,
                "enqueue_ns": list(self._enqueue_ns), "serialization_ns": list(self._serialization_ns),
            }


RECORDER = Recorder()


def emit(event_type: str, *, symbol: str | None = None, timeframe: str | None = None, payload: dict | None = None) -> str | None:
    return RECORDER.emit(event_type, symbol=symbol, timeframe=timeframe, payload=payload)


def new_evaluation_id(scope: str, symbol: str | None = None) -> str:
    return f"{_RUNTIME_INSTANCE_ID}:{scope}:{symbol or 'GLOBAL'}:{uuid.uuid4().hex}"


@contextlib.contextmanager
def evaluation_context(evaluation_id: str, scope: str, cache_meta: dict | None = None):
    t1 = _current_evaluation_id.set(evaluation_id)
    t2 = _current_evaluation_scope.set(scope)
    t3 = _current_analysis_cache_meta.set(cache_meta or {})
    try:
        yield
    finally:
        _current_analysis_cache_meta.reset(t3); _current_evaluation_scope.reset(t2); _current_evaluation_id.reset(t1)


def current_evaluation() -> tuple[str | None, str | None]:
    return _current_evaluation_id.get(), _current_evaluation_scope.get()


def analysis_cache_meta() -> dict:
    return dict(_current_analysis_cache_meta.get() or {})


@contextlib.contextmanager
def rest_purpose(name: str):
    token = _rest_purpose.set(str(name))
    try: yield
    finally: _rest_purpose.reset(token)


def current_rest_purpose() -> str:
    return str(_rest_purpose.get())


def snapshot_cache_meta(client: Any, symbol: str, inputs: dict[str, list[dict]]) -> dict:
    if not _ENABLED:
        return {}
    out = {}
    cache = getattr(client, "_kline_cache", {}) or {}
    for tf, rows in inputs.items():
        shared = list(cache.get((symbol, str(tf)), []))
        prov = RECORDER.provenance_for(symbol, str(tf), shared)
        out[str(tf)] = {
            "cache_rows": len(shared),
            "CACHE_DATA_HASH": cache_data_hash(shared),
            "PROVENANCE_HASH": provenance_hash(prov),
            "input_rows_at_engine": len(rows),
            "ENGINE_INPUT_HASH": cache_data_hash(rows),
        }
    return out


def capture_ws_application_payload(payload: str | bytes, connection_session_id: str) -> str | None:
    if not _ENABLED: return None
    if isinstance(payload, str):
        raw = payload.encode("utf-8"); text = payload; kind = "APPLICATION_WS_PAYLOAD_UTF8"
    else:
        raw = bytes(payload); text = raw.decode("utf-8", errors="strict"); kind = "APPLICATION_WS_PAYLOAD_BYTES"
    topic = message_type = exchange_ts = message_id = None
    try:
        parsed = json.loads(text)
        topic = parsed.get("topic"); message_type = parsed.get("type"); message_id = parsed.get("id")
        data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
        exchange_ts = data.get("ts") or data.get("time") or parsed.get("ts")
    except Exception:
        pass
    return emit("MARKET_WS_RAW", payload={
        "payload_kind": kind, "connection_session_id": connection_session_id,
        "local_receive_timestamp_utc": _utc_now(), "local_receive_monotonic_ns": time.monotonic_ns(),
        "payload": text, "payload_sha256": hashlib.sha256(raw).hexdigest(),
        "topic": topic, "message_type": message_type,
        "exchange_event_timestamp": exchange_ts, "exchange_message_id_or_sequence": message_id,
    })


def capture_rest_response(endpoint: str, params: dict | None, http_status: int, raw_body: bytes) -> str | None:
    if not _ENABLED or endpoint != "/api/v1/kline/query": return None
    params = dict(params or {}); purpose = current_rest_purpose()
    return emit("MARKET_REST_SEED", symbol=str(params.get("symbol") or "") or None,
                timeframe=str(params.get("granularity") or "") or None,
                payload={
                    "request_endpoint": endpoint, "request_parameters": params,
                    "response_timestamp_utc": _utc_now(), "http_status": int(http_status),
                    "raw_response_utf8": raw_body.decode("utf-8", errors="strict"),
                    "raw_response_sha256": hashlib.sha256(raw_body).hexdigest(),
                    "contract": params.get("symbol"), "timeframe": params.get("granularity"),
                    "from": params.get("from"), "to": params.get("to"), "purpose": purpose,
                })


def capture_cache_mutation(*, symbol: str, timeframe: str, before_rows: list[dict], after_rows: list[dict],
                           mutation_action: str, source: str, raw_event_id: str | None,
                           normalized_before: dict | None, normalized_after: dict | None,
                           raw_volume_fields: dict | None = None) -> str | None:
    if not _ENABLED: return None
    event_id = emit("MARKET_CACHE_MUTATION", symbol=symbol, timeframe=timeframe, payload={
        "cache_key": f"{symbol}|{timeframe}", "CACHE_BEFORE_HASH": cache_data_hash(before_rows),
        "CACHE_AFTER_HASH": cache_data_hash(after_rows), "mutation_action": mutation_action,
        "source": source, "raw_event_id": raw_event_id,
        "normalized_row_before": normalized_before, "normalized_row_after": normalized_after,
        "raw_volume_fields": dict(raw_volume_fields or {}),
        "normalized_activity_field": None if normalized_after is None else {"name":"v","value":normalized_after.get("v")},
    })
    if normalized_after is not None and normalized_after.get("ts") is not None:
        RECORDER.register_provenance(symbol, timeframe, int(normalized_after["ts"]), {
            "candle_ts": int(normalized_after["ts"]), "source": source,
            "raw_event_id": raw_event_id, "mutation_event_id": event_id,
            "raw_volume_fields": dict(raw_volume_fields or {}),
            "normalized_activity": normalized_after.get("v"),
        })
    return event_id


def _tf_analysis(symbol: str, timeframe: str, raw: list[dict], prepared: list[dict], cache_meta: dict) -> dict:
    prov = RECORDER.provenance_for(symbol, timeframe, raw)
    closed = prepared[:-1] if len(prepared) else []
    values = [float(r.get("v", 0.0)) for r in closed]
    avg20 = sum(values[-21:-1]) / len(values[-21:-1]) if len(values) > 21 else (sum(values)/len(values) if values else None)
    return {
        **dict(cache_meta or {}),
        "ANALYZER_RAW_INPUT_HASH": cache_data_hash(raw),
        "ANALYZER_PREPARED_INPUT_HASH": cache_data_hash(prepared),
        "RAW_INPUT_PROVENANCE_HASH": provenance_hash(prov),
        "raw_row_count": len(raw), "prepared_row_count": len(prepared),
        "last_raw_timestamp": raw[-1].get("ts") if raw else None,
        "last_closed_timestamp": closed[-1].get("ts") if closed else None,
        "sentinel_present": bool(prepared and prepared[-1].get("_closed_bar_sentinel")),
        "sentinel_timestamp": prepared[-1].get("ts") if prepared else None,
        "current_activity_consumed": values[-1] if values else None,
        "avg20_activity_consumed": avg20,
        "source_provenance": prov[-3:],
    }


def capture_analysis_input(symbol: str, raw_by_tf: dict[str, list[dict]], prepared_by_tf: dict[str, list[dict]], evaluation_timestamp_ms: int) -> str | None:
    if not _ENABLED: return None
    evaluation_id, scope = current_evaluation()
    meta = analysis_cache_meta()
    timeframes = {}
    for tf in ("15", "60", "240"):
        timeframes[tf] = _tf_analysis(symbol, tf, raw_by_tf.get(tf, []), prepared_by_tf.get(tf, []), meta.get(tf, {}))
        timeframes[tf]["cache_key"] = f"{symbol}|{tf}"
    return emit("MARKET_ANALYSIS_INPUT", symbol=symbol, payload={
        "evaluation_id": evaluation_id, "evaluation_scope": scope,
        "evaluation_timestamp_ms": int(evaluation_timestamp_ms), "timeframes": timeframes,
    })


def emit_stage_result(stage: str, authority: str, result: Any, *, symbol: str | None = None, details: dict | None = None) -> str | None:
    if not _ENABLED: return None
    evaluation_id, scope = current_evaluation()
    payload = {"evaluation_id": evaluation_id, "evaluation_scope": scope, "stage": stage, "authority": authority, "details": dict(details or {})}
    if result is None:
        payload.update({"decision":"HOLD","direction":None,"score":None,"entry_type":None,"regime":None})
    else:
        payload.update({
            "decision":"SIGNAL", "direction":getattr(result,"direction",None),
            "score":getattr(result,"score",None), "entry_type":getattr(result,"entry_type",None),
            "regime":getattr(result,"regime",None),
        })
    return emit("MARKET_ANALYSIS_RESULT", symbol=symbol or getattr(result,"symbol",None), payload=payload)


def register_checkpoint_provider(provider: Callable[[], dict]) -> None:
    RECORDER.register_checkpoint_provider(provider)

def request_checkpoint(reason: str) -> None:
    RECORDER.request_checkpoint(reason)

def metrics() -> dict:
    return RECORDER.metrics()
