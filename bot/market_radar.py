"""NEXUS-7 Market Radar: periodic Telegram panel of the monitored universe.

OBSERVABILITY ONLY. This module never changes a score, signal, NEXUS decision,
risk limit, size, leverage, SL/TP or order authorization, and never calls the
exchange. It derives a per-symbol view from canonical log lines that the
strategy/engine already emit (same passive pattern as ``funnel_metrics`` and
``entry_latency_observability``), and reads ``engine.positions`` keys only to
mark OPEN symbols.

Sources (first match wins, per record):
* ``Analyzer.analyze_mtf``: data/regime/MTF/score/post-score vetoes/SINAL
* ``pullback_confirmation_hardening``: ``[PULLBACK_CONFIRMATION] ... BLOCKED``
* ``nexus_decision_consistency``: ``[NEXUS_SCORE_DECOMP] decision=WAIT|...``
* ``engine._open``: ``[AI_DECISION] decision=APPROVE|REJECT``
* any later gate logging ``symbol=X ... result=BLOCK`` → RISK_BLOCK

Scores are the strategy confluence score (the value compared with
``MIN_ENTRY_SCORE``). A symbol that never reached scoring shows ``N/A``;
entries older than ``MARKET_RADAR_STALE_S`` expire back to SCANNING and their
score is not shown as current.

The periodic sender is started by the reviewed ``TradingEngine.run`` owner on
the engine event loop (like ``startup_ready_notification``) and cancelled with
it. Telegram failures are logged and swallowed; they never reach the engine.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

SCANNING = "SCANNING"
HOLD = "HOLD"
WAIT = "WAIT"
MTF_VETO = "MTF_VETO"
NEXUS_REJECT = "NEXUS_REJECT"
NEXUS_APPROVE = "NEXUS_APPROVE"
RISK_BLOCK = "RISK_BLOCK"
OPEN = "OPEN"
ERROR = "ERROR"
STATES = (SCANNING, HOLD, WAIT, MTF_VETO, NEXUS_REJECT, NEXUS_APPROVE, RISK_BLOCK, OPEN, ERROR)

# Tie-break for equal scores: the further a symbol progressed, the higher.
_STATE_RANK = {
    OPEN: 0, NEXUS_APPROVE: 1, RISK_BLOCK: 2, WAIT: 3, NEXUS_REJECT: 4,
    HOLD: 5, MTF_VETO: 6, ERROR: 7, SCANNING: 8,
}

TELEGRAM_MAX_CHARS = 4096
_NO_DATA_WAITING = "aguardando scan"
_NO_DATA_OPEN = "posição aberta (sem dado recente)"
_NO_AGE_REASONS = (_NO_DATA_WAITING, _NO_DATA_OPEN)
_SAFE_MAX_CHARS = 3900  # headroom below Telegram's hard limit


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else minimum


def settings() -> dict:
    return {
        "enabled": os.environ.get("MARKET_RADAR_ENABLED", "true").strip().lower() == "true",
        "interval_s": _env_float("MARKET_RADAR_INTERVAL_S", 1800.0, 300.0),
        "stale_s": _env_float("MARKET_RADAR_STALE_S", 900.0, 60.0),
        "first_delay_s": _env_float("MARKET_RADAR_FIRST_DELAY_S", 300.0, 30.0),
    }


@dataclass(frozen=True)
class RadarEntry:
    symbol: str
    state: str
    score: int | None
    direction: str | None
    reason: str
    observed_at: float


_SYM = r"(?P<symbol>[A-Z0-9]{2,20}USDT)"
_DIR = r"(?P<direction>LONG|SHORT)"

# (compiled regex, state, reason). Named groups: symbol, optional score/direction.
_RULES: tuple[tuple[re.Pattern, str, str], ...] = (
    (re.compile(rf"⛔ \[{_SYM}\] (?:CANDLES INSUFICIENTES|SEM DADOS)"), ERROR, "dados insuficientes"),
    (re.compile(rf"⛔ \[{_SYM}\] SCORE INVÁLIDO"), ERROR, "score inválido"),
    (re.compile(rf"⛔ \[{_SYM}\] REGIME (?P<regime>\S+) no 4H"), HOLD, "regime"),
    (re.compile(rf"⛔ \[{_SYM}\] 4H/1H NÃO ALINHADOS"), MTF_VETO, "4H/1H desalinhados"),
    (re.compile(rf"\[{_SYM}\] Score=(?P<score>\d+)/100 < \d+ → HOLD"), HOLD, "score < mínimo"),
    (re.compile(rf"⛔ \[{_SYM}\] BLOQUEIO RSI extremo:.*score era (?P<score>\d+)"), HOLD, "RSI extremo"),
    (re.compile(rf"⛔ \[{_SYM}\] BLOQUEIO volume:.*score era (?P<score>\d+)"), HOLD, "volume"),
    (re.compile(rf"⛔ \[{_SYM}\] BLOQUEIO alinhamento:.*score era (?P<score>\d+)"), MTF_VETO, "15M desalinhado"),
    (re.compile(rf"⛔ \[{_SYM}\] BLOQUEIO setup:.*score (?P<score>\d+)"), HOLD, "sem gatilho"),
    (re.compile(rf"\[{_SYM}\] ✅ SINAL {_DIR} score=(?P<score>\d+)/100"), WAIT, "sinal → confirmação"),
    (re.compile(rf"\[PULLBACK_CONFIRMATION\] .*symbol={_SYM} side={_DIR} result=BLOCKED"), WAIT, "pullback"),
)
_RE_NEXUS_DECOMP = re.compile(
    rf"\[NEXUS_SCORE_DECOMP\] symbol={_SYM} decision=(?:\w+\.)?(?P<decision>[A-Z_]+)"
)
_RE_AI = re.compile(
    rf"\[AI_DECISION\] symbol={_SYM} side={_DIR} decision=(?P<decision>APPROVE|REJECT)"
    r".*?decision_source=(?P<source>\S+)"
)
_RE_NEXUS_ERROR = re.compile(rf"_nexus_validate {_SYM}: ")
_RE_RISK_BLOCK = re.compile(
    rf"^\[(?P<tag>[A-Z0-9_]+)\].*\bsymbol={_SYM}\b.*\bresult=BLOCK\b(?!ED)"
)
_RE_RISK_BLOCK_ALT = re.compile(rf"^\[(?P<tag>[A-Z0-9_]+)\] result=BLOCK symbol={_SYM}\b")
_RE_ADJUSTED_EQUITY = re.compile(r"^\[ADJUSTED_EQUITY\] (?P<body>.*)$")
_RE_KV = re.compile(r"(?P<key>[a-z_]+)=(?P<value>\S+)")
_RE_GLOBAL_PAUSE = re.compile(r"^(?:⏸️ Scan pulado|🚫 ENTRADAS BLOQUEADAS|🚫 SCAN_SUSPENSO)")


class MarketRadar:
    """Thread-safe per-symbol state (records arrive from engine and worker threads)."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, RadarEntry] = {}
        self._global_note: tuple[str, float] | None = None
        self._account: tuple[dict, float] | None = None
        self.parse_errors = 0

    # ── ingestion ────────────────────────────────────────────────
    def _set(self, symbol, state, score, direction, reason, keep_from_previous=False):
        now = self._clock()
        with self._lock:
            prev = self._entries.get(symbol)
            if keep_from_previous and prev is not None:
                if score is None:
                    score = prev.score
                if direction is None:
                    direction = prev.direction
            self._entries[symbol] = RadarEntry(symbol, state, score, direction, reason, now)

    def observe(self, message: str) -> None:
        """Parse one already-formatted log message. Never raises."""
        try:
            self._observe(str(message))
        except Exception:  # noqa: BLE001 - observability must never propagate
            self.parse_errors += 1

    def _observe(self, msg: str) -> None:
        if msg.startswith("[MARKET_RADAR"):
            return
        m = _RE_ADJUSTED_EQUITY.search(msg)
        if m:
            fields = {k.group("key"): k.group("value") for k in _RE_KV.finditer(m.group("body"))}
            with self._lock:
                self._account = (fields, self._clock())
            return
        for rx, state, reason in _RULES:
            m = rx.search(msg)
            if m:
                g = m.groupdict()
                score = int(g["score"]) if g.get("score") else None
                direction = g.get("direction")
                if g.get("regime"):
                    reason = f"regime {g['regime']}"
                # Pullback WAIT follows the SINAL line: keep its score/direction.
                self._set(g["symbol"], state, score, direction, reason,
                          keep_from_previous=(reason == "pullback"))
                return

        m = _RE_NEXUS_DECOMP.search(msg)
        if m:
            decision = m.group("decision")
            if decision == "WAIT":
                self._set(m.group("symbol"), WAIT, None, None, "NEXUS WAIT",
                          keep_from_previous=True)
            return

        m = _RE_AI.search(msg)
        if m:
            symbol, source = m.group("symbol"), m.group("source")
            if source in ("timeout", "exception"):
                self._set(symbol, ERROR, None, m.group("direction"), f"NEXUS {source}",
                          keep_from_previous=True)
            elif m.group("decision") == "APPROVE":
                self._set(symbol, NEXUS_APPROVE, None, m.group("direction"), "NEXUS",
                          keep_from_previous=True)
            else:
                with self._lock:
                    prev = self._entries.get(symbol)
                nexus_wait = (prev is not None and prev.state == WAIT
                              and prev.reason == "NEXUS WAIT"
                              and self._clock() - prev.observed_at < 30.0)
                if not nexus_wait:
                    self._set(symbol, NEXUS_REJECT, None, m.group("direction"), "NEXUS",
                              keep_from_previous=True)
            return

        m = _RE_NEXUS_ERROR.search(msg)
        if m:
            self._set(m.group("symbol"), ERROR, None, None, "NEXUS erro", keep_from_previous=True)
            return

        m = _RE_RISK_BLOCK.search(msg) or _RE_RISK_BLOCK_ALT.search(msg)
        if m:
            self._set(m.group("symbol"), RISK_BLOCK, None, None, m.group("tag"),
                      keep_from_previous=True)
            return

        if _RE_GLOBAL_PAUSE.search(msg):
            note = msg.split("|")[0].strip()[:120]
            with self._lock:
                self._global_note = (note, self._clock())

    # ── snapshot/render ─────────────────────────────────────────
    def snapshot(self, symbols, open_symbols=(), *, stale_s: float) -> list[RadarEntry]:
        now = self._clock()
        opened = {str(s) for s in open_symbols}
        with self._lock:
            entries = dict(self._entries)
        rows: list[RadarEntry] = []
        for symbol in dict.fromkeys(str(s) for s in symbols):
            entry = entries.get(symbol)
            fresh = entry is not None and (now - entry.observed_at) <= stale_s
            if symbol in opened:
                rows.append(RadarEntry(
                    symbol, OPEN,
                    entry.score if fresh else None,
                    entry.direction if fresh else None,
                    "posição aberta" if fresh else _NO_DATA_OPEN,
                    entry.observed_at if fresh else now,
                ))
            elif fresh:
                rows.append(entry)
            else:
                rows.append(RadarEntry(symbol, SCANNING, None, None,
                                       "sem dado recente" if entry else _NO_DATA_WAITING,
                                       entry.observed_at if entry else now))
        return sort_rows(rows)

    def account(self, *, stale_s: float) -> dict | None:
        """Latest cash-flow-adjusted account view ([ADJUSTED_EQUITY]), if fresh."""
        with self._lock:
            account = self._account
        if account is None or self._clock() - account[1] > stale_s:
            return None
        return dict(account[0])

    def global_note(self, *, stale_s: float) -> str | None:
        with self._lock:
            note = self._global_note
        if note is None or self._clock() - note[1] > stale_s:
            return None
        return note[0]


def sort_rows(rows):
    """Score desc; N/A last; ties by pipeline progress, then symbol."""
    return sorted(rows, key=lambda r: (
        r.score is None, -(r.score or 0), _STATE_RANK.get(r.state, 99), r.symbol,
    ))


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _num(fields: dict, key: str) -> float | None:
    raw = str(fields.get(key, "N/A")).rstrip("%")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value == value else None


def account_lines(account: dict | None) -> list[str]:
    """Equity / performance HWM / external flows / trading drawdown / limit.

    Only the cash-flow-adjusted drawdown is shown as drawdown. A gross figure
    caused by an external flow is never presented as trading drawdown.
    """
    if not account:
        return ["Conta: `sem dado recente de drawdown ajustado`"]
    equity = _num(account, "equity")
    hwm = _num(account, "performance_hwm")
    limit = str(account.get("drawdown_limit", "N/A"))
    applied = int(_num(account, "external_flows_applied") or 0)
    applied_net = _num(account, "external_flows_net") or 0.0
    pending = int(_num(account, "pending_flows") or 0)
    pending_net = _num(account, "pending_net") or 0.0
    lines = [
        f"Equity: *{'N/A' if equity is None else f'{equity:.4f}'} USDT*",
        f"Performance HWM: *{'N/A' if hwm is None else f'{hwm:.4f}'}*",
        f"Fluxos externos: *{applied} reconciliado(s), líquido {applied_net:+.4f} USDT*",
    ]
    if account.get("status") == "RECONCILED" and pending == 0:
        lines.append(f"Drawdown de trading: *{account.get('trading_drawdown', 'N/A')}* (limite {limit})")
    else:
        lines.append(f"⚠️ Fluxos pendentes: *{pending}, líquido {pending_net:+.4f} USDT* — entradas bloqueadas")
        lines.append(f"Drawdown de trading: *N/A* (reconciliação pendente; limite {limit})")
    return lines


def render(rows, *, now: float, min_score: int, open_count: int,
           global_note: str | None = None, max_chars: int = _SAFE_MAX_CHARS,
           account: dict | None = None, show_account: bool = False) -> str:
    """Telegram Markdown text; the table sits in a code block (no escaping issues)."""
    ts = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    for i, r in enumerate(rows, 1):
        score = f"{r.score:>3d}" if r.score is not None else "N/A"
        direction = r.direction or "—"
        age = "—" if r.reason in _NO_AGE_REASONS else _age(now - r.observed_at)
        lines.append(f"{i:>2}. {r.symbol:<11} {score} {direction:<5} {r.state:<13} {age:>4}")
    above = sum(1 for r in rows if r.score is not None and r.score >= min_score)
    approved = sum(1 for r in rows if r.state == NEXUS_APPROVE)
    header = "📊 *NEXUS-7 — MARKET RADAR*\n"
    footer = (
        f"Monitorados: *{len(rows)}*\n"
        f"Acima do score mínimo ({min_score}): *{above}*\n"
        f"NEXUS APPROVE: *{approved}*\n"
        f"Posições abertas: *{open_count}*\n"
        + ("".join(f"{line}\n" for line in account_lines(account)) if show_account else "")
        + (f"Status: `{global_note.replace('`', '')}`\n" if global_note else "")
        + f"Timestamp UTC: `{ts}`\n"
        "_Somente observabilidade — não altera decisões nem ordens._"
    )
    legend = " #  SYMBOL      SCR DIR   STATE          AGE"
    body = "\n".join([legend] + lines)
    text = f"{header}```\n{body}\n```\n{footer}"
    while len(text) > max_chars and lines:
        lines.pop()
        body = "\n".join([legend] + lines + ["… (truncado)"])
        text = f"{header}```\n{body}\n```\n{footer}"
    return text[:max_chars]


# ── runtime wiring ──────────────────────────────────────────────
RADAR = MarketRadar()


class _RadarHandler(logging.Handler):
    def __init__(self, radar: MarketRadar):
        super().__init__(level=logging.DEBUG)
        self.radar = radar

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.radar.observe(record.getMessage())
        except Exception:  # noqa: BLE001 - a log handler must never raise
            self.radar.parse_errors += 1


def install(log) -> MarketRadar:
    """Attach the passive observer to the application logger (idempotent)."""
    if getattr(log, "_market_radar_installed", False):
        return RADAR
    log.addHandler(_RadarHandler(RADAR))
    log._market_radar_installed = True
    cfg = settings()
    log.info(
        "[MARKET_RADAR] installed=true enabled=%s interval_s=%.0f stale_s=%.0f "
        "source=existing_runtime_logs decision_effect=NONE execution_effect=NONE",
        str(cfg["enabled"]).lower(), cfg["interval_s"], cfg["stale_s"],
    )
    return RADAR


def _monitored_symbols(engine) -> list[str]:
    from bot.config import cfg

    return list(getattr(cfg, "SYMBOLS", []) or [])


def _open_symbols(engine) -> list[str]:
    try:
        return list((getattr(engine, "positions", {}) or {}).keys())
    except Exception:  # noqa: BLE001 - dict mutated concurrently: report none rather than raise
        return []


def build_message(engine, radar: MarketRadar = RADAR, *, now: float | None = None) -> str:
    from bot.config import cfg

    stale_s = settings()["stale_s"]
    opened = _open_symbols(engine)
    rows = radar.snapshot(_monitored_symbols(engine), opened, stale_s=stale_s)
    return render(
        rows,
        now=radar._clock() if now is None else now,
        min_score=int(cfg.MIN_ENTRY_SCORE),
        open_count=len(opened),
        global_note=radar.global_note(stale_s=stale_s),
        account=radar.account(stale_s=stale_s),
        show_account=True,
    )


async def send_once(engine, log, notify=None, radar: MarketRadar = RADAR) -> bool:
    """Build and send one panel. Returns False on any failure; never raises."""
    try:
        if notify is None:
            from bot.notifier import notify as notify
        text = build_message(engine, radar)
        result = await notify(text)
        from bot.telegram_transport import delivered
        if not delivered(result):
            log.warning("[MARKET_RADAR] sent=false class=%s skipped=%s execution_effect=NONE",
                        getattr(result, "cls", "NA"), getattr(result, "skipped", None) or "NA")
            return False
        log.info("[MARKET_RADAR] sent=true chars=%d execution_effect=NONE", len(text))
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - Telegram must never affect execution
        log.warning("[MARKET_RADAR] send_failed error=%s execution_effect=NONE", type(exc).__name__)
        return False


async def _loop(engine, log, notify=None, radar: MarketRadar = RADAR) -> None:
    cfg = settings()
    await asyncio.sleep(cfg["first_delay_s"])
    while True:
        await send_once(engine, log, notify, radar)
        await asyncio.sleep(settings()["interval_s"])


def start(engine, log, notify=None):
    """Start the periodic sender on the running loop; None when disabled."""
    if not settings()["enabled"]:
        return None
    return asyncio.create_task(_loop(engine, log, notify), name="market_radar")


async def cancel(task) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
