import logging, sys, os
import queue
import re
import threading
import time
from collections import Counter

_AI_TG_QUEUE = queue.Queue(maxsize=100)
_AI_TG_CACHE = {}
_AI_TG_LOCK = threading.Lock()
_AI_TG_WORKER_STARTED = False
_AI_TG_REJECT_COOLDOWN = int(os.environ.get("NEXUS_VETO_COOLDOWN", "900"))
_FUNNEL_TG_COOLDOWN = int(os.environ.get("NEXUS_FUNNEL_COOLDOWN", "900"))
_METRICS_LOCK = threading.Lock()
_AI_METRICS = {"started_at": time.time(), "total": 0, "approved": 0, "rejected": 0, "score_sum": 0.0, "score_count": 0, "reasons": Counter(), "sources": Counter(), "sides": Counter(), "funnel_total": 0, "funnel_stages": Counter(), "funnel_symbols": Counter(), "last_summary_ai_total": 0, "last_summary_funnel_total": 0}
_AI_METRICS_EVERY = max(1, int(os.environ.get("NEXUS_METRICS_EVERY", "10")))
_FUNNEL_METRICS_EVERY = max(10, int(os.environ.get("NEXUS_FUNNEL_METRICS_EVERY", "50")))

# Core observability pacing. This used to be injected by sitecustomize by
# replacing _enqueue at runtime. Keeping it here preserves the exact 1.5s
# delivery pacing while removing one monkey patch from application startup.
_AUDIT_PACING_LOCK = threading.Lock()
_AUDIT_LAST_ENQUEUE = 0.0
_AUDIT_MIN_INTERVAL = 1.5

# Main-loop integrity blockers can remain unchanged for minutes (for example an
# external/manual position without a confirmed stop). The engine evaluates the
# gate every few seconds, so logging the same warning every cycle creates noise
# that can hide genuinely new faults. This filter affects only the duplicate
# log line; it never changes IntegrityGuard state, scan gating, retries, or
# exchange behavior. A changed reason is emitted immediately and an identical
# reason is re-emitted periodically as proof of life.
_BLOCK_LOG_LOCK = threading.Lock()
_BLOCK_LOG_LAST_KEY = ""
_BLOCK_LOG_LAST_TS = 0.0
_BLOCK_LOG_INTERVAL = 60.0
_BLOCK_LOG_PREFIX = "🚫 ENTRADAS BLOQUEADAS:"

_AI_DECISION_RE = re.compile(r"^\[AI_DECISION\]\s+symbol=(?P<symbol>\S+)\s+side=(?P<side>\S+)\s+decision=(?P<decision>\S+)\s+approved=(?P<approved>\S+)\s+decision_source=(?P<source>\S+)\s+score=(?P<score>\S+)\s+confidence=(?P<confidence>\S+)\s+ts=(?P<ts>\S+)\s+reason=(?P<reason>.*)$")
_FUNNEL_SESSION_RE = re.compile(r"^⛔ \[(?P<symbol>[^\]]+)\] REJEITADO no ajuste de sessão: (?P<score_before>[-+\d.]+)→(?P<score>[-+\d.]+) < (?P<minimum>[-+\d.]+) \(sessão (?P<session>[^)]+)\)$")
_FUNNEL_REGIME_RE = re.compile(r"^⛔ \[(?P<symbol>[^\]]+)\] REJEITADO pelo regime: (?P<side>\S+) não permitido em (?P<regime>\S+) \(score era (?P<score>[-+\d.]+)\)$")
_FUNNEL_PNL_RE = re.compile(r"^⛔ \[(?P<symbol>[^\]]+)\] REJEITADO por PnL: (?P<pnl>[-+\d.]+)% ≤ 0 após taxas \(score (?P<score>[-+\d.]+), R:R (?P<rr>[-+\d.]+)\)$")
_FUNNEL_HOLD_RE = re.compile(r"^\[(?P<symbol>[^\]]+)\] Score=(?P<score>[-+\d.]+)/100 \(4H:(?P<s4>[-+\d.]+) 1H:(?P<s1>[-+\d.]+) 15M:(?P<s15>[-+\d.]+)\) \| regime=(?P<regime>\S+) RSI=(?P<rsi>[-+\d.]+) vol=(?P<vol>[-+\d.]+)x \| 4H=(?P<t4>\S+) 1H=(?P<t1>\S+) → HOLD$")
_FUNNEL_DATA_RE = re.compile(r"^⛔ \[(?P<symbol>[^\]]+)\] SEM DADOS: 15m=(?P<d15>\d+)/(?P<m15>\d+) 1h=(?P<d1>\d+)/(?P<m1>\d+) 4h=(?P<d4>\d+)/(?P<m4>\d+) — cache e REST falharam$")

# Emergency diagnostics deliberately bypass this module's handlers. This avoids
# recursive logging while ensuring auxiliary observability failures are visible.
def _diag(message, exc=None):
    try:
        suffix = f": {type(exc).__name__}: {exc}" if exc is not None else ""
        sys.stderr.write(f"[NEXUS_OBSERVABILITY] {message}{suffix}\n")
        sys.stderr.flush()
    except Exception:
        # Last-resort stderr itself is non-critical; never affect trading/risk.
        return

def _tg_enabled():
    from bot import telegram_credentials
    return os.environ.get("NEXUS_TELEGRAM", "true").lower() == "true" and telegram_credentials.configured()

def _safe_float(value):
    try: return float(value)
    except (TypeError, ValueError): return None

def _ai_tg_format(d):
    approved = str(d.get("approved", "False")).lower() == "true"
    verdict = "✅ APPROVE" if approved else "🚫 REJECT"
    order_line = "Ordem: LIBERADA PARA PRÓXIMOS GATES" if approved else "Ordem: BLOQUEADA PELO GATE DA IA"
    return "\n".join(["🧠 NEXUS AI — DECISÃO", f"Par: {d.get('symbol','?')}", f"Direção: {d.get('side','?')}", f"Decisão: {verdict}", f"Score NEXUS: {d.get('score','N/A')}", f"Confiança: {d.get('confidence','N/A')}", f"Fonte: {d.get('source','N/A')}", f"Motivo: {d.get('reason','N/A')}", order_line])

def _parse_funnel(msg):
    for regex, extra in ((_FUNNEL_SESSION_RE, ("SESSION","score_reduzido_pela_sessao")), (_FUNNEL_REGIME_RE,("REGIME","direcao_incompativel_com_regime")), (_FUNNEL_PNL_RE,("PNL_LIQUIDO","pnl_esperado_nao_positivo")), (_FUNNEL_HOLD_RE,("MTF","setup_nao_atingiu_criterios_de_sinal")), (_FUNNEL_DATA_RE,("DATA","dados_insuficientes"))):
        m=regex.match(msg)
        if m:
            d=m.groupdict(); d.update(stage=extra[0], reason=extra[1]); return d
    return None

def _funnel_tg_format(d):
    lines=["🔎 NEXUS-7 — FUNIL PRÉ-IA",f"Par: {d.get('symbol','?')}",f"Estágio: {d.get('stage','?')}","Decisão: ⏸ HOLD / NÃO ENVIAR À IA"]
    for key,label in (("side","Direção"),("score","Score"),("minimum","Mínimo"),("regime","Regime"),("session","Sessão"),("rr","R:R"),("pnl","PnL esperado líquido")):
        if d.get(key) is not None: lines.append(f"{label}: {d[key]}")
    if d.get("s4"): lines.append(f"Timeframes: 4H={d['s4']} | 1H={d.get('s1')} | 15M={d.get('s15')}")
    if d.get("rsi"): lines.append(f"RSI 15M: {d['rsi']} | Volume: {d.get('vol')}x")
    if d.get("d15"): lines.append(f"Dados: 15M={d['d15']}/{d['m15']} | 1H={d['d1']}/{d['m1']} | 4H={d['d4']}/{d['m4']}")
    lines += [f"Motivo: {d.get('reason','N/A')}","NEXUS AI: NÃO CHAMADA NESTE ESTÁGIO"]
    return "\n".join(lines)

def _should_send(key,cooldown):
    now=time.time()
    with _AI_TG_LOCK:
        last=_AI_TG_CACHE.get(key,0.0)
        if now-last<cooldown: return False
        _AI_TG_CACHE[key]=now
        if len(_AI_TG_CACHE)>1000:
            cutoff=now-max(_AI_TG_REJECT_COOLDOWN,_FUNNEL_TG_COOLDOWN)*2
            for k,t in list(_AI_TG_CACHE.items()):
                if t<cutoff: _AI_TG_CACHE.pop(k,None)
    return True

def _ai_tg_should_send(d):
    approved=str(d.get("approved","False")).lower()=="true"
    return True if approved else _should_send(("AI",d.get("symbol"),d.get("side"),d.get("reason")),_AI_TG_REJECT_COOLDOWN)

def _funnel_tg_should_send(d): return _should_send(("FUNNEL",d.get("symbol"),d.get("stage"),d.get("reason"),d.get("regime")),_FUNNEL_TG_COOLDOWN)

def _metrics_snapshot_locked():
    total=_AI_METRICS["total"]; approved=_AI_METRICS["approved"]
    avg=_AI_METRICS["score_sum"]/_AI_METRICS["score_count"] if _AI_METRICS["score_count"] else None
    return {"uptime_minutes":round((time.time()-_AI_METRICS["started_at"])/60,1),"ai_total":total,"approved":approved,"rejected":_AI_METRICS["rejected"],"approval_rate_pct":round(approved/total*100,1) if total else 0.0,"avg_nexus_score":round(avg,2) if avg is not None else None,"top_reject_reasons":_AI_METRICS["reasons"].most_common(5),"decision_sources":_AI_METRICS["sources"].most_common(5),"sides":_AI_METRICS["sides"].most_common(5),"pre_ai_total":_AI_METRICS["funnel_total"],"pre_ai_stages":_AI_METRICS["funnel_stages"].most_common(5),"pre_ai_symbols":_AI_METRICS["funnel_symbols"].most_common(5)}

def get_nexus_metrics():
    with _METRICS_LOCK: return _metrics_snapshot_locked()

def _metrics_summary_text(s):
    reasons=", ".join(f"{k}:{v}" for k,v in s["top_reject_reasons"]) or "nenhum"; stages=", ".join(f"{k}:{v}" for k,v in s["pre_ai_stages"]) or "nenhum"; score=s["avg_nexus_score"]
    return f"📊 NEXUS AI — MÉTRICAS\nDecisões IA: {s['ai_total']}\nAprovadas: {s['approved']}\nVetadas: {s['rejected']}\nTaxa de aprovação: {s['approval_rate_pct']:.1f}%\nScore NEXUS médio: {score if score is not None else 'N/A'}\nPrincipais vetos: {reasons}\nBloqueios pré-IA: {s['pre_ai_total']}\nEstágios pré-IA: {stages}\nJanela: {s['uptime_minutes']} min desde o startup"

def _record_ai_metric(d):
    approved=str(d.get("approved","False")).lower()=="true"
    with _METRICS_LOCK:
        _AI_METRICS["total"]+=1; _AI_METRICS["approved" if approved else "rejected"]+=1; _AI_METRICS["sources"][d.get("source","unknown")]+=1; _AI_METRICS["sides"][d.get("side","unknown")]+=1
        if not approved: _AI_METRICS["reasons"][d.get("reason","unknown")]+=1
        score=_safe_float(d.get("score"))
        if score is not None: _AI_METRICS["score_sum"]+=score; _AI_METRICS["score_count"]+=1
        due=_AI_METRICS["total"]-_AI_METRICS["last_summary_ai_total"]>=_AI_METRICS_EVERY
        if due: _AI_METRICS["last_summary_ai_total"]=_AI_METRICS["total"]; snap=_metrics_snapshot_locked()
        else: snap=None
    if snap: _enqueue(_metrics_summary_text(snap))

def _record_funnel_metric(d):
    with _METRICS_LOCK:
        _AI_METRICS["funnel_total"]+=1; _AI_METRICS["funnel_stages"][d.get("stage","unknown")]+=1; _AI_METRICS["funnel_symbols"][d.get("symbol","unknown")]+=1
        due=_AI_METRICS["funnel_total"]-_AI_METRICS["last_summary_funnel_total"]>=_FUNNEL_METRICS_EVERY
        if due: _AI_METRICS["last_summary_funnel_total"]=_AI_METRICS["funnel_total"]; snap=_metrics_snapshot_locked()
        else: snap=None
    if snap: _enqueue(_metrics_summary_text(snap))

def _tg_worker():
    while True:
        text=_AI_TG_QUEUE.get()
        try:
            # Canonical transport (classified, bounded retry, shared breaker).
            # Class names only: never the exception text, URL or token.
            from bot import telegram_transport
            res=telegram_transport.deliver_sync(text,source="nexus_audit")
            if not res.sent and res.skipped is None:
                _diag(f"Telegram delivery failed: class={res.cls} attempts={res.attempts} status={res.status if res.status is not None else 'NA'}")
        except Exception as exc:
            _diag("Telegram delivery failed", exc)
        finally: _AI_TG_QUEUE.task_done()

def _ensure_tg_worker():
    global _AI_TG_WORKER_STARTED
    if _AI_TG_WORKER_STARTED or not _tg_enabled(): return
    with _AI_TG_LOCK:
        if _AI_TG_WORKER_STARTED: return
        threading.Thread(target=_tg_worker,name="nexus-decision-telegram-audit",daemon=True).start(); _AI_TG_WORKER_STARTED=True

def _enqueue(text):
    global _AUDIT_LAST_ENQUEUE
    now = time.monotonic()
    with _AUDIT_PACING_LOCK:
        if now - _AUDIT_LAST_ENQUEUE < _AUDIT_MIN_INTERVAL:
            return
        _AUDIT_LAST_ENQUEUE = now
    _ensure_tg_worker()
    try: _AI_TG_QUEUE.put_nowait(text)
    except queue.Full: _diag("Telegram observability queue full; message dropped")

class _DecisionTelegramHandler(logging.Handler):
    def emit(self,record):
        try:
            msg=record.getMessage()
            if msg.startswith("[AI_DECISION]"):
                m=_AI_DECISION_RE.match(msg)
                if not m: return
                data=m.groupdict(); _record_ai_metric(data)
                if _tg_enabled() and _ai_tg_should_send(data): _enqueue(_ai_tg_format(data))
                return
            data=_parse_funnel(msg)
            if data:
                _record_funnel_metric(data)
                if _tg_enabled() and _funnel_tg_should_send(data): _enqueue(_funnel_tg_format(data))
        except Exception as exc:
            _diag("Decision telemetry handler failed", exc)

class _RepeatedBlockFilter(logging.Filter):
    """Throttle only identical main-loop integrity-block warnings."""
    def filter(self, record):
        global _BLOCK_LOG_LAST_KEY, _BLOCK_LOG_LAST_TS
        try:
            msg = record.getMessage()
            if not msg.startswith(_BLOCK_LOG_PREFIX):
                return True
            now = time.monotonic()
            with _BLOCK_LOG_LOCK:
                changed = msg != _BLOCK_LOG_LAST_KEY
                due = now - _BLOCK_LOG_LAST_TS >= _BLOCK_LOG_INTERVAL
                if changed or due:
                    _BLOCK_LOG_LAST_KEY = msg
                    _BLOCK_LOG_LAST_TS = now
                    return True
                return False
        except Exception:
            # Observability must fail open: never hide a log if filtering itself
            # malfunctions, and never affect trading/risk state.
            return True

def _make(name):
    lvl=getattr(logging,os.environ.get("LOG_LEVEL","INFO").upper(),logging.INFO); lg=logging.getLogger(name)
    if not lg.handlers:
        lg.setLevel(lvl); h=logging.StreamHandler(sys.stdout); h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s",datefmt="%H:%M:%S")); h.addFilter(_RepeatedBlockFilter()); lg.addHandler(h); lg.addHandler(_DecisionTelegramHandler()); lg.propagate=False
    return lg

log=_make("kakazito-trade")
