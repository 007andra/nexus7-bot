"""Passive end-to-end entry latency observability.

This module never wraps execution-critical callables and never changes trading
state. It observes existing log records and emits timing telemetry directly to
stdout so the measurements cannot recurse through the application logger.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field


_SYMBOL = r"(?P<symbol>[A-Z0-9]+USDT)"
_RE_WS = re.compile(rf"\[{_SYMBOL}\].*WS cache hit")
_RE_SIGNAL = re.compile(rf"\[{_SYMBOL}\].*✅ SINAL")
_RE_CANDIDATE = re.compile(rf"✅ \[{_SYMBOL}\] CANDIDATO")
_RE_PULLBACK = re.compile(rf"\[PULLBACK_CONFIRMATION\] symbol={_SYMBOL} .*result=(?P<result>[A-Z_]+)")
_RE_AI = re.compile(rf"\[AI_DECISION\] symbol={_SYMBOL} .*decision=(?P<decision>APPROVE|REJECT)")
_RE_PREFLIGHT = re.compile(rf"🔎 _open {_SYMBOL} ")
_RE_DISPATCH = re.compile(rf"📡 _open {_SYMBOL} tentativa")
_RE_ORDER = re.compile(rf"📤 \[ORDER\].*orderId=(?P<order_id>\S+) symbol={_SYMBOL} ")
_RE_TPSL = re.compile(rf"🛡️ {_SYMBOL}: .*anexados à posição")
_RE_FILLED = re.compile(rf"✅ \[FILLED\].*orderId=(?P<order_id>\S+) symbol={_SYMBOL} ")


@dataclass
class _Trace:
    symbol: str
    trace_id: str
    stages: dict[str, int] = field(default_factory=dict)


class EntryLatencyCollector:
    """Pure state machine fed by already-produced runtime log messages."""

    def __init__(self):
        self._traces: dict[str, _Trace] = {}
        self._latest_market_ns: dict[str, int] = {}
        # Only opening-order IDs admitted after an observed _open dispatch are
        # stored here. Reduce-only exits/partials use the same generic KuCoin
        # [ORDER]/[FILLED] log shapes, so symbol matching alone is unsafe.
        self._opening_order_to_symbol: dict[str, str] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def _new_trace(self, symbol: str, now_ns: int) -> _Trace:
        self._seq += 1
        trace = _Trace(symbol=symbol, trace_id=f"{symbol}-{self._seq}")
        # Never fabricate a market-data boundary: when no acquisition event was
        # observed for this symbol, market-relative durations stay NA instead
        # of a misleading 0.0 (audit P1-1).
        market_ns = self._latest_market_ns.get(symbol)
        if market_ns is not None:
            trace.stages["market_data"] = market_ns
        self._traces[symbol] = trace
        return trace

    def _trace(self, symbol: str, now_ns: int) -> _Trace:
        trace = self._traces.get(symbol)
        if trace is None:
            trace = self._new_trace(symbol, now_ns)
        return trace

    @staticmethod
    def _ms(a: int | None, b: int | None) -> str:
        if a is None or b is None or b < a:
            return "NA"
        return f"{(b-a)/1_000_000:.1f}"

    def _stage_line(self, trace: _Trace, stage: str, now_ns: int) -> str:
        trace.stages.setdefault(stage, now_ns)
        ordered = [
            "market_data", "signal", "pullback", "nexus", "preflight",
            "order_sent", "exchange_ack", "tpsl", "fill",
        ]
        idx = ordered.index(stage)
        previous = next(
            (trace.stages.get(ordered[j]) for j in range(idx - 1, -1, -1)
             if trace.stages.get(ordered[j]) is not None),
            None,
        )
        start = trace.stages.get("market_data")
        return (
            f"[ENTRY_LATENCY] trace={trace.trace_id} symbol={trace.symbol} "
            f"stage={stage} stage_ms={self._ms(previous, trace.stages[stage])} "
            f"total_ms={self._ms(start, trace.stages[stage])} telemetry_only=true "
            f"execution_effect=NONE"
        )

    def _summary_line(self, trace: _Trace, terminal: str) -> str:
        s = trace.stages
        return (
            f"[ENTRY_LATENCY_SUMMARY] trace={trace.trace_id} symbol={trace.symbol} "
            f"terminal={terminal} "
            f"market_to_signal_ms={self._ms(s.get('market_data'), s.get('signal'))} "
            f"signal_to_pullback_ms={self._ms(s.get('signal'), s.get('pullback'))} "
            f"pullback_to_nexus_ms={self._ms(s.get('pullback'), s.get('nexus'))} "
            f"nexus_to_preflight_ms={self._ms(s.get('nexus'), s.get('preflight'))} "
            f"preflight_to_send_ms={self._ms(s.get('preflight'), s.get('order_sent'))} "
            f"send_to_ack_ms={self._ms(s.get('order_sent'), s.get('exchange_ack'))} "
            f"ack_to_fill_ms={self._ms(s.get('exchange_ack'), s.get('fill'))} "
            f"ack_to_tpsl_ms={self._ms(s.get('exchange_ack'), s.get('tpsl'))} "
            f"market_to_terminal_ms={self._ms(s.get('market_data'), s.get(terminal))} "
            f"market_data_observed={str('market_data' in s).lower()} "
            f"telemetry_only=true execution_effect=NONE"
        )

    def _start_signal(self, symbol: str, now_ns: int) -> list[str]:
        trace = self._new_trace(symbol, now_ns)
        trace.stages["signal"] = now_ns
        return [self._stage_line(trace, "signal", now_ns)]

    def observe(self, message: str, now_ns: int | None = None) -> list[str]:
        now_ns = int(now_ns if now_ns is not None else time.monotonic_ns())
        out: list[str] = []
        with self._lock:
            m = _RE_WS.search(message)
            if m:
                self._latest_market_ns[m.group("symbol")] = now_ns
                return out

            m = _RE_SIGNAL.search(message)
            if m:
                return self._start_signal(m.group("symbol"), now_ns)

            m = _RE_CANDIDATE.search(message)
            if m:
                symbol = m.group("symbol")
                trace = self._traces.get(symbol)
                # CANDIDATO is downstream of the canonical strategy SINAL log.
                # If that signal was already observed, it belongs to the same
                # setup and must not create a second trace or reset timing.
                if trace is not None and "signal" in trace.stages:
                    return out
                return self._start_signal(symbol, now_ns)

            m = _RE_PULLBACK.search(message)
            if m:
                trace = self._trace(m.group("symbol"), now_ns)
                trace.stages["pullback"] = now_ns
                out.append(self._stage_line(trace, "pullback", now_ns))
                if m.group("result") == "BLOCKED":
                    out.append(self._summary_line(trace, "pullback"))
                return out

            m = _RE_AI.search(message)
            if m:
                trace = self._trace(m.group("symbol"), now_ns)
                trace.stages["nexus"] = now_ns
                out.append(self._stage_line(trace, "nexus", now_ns))
                if m.group("decision") == "REJECT":
                    out.append(self._summary_line(trace, "nexus"))
                return out

            m = _RE_PREFLIGHT.search(message)
            if m:
                trace = self._trace(m.group("symbol"), now_ns)
                trace.stages["preflight"] = now_ns
                out.append(self._stage_line(trace, "preflight", now_ns))
                return out

            m = _RE_DISPATCH.search(message)
            if m:
                trace = self._trace(m.group("symbol"), now_ns)
                trace.stages["order_sent"] = now_ns
                out.append(self._stage_line(trace, "order_sent", now_ns))
                return out

            m = _RE_ORDER.search(message)
            if m:
                symbol = m.group("symbol")
                trace = self._traces.get(symbol)
                # Generic KuCoin [ORDER] logs are emitted for opening and
                # reduce-only closing orders. Entry latency may consume an ACK
                # only when this exact trace has an observed _open dispatch and
                # has not already accepted an opening ACK.
                if (
                    trace is None
                    or "order_sent" not in trace.stages
                    or "exchange_ack" in trace.stages
                ):
                    return out
                trace.stages["exchange_ack"] = now_ns
                self._opening_order_to_symbol[m.group("order_id")] = symbol
                out.append(self._stage_line(trace, "exchange_ack", now_ns))
                return out

            m = _RE_TPSL.search(message)
            if m:
                trace = self._traces.get(m.group("symbol"))
                if trace is None or "exchange_ack" not in trace.stages:
                    return out
                trace.stages["tpsl"] = now_ns
                out.append(self._stage_line(trace, "tpsl", now_ns))
                if "fill" in trace.stages:
                    out.append(self._summary_line(trace, "fill"))
                return out

            m = _RE_FILLED.search(message)
            if m:
                order_id = m.group("order_id")
                mapped_symbol = self._opening_order_to_symbol.get(order_id)
                if not mapped_symbol:
                    return out
                symbol = m.group("symbol")
                if symbol != mapped_symbol:
                    return out
                trace = self._traces.get(symbol)
                if (
                    trace is None
                    or "exchange_ack" not in trace.stages
                    or "fill" in trace.stages
                ):
                    return out
                trace.stages["fill"] = now_ns
                out.append(self._stage_line(trace, "fill", now_ns))
                out.append(self._summary_line(trace, "fill"))
                self._opening_order_to_symbol.pop(order_id, None)
                return out

        return out


class _LatencyHandler(logging.Handler):
    def __init__(self, collector: EntryLatencyCollector):
        super().__init__(level=logging.DEBUG)
        self.collector = collector

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if message.startswith("[ENTRY_LATENCY"):
                return
            lines = self.collector.observe(message)
            for line in lines:
                sys.stdout.write(line + "\n")
            if lines:
                sys.stdout.flush()
        except Exception:
            # Telemetry must never influence execution or application logging.
            return


def install(log) -> EntryLatencyCollector:
    """Attach one passive handler to the application logger."""
    existing = getattr(log, "_entry_latency_collector", None)
    if existing is not None:
        return existing
    collector = EntryLatencyCollector()
    handler = _LatencyHandler(collector)
    log.addHandler(handler)
    log._entry_latency_collector = collector
    log.info(
        "[ENTRY_LATENCY_OBSERVABILITY] installed=true source=existing_runtime_logs "
        "correlation=latest_market_to_canonical_signal candidate_dedupe=true "
        "opening_order_lifecycle_only=true reduce_only_orders_ignored=true "
        "critical_callables_wrapped=false thresholds_unchanged=true leverage_unchanged=true "
        "sizing_unchanged=true execution_effect=NONE"
    )
    return collector
