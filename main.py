"""
BGX Capital — API Server v12.2 (exchange-selectable)
Única mudança em relação à versão Bybit:
  - Import: KuCoinClient em vez de BybitClient
  - Variável de ambiente: KUCOIN_API_KEY/SECRET/PASSPHRASE
  - Tudo mais (auth, CORS, endpoints, rate limit) idêntico
"""
import asyncio, builtins, os, time
from collections import defaultdict
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# ── ÚNICA LINHA ALTERADA em relação à versão Bybit ───────────────
from bot.exchange import ExchangeClient, PAPER_TRADE, TRADING_MODE_REASON, EXCHANGE_NAME, EXCHANGE_PRODUCT
# ─────────────────────────────────────────────────────────────────

from bot.nexus_runtime_engine import TradingEngine
from bot.config import cfg
from bot.logger import log
from bot import database as db
from bot import runtime_mode_observability as runtime_mode
from bot.startup_block import classify_startup_block, telegram_block_message
from bot.status_observability import enrich_status


# ── Autenticação ──────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)
def _require_auth(credentials: HTTPAuthorizationCredentials = Depends(_bearer)):
    import secrets
    secret = cfg.BOT_API_SECRET
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="BOT_API_SECRET não configurado — configure no Railway antes de usar a API."
        )
    if not credentials or not secrets.compare_digest(credentials.credentials.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="Token inválido ou ausente")


# ── Rate Limiting ─────────────────────────────────────────────────
_rate_counters: dict = defaultdict(list)
def _rate_limit(request: Request):
    ip  = request.client.host if request.client else "unknown"
    now = time.time()
    _rate_counters[ip] = [t for t in _rate_counters[ip] if now - t < 60]
    if len(_rate_counters[ip]) >= 10:
        raise HTTPException(status_code=429, detail="Rate limit — aguarde 1 minuto")
    _rate_counters[ip].append(now)


# ── Lifespan ──────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    BUG CORRIGIDO (deploy travado no Railway):
    'await client.load_instruments()' era executado ANTES do yield. O FastAPI
    só passa a responder requisições depois que o lifespan atinge o yield —
    então o endpoint /health ficava indisponível enquanto o carregamento
    rodava. Qualquer lentidão ou retry na API da KuCoin (o _get tem 3
    tentativas com backoff exponencial) estourava o healthcheckTimeout do
    Railway e o deploy ficava preso em "carregando" até falhar.

    Agora: o servidor sobe imediatamente e /health responde de cara.
    O carregamento de instrumentos vai para uma task de background com
    timeout, e o engine só inicia depois que ela conclui.
    """
    log.info("🚀 BGX Capital v12.2 (%s %s) iniciando...", EXCHANGE_NAME, EXCHANGE_PRODUCT)

    # ══════════════════════════════════════════════════════════════
    # SELF-CHECK DE INTEGRIDADE (previne bugs silenciosos)
    # ══════════════════════════════════════════════════════════════
    _sitecustomize_status = getattr(
        builtins, "_nexus_sitecustomize_status", "not_loaded"
    )
    _report = None
    _selfcheck_error = None
    try:
        from bot.selfcheck import run_selfcheck
        _report = run_selfcheck()
    except Exception as _e:
        _selfcheck_error = _e

    _startup_block = classify_startup_block(
        sitecustomize_status=_sitecustomize_status,
        critical_issues=(_report or {}).get("critical", ()),
        selfcheck_error=_selfcheck_error,
    )
    _blocked_by_selfcheck = _startup_block is not None
    _startup_id = (
        os.environ.get("RAILWAY_DEPLOYMENT_ID")
        or os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        or f"pid-{os.getpid()}"
    )[:16]
    app.state.startup_block = (
        {
            "code": _startup_block.code,
            "detail": _startup_block.detail,
            "startup_id": _startup_id,
        }
        if _startup_block else None
    )
    if _startup_block:
        log.critical(
            "🚫 OPERAÇÃO BLOQUEADA: code=%s startup_id=%s detail=%s",
            _startup_block.code,
            _startup_id,
            _startup_block.detail,
        )
    if PAPER_TRADE:
        log.warning("🟡 PAPER TRADE MODE ATIVO")

    client = ExchangeClient()
    engine = TradingEngine(client)
    app.state.client = client
    app.state.engine = engine
    app.state.ready  = False

    async def _bootstrap():
        """Carrega instrumentos e inicia o engine — fora do caminho do healthcheck."""
        try:
            await asyncio.wait_for(client.load_instruments(), timeout=45)
            log.info("✅ Instrumentos carregados")
        except asyncio.TimeoutError:
            log.error("⏱️ load_instruments excedeu 45s — seguindo mesmo assim")
        except Exception as e:
            log.error(f"❌ load_instruments falhou: {e} — seguindo mesmo assim")

        # Binance private-auth startup probe: signed READ-ONLY request.
        # This proves API key/secret/signature/timestamp validity without
        # creating, cancelling, or modifying any exchange order. Failure is
        # telemetry-only while PAPER is active; LIVE release gates remain
        # authoritative and unchanged.
        if EXCHANGE_NAME == "binance":
            try:
                await asyncio.wait_for(client.sync_time(), timeout=5)
                _auth_rows = await asyncio.wait_for(
                    client._get("/fapi/v3/balance", auth=True), timeout=10
                )
                if isinstance(_auth_rows, list):
                    log.info(
                        "[BINANCE_PRIVATE_AUTH] status=PASS "
                        "endpoint=/fapi/v3/balance http=200 response=list "
                        "values_redacted=true execution_effect=NONE"
                    )

                    # Read-only execution-readiness probe. This intentionally
                    # avoids account-wide open-order queries (weight 40) and
                    # performs no exchange mutation.
                    try:
                        _probe_symbol = "BTCUSDT"
                        _position_mode = await asyncio.wait_for(
                            client.get_position_mode(), timeout=8
                        )
                        _account_state = await asyncio.wait_for(
                            client.get_account_state(), timeout=8
                        )
                        _positions = await asyncio.wait_for(
                            client.get_positions(), timeout=8
                        )
                        _normal_orders = await asyncio.wait_for(
                            client.get_open_orders(_probe_symbol), timeout=8
                        )
                        _algo_orders = await asyncio.wait_for(
                            client.get_stop_orders(_probe_symbol), timeout=8
                        )
                        _brackets = await asyncio.wait_for(
                            client.get_leverage_brackets(_probe_symbol), timeout=8
                        )
                        _mode_ok = _position_mode == "ONE_WAY"
                        _state_ok = isinstance(_account_state, dict)
                        _positions_ok = isinstance(_positions, list)
                        _normal_ok = isinstance(_normal_orders, list)
                        _algo_ok = isinstance(_algo_orders, list)
                        _brackets_ok = bool(
                            isinstance(_brackets, dict)
                            and isinstance(_brackets.get("brackets"), list)
                            and _brackets.get("brackets")
                        )
                        _ready = all(
                            (
                                _mode_ok,
                                _state_ok,
                                _positions_ok,
                                _normal_ok,
                                _algo_ok,
                                _brackets_ok,
                            )
                        )
                        log.log(
                            20 if _ready else 30,
                            "[BINANCE_READINESS] status=%s position_mode=%s "
                            "account_state=%s positions_read=%s normal_orders_read=%s "
                            "algo_orders_read=%s leverage_brackets_read=%s "
                            "probe_symbol=%s mutation=false execution_effect=%s",
                            "PASS" if _ready else "BLOCK_LIVE_RELEASE",
                            _position_mode,
                            _state_ok,
                            _positions_ok,
                            _normal_ok,
                            _algo_ok,
                            _brackets_ok,
                            _probe_symbol,
                            "NONE" if _ready else "BLOCK_LIVE_RELEASE",
                        )
                    except Exception as _readiness_error:
                        log.warning(
                            "[BINANCE_READINESS] status=FAIL error_type=%s "
                            "detail=%s mutation=false execution_effect=BLOCK_LIVE_RELEASE",
                            type(_readiness_error).__name__,
                            str(_readiness_error).replace("\n", " ")[:180],
                        )
                else:
                    log.warning(
                        "[BINANCE_PRIVATE_AUTH] status=FAIL "
                        "endpoint=/fapi/v3/balance reason=unexpected_response "
                        "values_redacted=true execution_effect=NONE"
                    )
            except Exception as _e:
                # BinanceClient RuntimeError messages contain only HTTP/code/msg;
                # credential/signature material is never included by _request.
                _auth_error = str(_e).replace("\n", " ")[:240]
                _classification = "FUTURES_PRIVATE_REJECTED"
                _spot_http = None
                _spot_code = None

                # If Futures returns -2015, run one additional signed READ-ONLY
                # Spot account probe. This separates a Futures-scope problem
                # from key/IP/environment problems without logging account data.
                if "code=-2015" in _auth_error:
                    try:
                        import aiohttp as _aiohttp

                        from bot import binance as _binance_module
                        _key = _binance_module.API_KEY
                        _params = client._signed_params({})
                        _timeout = _aiohttp.ClientTimeout(total=8)
                        async with _aiohttp.ClientSession(timeout=_timeout) as _session:
                            async with _session.get(
                                "https://api.binance.com/api/v3/account",
                                params=_params,
                                headers={"X-MBX-APIKEY": _key},
                            ) as _resp:
                                _spot_http = _resp.status
                                try:
                                    _payload = await _resp.json(content_type=None)
                                except Exception:
                                    _payload = {}
                                if isinstance(_payload, dict):
                                    _spot_code = _payload.get("code")

                        if _spot_http == 200:
                            _classification = "FUTURES_PERMISSION_OR_ACCOUNT_SCOPE"
                        elif _spot_code == -2015:
                            _classification = "API_KEY_IP_OR_ENVIRONMENT"
                        elif _spot_code == -1022:
                            _classification = "API_SECRET_OR_SIGNATURE"
                        elif _spot_code == -1021:
                            _classification = "TIMESTAMP_OR_RECV_WINDOW"
                        else:
                            _classification = "SPOT_DIAGNOSTIC_INCONCLUSIVE"
                    except Exception:
                        _classification = "SPOT_DIAGNOSTIC_FAILED"

                log.warning(
                    "[BINANCE_PRIVATE_AUTH] status=FAIL "
                    "endpoint=/fapi/v3/balance error_type=%s detail=%s "
                    "classification=%s spot_http=%s spot_code=%s "
                    "values_redacted=true execution_effect=NONE",
                    type(_e).__name__, _auth_error, _classification,
                    _spot_http, _spot_code,
                )

        app.state.ready = True

        if _blocked_by_selfcheck:
            app.state.blocked = True
            log.critical(
                "🚫 Engine NÃO iniciado — code=%s startup_id=%s",
                _startup_block.code,
                _startup_id,
            )
            try:
                from bot.notifier import notify as _n
                await _n(telegram_block_message(_startup_block, _startup_id))
            except Exception as _e:
                log.warning("startup block notify failed: %s", type(_e).__name__)
            return

        # Verifica o canal do Telegram ANTES de iniciar o engine.
        # Falha aqui não impede a operação — apenas registra o motivo.
        try:
            from bot.notifier import test_telegram
            app.state.telegram = await asyncio.wait_for(test_telegram(), timeout=10)
        except Exception as _e:
            log.warning(f"test_telegram: {_e}")
            app.state.telegram = {"ok": False, "reason": str(_e)}

        app.state.blocked = False
        app.state.engine_task = asyncio.create_task(engine.run())
        log.info("✅ BGX Capital online (%s %s)", EXCHANGE_NAME, EXCHANGE_PRODUCT)

        # Mensagem de startup deriva do estado operacional real. Em especial,
        # PAPER_TRADE=false + VALIDATION_LOCK agora é SHADOW_LIVE e nunca
        # anuncia que ordens reais serão enviadas à exchange.
        try:
            from bot.notifier import notify as _n
            _mode = runtime_mode.snapshot(
                paper_trade=PAPER_TRADE,
                engine=engine,
                blocked=bool(getattr(app.state, "blocked", False)),
                mode_reason=TRADING_MODE_REASON,
            )
            await _n(runtime_mode.startup_message(_mode))
        except Exception as _e:
            log.debug(f"notify modo: {_e}")

    app.state.bootstrap_task = asyncio.create_task(_bootstrap())

    # yield IMEDIATO — /health passa a responder agora, sem esperar a KuCoin
    yield

    # ── Shutdown ordenado ─────────────────────────────────────────
    # P2 CORRIGIDO: a conexão com o banco nunca era fechada. Em restarts
    # frequentes, conexões PostgreSQL ficavam penduradas até o timeout do
    # servidor, consumindo slots do pool.
    # Fail closed before cleanup: a shutting-down engine must never remain
    # published as the HTTP new-risk readiness authority.
    from bot.execution_ownership import invalidate_local_execution_ownership
    invalidate_local_execution_ownership(
        engine, event="local_invalidated", reason="shutdown"
    )
    app.state.ready = False
    app.state.engine = None
    engine.stop()
    for t in ("bootstrap_task", "engine_task"):
        task=getattr(app.state,t,None)
        if task and not task.done():
            task.cancel()
    tasks = [getattr(app.state, name, None) for name in ('bootstrap_task', 'engine_task')]
    await asyncio.gather(*(t for t in tasks if t is not None), return_exceptions=True)
    try:
        await client.close()
    except Exception as e:
        log.warning(f"Erro ao fechar cliente: {e}")
    try:
        await db.close()
    except Exception as e:
        log.warning(f"Erro ao fechar banco: {e}")
    log.info("👋 Encerrado")


app = FastAPI(title=f"BGX Capital {EXCHANGE_NAME}", version="12.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)


# ── Health ────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """
    Liveness do Railway sem I/O externo. O HTTP continua 200 enquanto o
    processo está vivo, mas `ready` representa prontidão operacional real do
    engine, não apenas o término do bootstrap. O modo e a capacidade de envio
    de ordens também respeitam o VALIDATION_LOCK/SHADOW.
    """
    eng = getattr(app.state, "engine", None)
    runtime = runtime_mode.snapshot(
        paper_trade=PAPER_TRADE,
        engine=eng,
        blocked=bool(getattr(app.state, "blocked", False)),
        mode_reason=TRADING_MODE_REASON,
    )
    return {
        "status":   "ok",
        "version":  "12.2.0",
        "exchange": EXCHANGE_NAME,
        "ready":    runtime["ready"],
        "connected": runtime["connected"],
        "active": runtime["active"],
        "blocked":  bool(getattr(app.state, "blocked", False)),
        "startup_block": getattr(app.state, "startup_block", None),
        "telegram":     getattr(app.state, "telegram", {"ok": None}),
        "trading_mode": runtime["trading_mode"],
        "mode_reason":  runtime["mode_reason"],
        "validation_lock": runtime["validation_lock"],
        "shadow_readonly_ready": runtime["shadow_readonly_ready"],
        "orders_sent_to_exchange": runtime["orders_sent_to_exchange"],
    }

@app.get("/")
async def root():
    return {"status": "online", "version": "12.2.0", "exchange": EXCHANGE_NAME}


# ── Status / Saldo / Posições ─────────────────────────────────────
@app.get("/api/status", dependencies=[Depends(_require_auth)])
async def status():
    return enrich_status(app.state.engine, app.state.engine.get_status())

@app.get("/api/balance", dependencies=[Depends(_require_auth)])
async def balance():
    b = await app.state.client.get_balance()
    return {"balance": b, "currency": "USDT", "exchange": EXCHANGE_NAME}

@app.get("/api/positions", dependencies=[Depends(_require_auth)])
async def positions():
    eng = app.state.engine
    return {
        "open":     [p.to_dict() for p in eng.positions.values()],
        "count":    len(eng.positions),
        "exchange": EXCHANGE_NAME,
    }


# ── Controle ──────────────────────────────────────────────────────
@app.post("/api/pause", dependencies=[Depends(_require_auth), Depends(_rate_limit)])
async def pause(request: Request):
    app.state.engine.pause_entries()
    return {"message": "Novas entradas pausadas; posições continuam sendo gerenciadas"}

@app.post("/api/resume", dependencies=[Depends(_require_auth), Depends(_rate_limit)])
async def resume(request: Request):
    if getattr(app.state, 'blocked', True) or not getattr(app.state, 'ready', False):
        raise HTTPException(status_code=503, detail='Startup safety checks are not ready')
    engine=app.state.engine
    task=getattr(app.state,"engine_task",None)
    if task and not task.done():
        if not getattr(engine, '_running', False):
            raise HTTPException(status_code=503, detail='Engine shutdown in progress')
        engine.active=True
        engine.resume_entries()
        return {"message":"Bot reativado"}
    if getattr(engine, '_running', False):
        raise HTTPException(status_code=503, detail='Engine task failed; service restart required')
    engine.resume_entries()
    app.state.engine_task=asyncio.create_task(engine.run())
    return {"message":"Bot retomado"}

@app.post("/api/close-all", dependencies=[Depends(_require_auth), Depends(_rate_limit)])
async def close_all(request: Request):
    engine=app.state.engine
    engine.stop()
    result=await engine.close_all_positions()
    return {"message":f"Emergency close: {result['closed']} posições fechadas",**result}


# ── PnL / Stats ───────────────────────────────────────────────────
@app.get("/api/pnl", dependencies=[Depends(_require_auth)])
async def pnl():
    return app.state.engine.stats.all_summaries()

@app.post("/api/test-telegram", dependencies=[Depends(_require_auth)])
async def test_telegram_endpoint():
    """Envia uma mensagem de teste ao Telegram e reporta o resultado."""
    from bot.notifier import test_telegram, notify
    res=await test_telegram()
    if res.get("ok"):
        await notify(
            "🧪 *TESTE DE CONEXÃO*\n"
            "Se você está lendo isto, o canal está funcionando."
        )
        res["mensagem_enviada"]=True
    return res


@app.get("/api/integrity", dependencies=[Depends(_require_auth)])
async def integrity_status():
    """
    Estado do kill switch de integridade (Fase 3, P0).

    Responde: o bot PODE abrir novas posições agora? Se não, por quê?
    """
    eng=app.state.engine
    g=getattr(eng,"integrity",None)
    if g is None:
        return {"error":"IntegrityGuard não inicializado"}
    d=g.to_dict()
    d["orders_tracked"]=len(getattr(eng,"orders",[]) or [])
    return d


@app.get("/api/liquidation-check", dependencies=[Depends(_require_auth)])
async def liquidation_check(entry: float, stop: float, leverage: int = 0,
                            side: str = "LONG"):
    """
    Calcula se um stop é efetivo para dado leverage, incluindo margem de
    manutenção, taxas e slippage. Útil para dimensionar o leverage antes
    de configurá-lo.
    """
    from bot import liquidation as liq
    lev = leverage or cfg.LEVERAGE
    a = liq.analyze(entry, stop, lev, side.upper() == "LONG")
    out = a.to_dict()
    out["leverage"] = lev
    out["max_leverage_para_este_stop"] = liq.max_leverage_for_stop(a.stop_move_pct)
    return out


@app.get("/api/why-no-trade", dependencies=[Depends(_require_auth)])
async def why_no_trade():
    """
    Responde diretamente: por que o bot não está operando?

    Mostra o modo de operação, os thresholds ativos e a distribuição
    dos scores recentes — separando "não há setup bom" (mercado) de
    "algo está bloqueando" (configuração ou bug).
    """
    eng = app.state.engine
    mn  = cfg.MIN_ENTRY_SCORE
    runtime = runtime_mode.snapshot(
        paper_trade=PAPER_TRADE,
        engine=eng,
        blocked=bool(getattr(app.state, "blocked", False)),
        mode_reason=TRADING_MODE_REASON,
    )

    # Buffer do strategy: registra TODO score avaliado, não só os aprovados
    try:
        from bot.strategy import get_score_log
        _log   = get_score_log(300)
        scores = [x["score"] for x in _log]
    except Exception:
        _log   = []
        scores = list(getattr(eng, "_score_hist", []))

    if scores:
        dist = {
            "avaliados":       len(scores),
            "maximo":          max(scores),
            "media":           round(sum(scores) / len(scores), 1),
            "acima_do_minimo": len([s for s in scores if s >= mn]),
            "a_5pts_ou_menos": len([s for s in scores if mn - 5 <= s < mn]),
        }
        if dist["acima_do_minimo"] > 0:
            veredito = "Sinais passaram o score — verifique vetos do NEXUS AI e do Risk Engine"
        elif dist["maximo"] >= mn - 5:
            veredito = f"Chegou perto (máx {dist['maximo']}), mas nenhum atingiu {mn}"
        else:
            veredito = f"Nenhum setup próximo do mínimo (máx {dist['maximo']} vs {mn})"
    else:
        dist = {"avaliados": 0}
        veredito = "Nenhum sinal avaliado ainda — bot pode ter acabado de subir"

    return {
        "ultimos_avaliados": [
            {"par": x["symbol"], "score": x["score"],
             "4H": x["s4h"], "1H": x["s1h"], "15M": x["s15"]}
            for x in _log[:10]
        ],
        "modo":            runtime["trading_mode"],
        "ordens_reais":    runtime["orders_sent_to_exchange"],
        "ready":           runtime["ready"],
        "validation_lock": runtime["validation_lock"],
        "thresholds": {
            "score_minimo":  mn,
            "nexus_minimo":  float(os.environ.get("NEXUS_MIN_SCORE", "55")),
            "rr_minimo":     cfg.MIN_RR_RATIO,
            "sl_max_pct":    round(100 / max(1, cfg.LEVERAGE) * 0.75, 2),
            "filtro_liquidacao_ativo": os.environ.get(
                "ALLOW_SL_BEYOND_LIQUIDATION", "false").lower() != "true",
            "liquidacao_pct": round(100 / max(1, cfg.LEVERAGE), 2),
        },
        "scores_recentes": dist,
        "posicoes_abertas": len(getattr(eng, "positions", {})),
        "max_posicoes":     cfg.MAX_POSITIONS,
        "pares_monitorados": len(getattr(eng, "viable_symbols", [])),
        "veredito":         veredito,
    }


@app.get("/api/selfcheck", dependencies=[Depends(_require_auth)])
async def selfcheck():
    """
    Verificação de integridade do código sob demanda.

    Detecta NameError/AttributeError latentes, métodos duplicados e
    combinações de configuração impossíveis — a classe de bug que
    historicamente passou despercebida por horas em produção.
    """
    from bot.selfcheck import run_selfcheck
    rep = run_selfcheck(verbose=False)
    return {
        "status":         "BLOCKED" if rep["critical"] else "OK",
        "files_checked":  rep["files_checked"],
        "critical_count": len(rep["critical"]),
        "critical":       rep["critical"],
        "warnings":       rep["warning"],
        "engine_blocked": bool(getattr(app.state, "blocked", False)),
        "startup_block":  getattr(app.state, "startup_block", None),
    }


@app.get("/api/nexus/{symbol}", dependencies=[Depends(_require_auth)])
async def nexus_decision(symbol: str, direction: str = "LONG"):
    """
    NEXUS AI — decisão completa para um símbolo (seções 18, 23).

    Retorna a estrutura padronizada com score, regime, EV, R:R líquido,
    reasoning, warnings e a saída de cada um dos 7 modelos do ensemble.
    Útil para entender POR QUE a IA aprovou ou vetou um setup.
    """
    from bot import nexus_ai
    eng = app.state.engine
    sym = symbol.upper()
    try:
        k15 = eng.client.get_cached_klines(sym, "15",  200)
        k1h = eng.client.get_cached_klines(sym, "60",  100)
        k4h = eng.client.get_cached_klines(sym, "240",  60)
        if len(k15) < 60:
            k15 = await eng.client.get_klines(sym, "15", 200)
        if len(k1h) < 40:
            k1h = await eng.client.get_klines(sym, "60", 100)
        if len(k4h) < 20:
            k4h = await eng.client.get_klines(sym, "240", 60)

        px = float(k15[-1]["c"]) if k15 else 0.0
        if direction.upper() == "LONG":
            sl, tp = px * 0.985, px * 1.03
        else:
            sl, tp = px * 1.015, px * 0.97

        funding = None
        try:
            funding = await eng.client.get_funding_rate(sym)
        except Exception as _e:
            log.debug(f"endpoint /api/nexus: funding indisponível: {_e}")

        d = nexus_ai.decide(
            symbol=sym, k15=k15, k1h=k1h, k4h=k4h,
            entry=px, sl=sl, tp=tp,
            ticker=eng.client.get_cached_ticker(sym) or None,
            funding=funding,
        )
        return d.to_dict()
    except Exception as e:
        return {"symbol": sym, "error": str(e),
                "decision": "WAIT", "execution_allowed": False}


@app.get("/api/nexus", dependencies=[Depends(_require_auth)])
async def nexus_last():
    """Últimas decisões do NEXUS AI por símbolo (seção 23)."""
    eng = app.state.engine
    return {
        "enabled":   getattr(eng, "_last_nexus", None) is not None,
        "min_score": float(os.environ.get("NEXUS_MIN_SCORE", "85")),
        "decisions": getattr(eng, "_last_nexus", {}),
    }


@app.get("/api/expectancy", dependencies=[Depends(_require_auth)])
async def expectancy(days: int = 0):
    """
    ITEM 4: métricas que realmente medem edge — não o win rate isolado.

    Retorna expectancy em R, payoff ratio, profit factor e o win rate de
    BREAKEVEN para o payoff observado. A comparação win_rate vs
    breakeven_wr é o que diz se a estratégia é lucrativa.

    Exemplos:
      40% acerto, payoff 2.0 → breakeven 33% → lucrativo (+7pp)
      90% acerto, payoff 0.3 → breakeven 77% → lucrativo (+13pp) mas com
      expectancy MENOR e cauda de perdas muito pior.
    """
    return await db.get_expectancy_stats(days or None)


@app.get("/api/db-stats", dependencies=[Depends(_require_auth)])
async def db_stats():
    return await db.get_stats()


# ── Score ─────────────────────────────────────────────────────────
@app.get("/api/score/{symbol}", dependencies=[Depends(_require_auth)])
async def score_endpoint(symbol: str, direction: str = "LONG"):
    from bot import score as scoring
    try:
        kl = app.state.client.get_cached_klines(symbol.upper(), "15", 50)
        if len(kl) < 10:
            kl = await app.state.client.get_klines(symbol.upper(), "15", 50)
        c = [k["c"] for k in kl]
        h = [k["h"] for k in kl]
        l = [k["l"] for k in kl]
        v = [k["v"] for k in kl]
        result = await scoring.calculate(
            symbol.upper(), direction.upper(), c, h, l, v, app.state.client,
        )
        return result
    except Exception as e:
        return {"error": str(e), "total": 0, "aprovado": False}


# ── Market data ───────────────────────────────────────────────────
@app.get("/api/market/{symbol}", dependencies=[Depends(_require_auth)])
async def market(symbol: str):
    sym = symbol.upper()
    try:
        oi  = await app.state.client.get_open_interest(sym)
        fr  = await app.state.client.get_funding_rate(sym)
        tk  = app.state.client.get_cached_ticker(sym)
        return {"symbol": sym, "open_interest": oi, "funding_rate": fr, "ticker": tk}
    except Exception as e:
        return {"error": str(e)}


# ── Correlação ────────────────────────────────────────────────────
@app.get("/api/correlation", dependencies=[Depends(_require_auth)])
async def correlation():
    from bot import correlation as corr_guard
    symbols = app.state.engine.viable_symbols[:8]
    return {
        "matrix":    corr_guard.get_correlation_matrix(symbols),
        "symbols":   symbols,
        "threshold": cfg.MAX_CORRELATION,
    }


# ── WS Cache stats ────────────────────────────────────────────────
@app.get("/api/ws-stats", dependencies=[Depends(_require_auth)])
async def ws_stats():
    return app.state.client.get_cache_stats()


# ── Dashboard ─────────────────────────────────────────────────────
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    try:
        with open("dashboard/index.html") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>Dashboard não encontrado</h1>", status_code=404)


# ── Backtest manual ───────────────────────────────────────────────
@app.post("/api/backtest", dependencies=[Depends(_require_auth)])
async def trigger_backtest():
    # weekly_backtest_loop never returns; each call used to start another
    # permanent loop task. Keep at most one alive per process.
    running = getattr(app.state, "backtest_task", None)
    if running is not None and not running.done():
        return {"message": "Backtest já em execução", "started": False}
    from bot import backtest as bt
    app.state.backtest_task = asyncio.create_task(bt.weekly_backtest_loop(app.state.client))
    return {"message": "Backtest iniciado", "started": True}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=cfg.PORT,
                log_level=cfg.LOG_LEVEL.lower())
