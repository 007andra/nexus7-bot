"""One terminal record per LIVE candidate (observability only).

Answers "why did this candidate die?" with a single line:

    [CANDIDATE_TERMINAL] candidate_id= symbol= side= score= terminal_stage=
    terminal_reason= nexus_called= final_sizing_reached= cross_reached=
    predispatch_reached= submission_reached= execution_effect=NONE

Contract (never a decision input):
- fully passive: no trading callable is wrapped, called or re-evaluated, so the
  protected ``_open`` wrapper chain (RUNTIME_CONTRACT) is untouched;
- stage evidence comes only from log records the existing gates already emit;
- records are written to stdout, never through the application logger, so
  they cannot recurse into handlers or alter any log-driven state machine;
- every telemetry failure is swallowed.

Correlation: a LIVE ``_open`` trace starts at ``[MIN_ORDER_FEASIBILITY]`` (the
outermost pilot gate, logged for every candidate) and ends at the first BLOCK
or exchange ACK. Symbol-less gate lines (pre-live preflight, pre-dispatch) are
attributed to the active trace; ``_open`` runs candidates sequentially. A trace
still open when the next one starts is flushed as ``NO_TERMINAL_LOG``.
Scan-stage deaths before ``_open`` (PULLBACK_CONFIRMATION, session/regime/PnL
funnel) are deduplicated per setup so a setup re-evaluated every scan cycle
produces one record per reason.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field

# Ordered pipeline stages (deepest reached wins when no explicit BLOCK seen).
STAGES = (
    "SCAN",
    "MIN_ORDER_FEASIBILITY",
    "PRELIVE",
    "NEXUS",
    "FINAL_SIZING",
    "FINAL_LOSS_BUDGET",
    "CROSS_STRESS",
    "PREDISPATCH",
    "SUBMISSION",
)

_SYM = r"(?P<symbol>[A-Z0-9]+USDT)"

# (regex, stage, outcome) - outcome: "reach" | "block" | "accepted".
_OPEN_RULES = (
    (re.compile(r"\[MIN_ORDER_FEASIBILITY\] symbol=\S+ result=BLOCK reason=(?P<reason>\S+)"),
     "MIN_ORDER_FEASIBILITY", "block"),
    (re.compile(r"\[MIN_ORDER_FEASIBILITY\] symbol=\S+ result=(?:PASS|DEFER)"),
     "MIN_ORDER_FEASIBILITY", "reach"),
    (re.compile(r"\[PILOT_LIVE_GATE\] symbol=\S+ stage=(?P<reason>\S+) result=BLOCK"),
     "PRELIVE", "block"),
    (re.compile(r"\[PILOT_LIVE_PREFLIGHT\] result=BLOCKED(?: reason=(?P<reason>\S+))?"),
     "PRELIVE", "block"),
    (re.compile(r"\[PILOT_LIVE_PREFLIGHT\] result=PASS"), "PRELIVE", "reach"),
    (re.compile(r"\[NEXUS_ZERO\] symbol=\S+ stage=(?P<reason>\S+)"), "NEXUS", "block"),
    (re.compile(r"\[AI_DECISION\] symbol=\S+ side=\S+ decision=REJECT(?:.* reason=(?P<reason>.*))?"),
     "NEXUS", "block"),
    (re.compile(r"\[(?:NEXUS_COST|NEXUS_SCORE_DECOMP|AI_DECISION)\]"), "NEXUS", "reach"),
    (re.compile(r"\[FINAL_SIZING_INVARIANT\] symbol=\S+ result=BLOCK(?: reason=(?P<reason>\S+))?"),
     "FINAL_SIZING", "block"),
    (re.compile(r"\[(?:RISK_V3_CORE|FINAL_SIZING_INVARIANT)\] symbol="), "FINAL_SIZING", "reach"),
    (re.compile(r"\[FINAL_LOSS_BUDGET\] symbol=\S+ setup_id=\S+ stage=\S+ result=BLOCK(?:.* reason=(?P<reason>\S+))?"),
     "FINAL_LOSS_BUDGET", "block"),
    (re.compile(r"\[FINAL_LOSS_BUDGET\] symbol="), "FINAL_LOSS_BUDGET", "reach"),
    (re.compile(r"\[BINANCE_CROSS_STRESS\] symbol=\S+ result=BLOCK(?: reason=(?P<reason>\S+))?"),
     "CROSS_STRESS", "block"),
    (re.compile(r"\[BINANCE_CROSS_STRESS\] symbol="), "CROSS_STRESS", "reach"),
    (re.compile(r"\[PILOT_PREDISPATCH_[A-Z_]+\] result=BLOCK(?:.*? reason=(?P<reason>\S+))?"),
     "PREDISPATCH", "block"),
    (re.compile(r"\[PILOT_PREDISPATCH_[A-Z_]+\]"), "PREDISPATCH", "reach"),
    (re.compile(r"📡 _open \S+ tentativa|\[PILOT\] symbol=\S+ submission_committed"),
     "SUBMISSION", "reach"),
    (re.compile(r"📤 \[(?:BINANCE_)?ORDER\] clientOid=\S+ orderId=\S+"), "SUBMISSION", "accepted"),
)

_OPEN_START = re.compile(rf"\[MIN_ORDER_FEASIBILITY\] symbol={_SYM} result=")
_SYMBOL_FIELD = re.compile(rf"(?:\bsymbol=|📡 _open |\[){_SYM}\b")
_CANDIDATE = re.compile(rf"✅ \[{_SYM}\] CANDIDATO: (?P<side>\S+) score=(?P<score>\S+)")
_CANDIDATE_ID = re.compile(rf"\[TECHNICAL_STOP_POLICY\] symbol={_SYM} .*?candidate_id=(?P<cid>\S+)")

_PULLBACK_BLOCK = re.compile(
    rf"\[PULLBACK_CONFIRMATION\] (?:setup_id=(?P<setup>\S+) )?symbol={_SYM} "
    r"side=(?P<side>\S+) result=BLOCKED reason=(?P<reason>\S+)"
)
_FUNNEL_REJECT = re.compile(
    rf"⛔ \[{_SYM}\] REJEITADO (?P<kind>no ajuste de sessão|pelo regime|por PnL)"
)
_FUNNEL_REASON = {
    "no ajuste de sessão": "session_score_adjustment",
    "pelo regime": "regime_disallows_direction",
    "por PnL": "expected_pnl_nonpositive",
}


@dataclass
class CandidateTrace:
    candidate_id: str
    symbol: str
    side: str
    score: str
    reached: set = field(default_factory=set)
    block_stage: str | None = None
    block_reason: str | None = None
    accepted: bool = False

    @property
    def finished(self) -> bool:
        return self.accepted or self.block_stage is not None


def _clean(value) -> str:
    text = str(value if value not in (None, "") else "NA").strip()
    return re.sub(r"\s+", "_", text)[:96] or "NA"


def observe_open_message(trace: CandidateTrace, message: str) -> None:
    """Fold one log message into a trace. Pure; first BLOCK wins."""
    for pattern, stage, outcome in _OPEN_RULES:
        match = pattern.search(message)
        if not match:
            continue
        trace.reached.add(stage)
        if outcome == "accepted":
            trace.accepted = True
        elif outcome == "block" and trace.block_stage is None:
            trace.block_stage = stage
            reason = match.groupdict().get("reason")
            trace.block_reason = _clean(reason) if reason else "BLOCK"
        return


def classify(trace: CandidateTrace, *, unfinished_reason: str = "NO_TERMINAL_LOG") -> tuple[str, str]:
    """Return ``(terminal_stage, terminal_reason)`` for a trace."""
    if trace.accepted:
        return "SUBMISSION", "ORDER_ACCEPTED"
    if trace.block_stage is not None:
        return trace.block_stage, trace.block_reason or "BLOCK"
    deepest = next((s for s in reversed(STAGES) if s in trace.reached), "OPEN_ENTRY")
    return deepest, unfinished_reason


def format_record(trace: CandidateTrace, stage: str, reason: str) -> str:
    def flag(name: str) -> str:
        return str(name in trace.reached).lower()

    return (
        f"[CANDIDATE_TERMINAL] candidate_id={_clean(trace.candidate_id)} "
        f"symbol={_clean(trace.symbol)} side={_clean(trace.side)} score={_clean(trace.score)} "
        f"terminal_stage={stage} terminal_reason={_clean(reason)} "
        f"nexus_called={flag('NEXUS')} final_sizing_reached={flag('FINAL_SIZING')} "
        f"cross_reached={flag('CROSS_STRESS')} predispatch_reached={flag('PREDISPATCH')} "
        f"submission_reached={flag('SUBMISSION')} telemetry_only=true execution_effect=NONE"
    )


class ScanTerminalDeduper:
    """One scan-stage record per (setup, stage, reason) - bounded memory."""

    def __init__(self, max_keys: int = 4096):
        self._seen: dict[tuple, None] = {}
        self._max = max_keys
        self._lock = threading.Lock()

    def first(self, key: tuple) -> bool:
        with self._lock:
            if key in self._seen:
                return False
            self._seen[key] = None
            while len(self._seen) > self._max:
                self._seen.pop(next(iter(self._seen)))
            return True


def _scan_record(candidate_id: str, symbol: str, side: str, stage: str, reason: str) -> str:
    trace = CandidateTrace(candidate_id=candidate_id, symbol=symbol, side=side, score="NA")
    trace.reached.add("SCAN")
    return format_record(trace, stage, reason)


def observe_scan_message(message: str, deduper: ScanTerminalDeduper,
                         now: float | None = None) -> str | None:
    match = _PULLBACK_BLOCK.search(message)
    if match:
        symbol, side, reason = match.group("symbol"), match.group("side"), match.group("reason")
        setup = match.group("setup") or f"{symbol}:{side}:PULLBACK:NA"
        if deduper.first((setup, "PULLBACK_CONFIRMATION", reason)):
            return _scan_record(setup, symbol, side, "PULLBACK_CONFIRMATION", reason)
        return None
    match = _FUNNEL_REJECT.search(message)
    if match:
        symbol = match.group("symbol")
        reason = _FUNNEL_REASON[match.group("kind")]
        bucket = int((time.time() if now is None else now) // 900)
        if deduper.first((symbol, "SCAN_FUNNEL", reason, bucket)):
            return _scan_record(f"{symbol}:{bucket}", symbol, "NA", "SCAN_FUNNEL", reason)
    return None


class CandidateTerminalCollector:
    """Pure state machine fed by already-produced runtime log messages."""

    def __init__(self):
        self.active: CandidateTrace | None = None
        self._candidates: dict[str, tuple[str, str]] = {}
        self._candidate_ids: dict[str, str] = {}
        self._dedupe = ScanTerminalDeduper()
        self._lock = threading.Lock()

    def _start(self, symbol: str) -> CandidateTrace:
        side, score = self._candidates.get(symbol, ("NA", "NA"))
        cid = self._candidate_ids.get(symbol) or f"{symbol}:{side}:{score}"
        return CandidateTrace(candidate_id=cid, symbol=symbol, side=side, score=score)

    def observe(self, message: str, now: float | None = None) -> list[str]:
        if message.startswith("[CANDIDATE_TERMINAL"):
            return []
        out: list[str] = []
        with self._lock:
            m = _CANDIDATE.search(message)
            if m:
                self._candidates[m.group("symbol")] = (m.group("side"), m.group("score"))
                return out
            m = _CANDIDATE_ID.search(message)
            if m:
                self._candidate_ids[m.group("symbol")] = m.group("cid")
                return out

            m = _OPEN_START.search(message)
            if m:
                if self.active is not None:
                    out.append(format_record(self.active, *classify(self.active)))
                self.active = self._start(m.group("symbol"))

            trace = self.active
            if trace is not None:
                sym = _SYMBOL_FIELD.search(message)
                if sym is None or sym.group("symbol") == trace.symbol:
                    before = (len(trace.reached), trace.finished)
                    observe_open_message(trace, message)
                    if trace.finished:
                        out.append(format_record(trace, *classify(trace)))
                        self.active = None
                        return out
                    if (len(trace.reached), trace.finished) != before:
                        return out

            line = observe_scan_message(message, self._dedupe, now)
            if line:
                out.append(line)
        return out


class _TerminalHandler(logging.Handler):
    def __init__(self, collector: CandidateTerminalCollector):
        super().__init__(level=logging.DEBUG)
        self.collector = collector

    def emit(self, record: logging.LogRecord) -> None:
        try:
            lines = self.collector.observe(record.getMessage())
            for line in lines:
                sys.stdout.write(line + "\n")
            if lines:
                sys.stdout.flush()
        except Exception:  # noqa: BLE001 - telemetry must never affect execution
            return


def install(log) -> CandidateTerminalCollector:
    """Attach one passive handler to the application logger."""
    existing = getattr(log, "_candidate_terminal_collector", None)
    if existing is not None:
        return existing
    collector = CandidateTerminalCollector()
    log.addHandler(_TerminalHandler(collector))
    log._candidate_terminal_collector = collector
    log.warning(
        "[CANDIDATE_TERMINAL] installed=true source=existing_gate_logs wraps_callables=false "
        "scope=live_open_trace_plus_scan_pullback_funnel output=stdout "
        "decision_effect=NONE execution_effect=NONE"
    )
    return collector


__all__ = [
    "CandidateTerminalCollector", "CandidateTrace", "ScanTerminalDeduper", "classify",
    "format_record", "install", "observe_open_message", "observe_scan_message",
]
