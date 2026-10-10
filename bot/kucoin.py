"""
BGX Capital — KuCoin Futures Client v1.0
Substitui bot/bybit.py completamente.

Implementa a mesma interface pública do BybitClient:
  - get_balance()
  - place_order(symbol, side, qty, sl, tp, instruments)
  - set_sl(symbol, sl, instruments)
  - cancel_all_orders(symbol)
  - get_klines(symbol, interval, limit)
  - get_cached_klines(symbol, interval, limit)
  - get_ticker(symbol)
  - get_cached_ticker(symbol)
  - get_open_interest(symbol)
  - get_funding_rate(symbol)
  - get_orderbook(symbol)
  - get_positions()
  - set_leverage(symbol, leverage)
  - start_websocket(symbols, intervals)
  - get_cache_stats()
  - close()

KuCoin Futures API v1: https://docs.kucoin.com/futures
WebSocket: wss://ws-api.kucoin.com/endpoint (token-based)

Diferenças-chave KuCoin vs Bybit:
  - Autenticação: KC-API-KEY, KC-API-SIGN, KC-API-TIMESTAMP, KC-API-PASSPHRASE
  - WS requer token REST obtido via POST /api/v1/bullet-public (ou private)
  - Símbolo: XBTUSDTM (BTC), ETHUSDTM (ETH) — sufixo M para USDT margined
  - Leverage: POST /api/v1/position/margin/auto-deposit-status (diferente da Bybit)
  - Funding a cada 8h (igual Bybit)
  - OrderLinkId: clientOid (máx 40 chars, apenas alfanumérico + hífen)
"""

import asyncio, hashlib, hmac, json, math, os, time
from base64 import b64encode
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional

import aiohttp

from bot.logger import log
from bot.execution_capability import assert_exchange_mutation_allowed
from bot.order_state import OrderState, InvalidTransition
from bot.quantity import base_to_contracts

# ── Credenciais ────────────────────────────────────────────────────
# .strip() automático — espaços acidentais no Railway são a causa #1 de 400004
API_KEY        = os.environ.get("KUCOIN_API_KEY",        "").strip()
API_SECRET     = os.environ.get("KUCOIN_API_SECRET",     "").strip()
API_PASSPHRASE = os.environ.get("KUCOIN_API_PASSPHRASE", "").strip()
# OPERAÇÃO REAL ATIVADA — autenticação KuCoin confirmada (saldo real lido com sucesso).
# ══════════════════════════════════════════════════════════════════
# BARREIRA EXPLÍCITA: PAPER TRADING ↔ DINHEIRO REAL (seção 24)
#
# P0 CORRIGIDO: o default era "false", ou seja, a AUSÊNCIA da variável
# fazia o bot operar com CAPITAL REAL. Uma variável apagada por engano,
# um ambiente novo ou um deploy sem config resultaria em ordens reais
# sem ninguém ter pedido.
#
# Agora a operação real exige AFIRMAÇÃO EXPLÍCITA de duas variáveis:
#     PAPER_TRADE=false
#     LIVE_TRADING_CONFIRMED=I_UNDERSTAND_THE_RISK
#
# Qualquer configuração ambígua ou ausente → PAPER TRADE.
# Falha para o lado seguro, sempre.
# ══════════════════════════════════════════════════════════════════
_paper_env = os.environ.get("PAPER_TRADE", "").strip().lower()
_live_ack  = os.environ.get("LIVE_TRADING_CONFIRMED", "").strip()
_LIVE_TOKEN = "I_UNDERSTAND_THE_RISK"

# Motivo legível do modo escolhido — exposto em /health e /api/status
# para que "por que não abre ordens?" seja respondível em 5 segundos.
# bot.kucoin is imported unconditionally by the runtime bootstrap for
# compatibility wrappers. Only claim KuCoin as the order venue when it is the
# active EXCHANGE; bot.exchange emits the venue-correct LIVE banner.
_ACTIVE_EXCHANGE = os.environ.get("EXCHANGE", "kucoin").strip().lower().replace("-", "_")
_KUCOIN_IS_ACTIVE = _ACTIVE_EXCHANGE in {"kucoin", "kucoin_futures"}

if _paper_env == "false" and _live_ack == _LIVE_TOKEN:
    PAPER_TRADE = False
    TRADING_MODE_REASON = "OPERAÇÃO REAL — confirmada por PAPER_TRADE=false + LIVE_TRADING_CONFIRMED"
    if _KUCOIN_IS_ACTIVE:
        log.critical("=" * 62)
        log.critical("🔴 OPERAÇÃO REAL ATIVA — ordens serão enviadas à KuCoin")
        log.critical("=" * 62)
    else:
        log.info(
            "[KUCOIN_MODULE] loaded_for_compatibility=true active_exchange=%s "
            "kucoin_orders=false execution_effect=NONE",
            _ACTIVE_EXCHANGE,
        )

elif _paper_env == "false":
    PAPER_TRADE = True
    TRADING_MODE_REASON = (
        "PAPER — PAPER_TRADE=false mas falta LIVE_TRADING_CONFIRMED"
    )
    log.critical("=" * 62)
    log.critical("🚫 NENHUMA ORDEM SERÁ ENVIADA À EXCHANGE")
    log.critical("")
    log.critical("   Você definiu PAPER_TRADE=false, mas falta a segunda")
    log.critical("   confirmação. O bot vai analisar e simular, sem operar.")
    log.critical("")
    log.critical("   PARA OPERAR DE VERDADE, adicione no Railway:")
    log.critical(f"      LIVE_TRADING_CONFIRMED={_LIVE_TOKEN}")
    log.critical("=" * 62)

elif _paper_env == "true":
    PAPER_TRADE = True
    TRADING_MODE_REASON = "PAPER — PAPER_TRADE=true (simulação pedida)"
    log.warning("🟡 PAPER TRADE ativo por configuração — ordens simuladas")

else:
    PAPER_TRADE = True
    TRADING_MODE_REASON = (
        f"PAPER — PAPER_TRADE ausente ou inválido ('{_paper_env}')"
    )
    log.critical("=" * 62)
    log.critical("🚫 NENHUMA ORDEM SERÁ ENVIADA À EXCHANGE")
    log.critical("")
    log.critical(f"   PAPER_TRADE não está definido (valor lido: '{_paper_env}')")
    log.critical("   Por segurança, o bot assume simulação.")
    log.critical("")
    log.critical("   PARA OPERAR DE VERDADE, adicione no Railway:")
    log.critical("      PAPER_TRADE=false")
    log.critical(f"      LIVE_TRADING_CONFIRMED={_LIVE_TOKEN}")
    log.critical("=" * 62)

# ── Endpoints ─────────────────────────────────────────────────────
# Configuráveis para permitir testes de integração contra um mock
# ou apontar para a testnet sem alterar código.
REST_BASE = os.environ.get("KUCOIN_REST_BASE", "https://api-futures.kucoin.com")
WS_BASE   = os.environ.get("KUCOIN_WS_BASE",   "wss://ws-api.kucoin.com/endpoint")

# ── Constantes ────────────────────────────────────────────────────
# Taxa taker KuCoin Futures: 0.06% (auditoria #8 — Bybit era 0.055%)
# Configurável caso sua conta tenha tier de taxa diferente.
TAKER_FEE = float(os.environ.get("TAKER_FEE", "0.0006"))
MAKER_FEE = float(os.environ.get("MAKER_FEE", "0.0002"))

# ── Mapa de símbolos Bybit → KuCoin ──────────────────────────────
# KuCoin Futures USDT-margined usa sufixo "M" (ex: XBTUSDTM, ETHUSDTM)
# ── Mapa de símbolos: padrão (BTCUSDT) ↔ KuCoin (XBTUSDTM) ──────
#
# CORRIGIDO: antes era hardcoded e continha símbolos que NÃO EXISTEM
# na KuCoin Futures (ex: BNBUSDTM). Símbolos inexistentes faziam a API
# retornar erro e o par era silenciosamente ignorado — por isso apenas
# alguns pares (como SOL) apareciam nos logs.
#
# Agora o mapa é construído DINAMICAMENTE em load_instruments() a partir
# de /api/v1/contracts/active, garantindo que só existam pares reais.
#
# Exceções conhecidas de nomenclatura KuCoin:
#   BTC  → XBT   (KuCoin usa o código XBT para Bitcoin)
_BASE_ALIAS = {
    "BTC": "XBT",   # KuCoin nomeia Bitcoin como XBT
}
_BASE_ALIAS_REV = {v: k for k, v in _BASE_ALIAS.items()}

# Preenchidos dinamicamente por KuCoinClient.load_instruments()
SYMBOL_MAP: dict     = {}
SYMBOL_MAP_REV: dict = {}


def _guess_kucoin(symbol: str) -> str:
    """
    Converte BTCUSDT → XBTUSDTM sem consultar a API.
    Usado apenas como fallback antes do mapa dinâmico ser carregado.
    """
    if not symbol.endswith("USDT"):
        return symbol
    base = symbol[:-4]                      # BTCUSDT → BTC
    base = _BASE_ALIAS.get(base, base)      # BTC → XBT
    return f"{base}USDTM"


def to_kucoin(symbol: str) -> str:
    """Converte símbolo padrão (BTCUSDT) → KuCoin (XBTUSDTM)."""
    if symbol in SYMBOL_MAP:
        return SYMBOL_MAP[symbol]
    return _guess_kucoin(symbol)


def to_standard(symbol: str) -> str:
    """Converte símbolo KuCoin (XBTUSDTM) → padrão (BTCUSDT)."""
    if symbol in SYMBOL_MAP_REV:
        return SYMBOL_MAP_REV[symbol]
    if symbol.endswith("USDTM"):
        base = symbol[:-5]                       # XBTUSDTM → XBT
        base = _BASE_ALIAS_REV.get(base, base)   # XBT → BTC
        return f"{base}USDT"
    return symbol

# ── Intervalo: Bybit → KuCoin ────────────────────────────────────
# Formato do intervalo nos tópicos WS da KuCoin: _1min, _15min, _1hour, _4hour
# CORRIGIDO: o código usava números crus (_15, _60), que a KuCoin rejeita.
WS_INTERVAL_MAP = {
    "1":   "1min",
    "3":   "3min",
    "5":   "5min",
    "15":  "15min",
    "30":  "30min",
    "60":  "1hour",
    "120": "2hour",
    "240": "4hour",
    "480": "8hour",
    "720": "12hour",
    "D":   "1day",
    "W":   "1week",
}
WS_INTERVAL_MAP_REV = {v: k for k, v in WS_INTERVAL_MAP.items()}

INTERVAL_MAP = {
    "1": 1, "3": 3, "5": 5, "15": 15, "30": 30,
    "60": 60, "120": 120, "240": 240, "D": 1440, "W": 10080,
}


class KuCoinClient:
    """
    Cliente KuCoin Futures com interface idêntica ao BybitClient.
    Substitui bot/bybit.py sem alterar engine.py, strategy.py ou qualquer
    outro módulo — apenas a variável de import muda.
    """

    def __init__(self):
        self._session:   Optional[aiohttp.ClientSession] = None
        self._ws_retry:  int  = 0
        self._ws_token:  str  = ""
        self._ws_token_ts: float = 0.0   # timestamp da obtenção do token WS
        # Versão da API Key: "2" = passphrase assinada | "1" = texto plano
        # Fallback automático para "1" se receber 400004 com "2"
        self._api_version: str = os.environ.get("KUCOIN_API_VERSION", "2")
        self._version_fallback_done: bool = False
        # Offset entre relógio local e servidor KuCoin (ms).
        # KuCoin rejeita requests com timestamp fora de ±5s → 400005 Invalid SIGN.
        self._time_offset_ms: int = 0
        self._time_synced: bool = False
        self._sign_logged: bool = False   # loga a primeira assinatura para diagnóstico

        # Diagnóstico de credenciais no startup
        # Aviso explícito do modo de operação
        if PAPER_TRADE:
            log.warning("🟡 PAPER TRADE — ordens simuladas, capital NÃO é usado")
        else:
            log.warning("🔴 OPERAÇÃO REAL ATIVA — ordens serão executadas com CAPITAL REAL")

        if API_KEY:
            log.info(f"🔑 KuCoin API Key: {API_KEY[:6]}...{API_KEY[-4:]} ({len(API_KEY)} chars)")
            log.info(
                f"🔑 API Secret: {len(API_SECRET)} chars | "
                f"Passphrase: {len(API_PASSPHRASE)} chars | "
                f"API version: v{self._api_version}"
            )
            if not API_PASSPHRASE:
                log.error("❌ KUCOIN_API_PASSPHRASE VAZIA — configure no Railway!")
            if not API_SECRET:
                log.error("❌ KUCOIN_API_SECRET VAZIA — configure no Railway!")
            # Detecta espaços acidentais (causa comum de 400004)
            if API_PASSPHRASE != API_PASSPHRASE.strip():
                log.error("❌ Passphrase tem ESPAÇOS no início/fim — remova no Railway!")
            if API_SECRET != API_SECRET.strip():
                log.error("❌ API Secret tem ESPAÇOS no início/fim — remova no Railway!")
            # Alertar sobre caracteres problemáticos na passphrase
            import string
            special = [c for c in API_PASSPHRASE if c not in string.ascii_letters + string.digits + "_-!@#$%"]
            if special:
                log.warning(f"⚠️  Passphrase contém chars especiais: {special} — pode causar 400004")
        else:
            log.warning("⚠️  KUCOIN_API_KEY não configurado")

        # Cache de klines e tickers (igual ao BybitClient)
        self._kline_cache: dict = {}    # (symbol, interval) → deque[dict]
        self._ticker_cache: dict = {}   # symbol → dict
        self._ob_cache:    dict = {}    # symbol → dict
        self._stale_logged: dict = {}   # controle de log de cache obsoleto
        self._ping_task     = None             # task de ping do WS (cancelável)
        self._ws_endpoint   = None             # endpoint WS vindo da KuCoin
        self._ws_ping_ms    = 18000            # pingInterval exigido pela KuCoin
        self._rate_lock     = asyncio.Lock()   # serializa o throttle
        # ══════════════════════════════════════════════════════════
        # P0 (Fase 4F) — CONTADOR REAL DE RATE LIMIT
        #
        # O IntegrityGuard lia client._rate_limit_hits, mas NADA
        # incrementava esse atributo: era um contador fictício e a
        # condição RATE_LIMITED nunca disparava.
        #
        # Agora é incrementado a cada 429 e decai com o tempo, para que
        # um pico isolado não bloqueie o bot indefinidamente.
        # ══════════════════════════════════════════════════════════
        self._rate_limit_hits: int   = 0
        self._rate_limit_last: float = 0.0
        self._rate_limit_backoff_until: float = 0.0
        self._rate_sem      = asyncio.Semaphore(self._RATE_MAX_CONCURRENT)
        self._last_req_ts   = 0.0
        self._last_ws_update: float = 0.0   # timestamp do último kline via WS
        self._instruments: dict = {}    # symbol → {minQty, qtyStep, tickSize, minNotional}

        self._connected = False
        self._running   = False

    # ── Rate Limiter ──────────────────────────────────────────────
    # A KuCoin limita requisições por peso (~30/10s no público).
    # Com 12 pares × 3 filtros REST a cada scan de 20s, o bot fazia
    # ~1.8 req/s de forma não-controlada, arriscando HTTP 429 e ban
    # temporário de IP. O semáforo + intervalo mínimo evitam isso.
    _RATE_MAX_CONCURRENT = int(os.environ.get("KUCOIN_MAX_CONCURRENT", "5"))
    _RATE_MIN_INTERVAL   = float(os.environ.get("KUCOIN_MIN_INTERVAL", "0.06"))

    def _register_429(self):
        """Registra um 429 e agenda backoff global."""
        import random
        now = time.time()
        # Decaimento: hits antigos (>5min) não contam mais
        if now - self._rate_limit_last > 300:
            self._rate_limit_hits = 0
        self._rate_limit_hits += 1
        self._rate_limit_last  = now
        # Backoff GLOBAL: todas as requisições esperam, não só esta.
        # Sem isso, N corrotinas em paralelo geram retry storm.
        espera = min(60.0, 2 ** min(self._rate_limit_hits, 6))
        espera *= (1 + random.random() * 0.3)     # jitter até +30%
        self._rate_limit_backoff_until = now + espera

    def _backoff_seconds(self, attempt: int) -> float:
        """Backoff exponencial com jitter (evita sincronização de retries)."""
        import random
        base = min(30.0, 2 ** (attempt + 1))
        return base * (1 + random.random() * 0.5)

    def rate_limit_status(self) -> dict:
        now = time.time()
        return {
            "hits_recentes":   self._rate_limit_hits,
            "ultimo_429_s":    round(now - self._rate_limit_last, 1)
                               if self._rate_limit_last else None,
            "em_backoff":      now < self._rate_limit_backoff_until,
            "backoff_restante": max(0.0, round(
                self._rate_limit_backoff_until - now, 1)),
        }

    async def _throttle(self):
        """Espaça as requisições para respeitar o rate limit da KuCoin."""
        # Backoff GLOBAL após 429: bloqueia TODAS as requisições, não só
        # a que falhou. É isto que impede o retry storm.
        _bo = self._rate_limit_backoff_until - time.time()
        if _bo > 0:
            await asyncio.sleep(min(_bo, 60.0))

        async with self._rate_lock:
            now  = time.monotonic()
            wait = self._last_req_ts + self._RATE_MIN_INTERVAL - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_req_ts = time.monotonic()

    # ── Sessão HTTP ───────────────────────────────────────────────
    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )

    # ── Autenticação HMAC-SHA256 ──────────────────────────────────
    def _sign(self, timestamp: str, method: str, endpoint: str, body: str = "") -> str:
        """
        KuCoin signature: HMAC-SHA256( timestamp + method + endpoint + body )
        Retorna base64 do digest.

        Formato exato exigido pela KuCoin:
          str_to_sign = timestamp + METHOD + /api/v1/endpoint?query + body
        """
        message = f"{timestamp}{method.upper()}{endpoint}{body}"
        sig = hmac.new(
            API_SECRET.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature = b64encode(sig).decode()
        # Log de diagnóstico — mostra o que está sendo assinado (sem expor o secret)
        if not self._sign_logged:
            self._sign_logged = True
            log.info(f"🔏 Assinando: '{message[:80]}...' → sign[:12]={signature[:12]}")
        return signature

    def _sign_passphrase(self) -> str:
        """KuCoin exige passphrase também assinada com HMAC-SHA256."""
        sig = hmac.new(
            API_SECRET.encode("utf-8"),
            API_PASSPHRASE.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return b64encode(sig).decode()

    def _auth_headers(self, method: str, endpoint: str, body: str = "") -> dict:
        """
        Headers de autenticação KuCoin.

        IMPORTANTE — v1 vs v2:
          API Key v2: passphrase ASSINADA com HMAC-SHA256 (base64)
          API Key v1: passphrase em TEXTO PLANO

        Enviar passphrase assinada para uma key v1 (ou vice-versa) resulta
        em "400004 Invalid KC-API-PASSPHRASE".

        self._api_version controla qual formato usar. Começa em "2" e faz
        fallback automático para "1" se receber 400004.
        """
        ts = str(self._now_ms())   # timestamp ajustado pelo offset do servidor
        if self._api_version == "2":
            passphrase = self._sign_passphrase()   # assinada
        else:
            passphrase = API_PASSPHRASE            # texto plano (v1)
        return {
            "KC-API-KEY":         API_KEY,
            "KC-API-SIGN":        self._sign(ts, method, endpoint, body),
            "KC-API-TIMESTAMP":   ts,
            "KC-API-PASSPHRASE":  passphrase,
            "KC-API-KEY-VERSION": self._api_version,
            "Content-Type":       "application/json",
        }

    # ── Requests base ─────────────────────────────────────────────
    async def _get(self, endpoint: str, params: dict = None, auth: bool = False) -> dict:
        await self._ensure_session()
        from urllib.parse import urlencode

        # Para autenticação KuCoin: a URL assinada DEVE ser idêntica à URL enviada.
        # Problema anterior: aiohttp montava a URL com params internamente podendo
        # reordenar os parâmetros, causando mismatch com a assinatura.
        # SOLUÇÃO: montar a URL completa manualmente e enviar sem params separados.
        if params:
            query_string   = urlencode(params)
            signed_endpoint = f"{endpoint}?{query_string}"
            full_url        = REST_BASE + signed_endpoint  # URL idêntica à assinada
        else:
            signed_endpoint = endpoint
            full_url        = REST_BASE + endpoint

        headers = self._auth_headers("GET", signed_endpoint) if auth else {}

        for attempt in range(3):
            try:
                await self._throttle()
                async with self._rate_sem, self._session.get(full_url, headers=headers) as r:
                    # ══════════════════════════════════════════════════
                    # BUG CORRIGIDO (Fase 4F) — 429 ERA PERDIDO
                    #
                    # r.json() levanta ContentTypeError quando o status é
                    # de erro, ANTES de chegar ao tratamento de 429. A
                    # exceção caía no except genérico, que fazia retry
                    # cego — o contador nunca incrementava e o backoff
                    # global nunca era acionado.
                    #
                    # Agora o status HTTP é verificado ANTES de parsear.
                    # ══════════════════════════════════════════════════
                    if r.status == 429:
                        self._register_429()
                        _w = self._backoff_seconds(attempt)
                        log.warning(
                            f"🚦 Rate limit KuCoin HTTP 429 "
                            f"({self._rate_limit_hits} recentes) — "
                            f"aguardando {_w:.1f}s"
                        )
                        await asyncio.sleep(_w)
                        continue

                    try:
                        data = await r.json(content_type=None)
                    except Exception as _je:
                        log.warning(
                            f"KuCoin GET {endpoint}: resposta não-JSON "
                            f"(HTTP {r.status}): {_je}"
                        )
                        await asyncio.sleep(self._backoff_seconds(attempt))
                        continue

                    if not isinstance(data, dict):
                        log.warning(f"KuCoin GET {endpoint}: payload inesperado")
                        return {}

                    if data.get("code") == "200000":
                        return data.get("data", {})

                    code = data.get("code", "")

                    # Código de rate limit no corpo (alguns endpoints)
                    if code in ("429000", "429"):
                        self._register_429()
                        _w = self._backoff_seconds(attempt)
                        log.warning(
                            f"🚦 Rate limit KuCoin ({self._rate_limit_hits} "
                            f"recentes) — aguardando {_w:.1f}s"
                        )
                        await asyncio.sleep(_w)
                        continue

                    # Erros de autenticação: 400004 (passphrase) / 400005 (sign)
                    # Estratégia: alterna v2↔v1 e re-sincroniza o relógio.
                    # NÃO trava numa versão — cada erro tenta a alternativa.
                    if code in ("400004", "400005") and attempt < 2:
                        old_ver = self._api_version
                        self._api_version = "1" if old_ver == "2" else "2"

                        # 400005 = sign inválido → pode ser drift de relógio
                        if code == "400005" and not self._time_synced:
                            await self.sync_time()

                        log.warning(
                            f"🔄 {code} com API v{old_ver} → tentando v{self._api_version} "
                            f"(passphrase {'texto plano' if self._api_version == '1' else 'assinada'})"
                        )
                        headers = self._auth_headers("GET", signed_endpoint)
                        continue   # retenta com a outra versão

                    log.warning(f"KuCoin GET {endpoint}: {code} {data.get('msg','')}")
                    return {}
            except Exception as e:
                wait = 2 ** attempt
                log.warning(f"KuCoin GET {endpoint} tentativa {attempt+1}: {e} — retry em {wait}s")
                await asyncio.sleep(wait)
        return {}

    def _parse_retry_after(self, header_value) -> float:
        """
        Interpreta o header Retry-After com segurança.

        ADV-02: aceita apenas segundos inteiros/decimais dentro de uma
        faixa razoável. Qualquer valor ausente, malformado ou
        absurdamente alto cai no backoff interno já existente — nunca
        dorme por tempo indefinido nem propaga exceção.
        """
        if not header_value:
            return 0.0
        try:
            v = float(header_value)
        except (TypeError, ValueError):
            return 0.0
        if v < 0 or v > 120:   # teto de segurança — nunca dorme mais que isso
            return 0.0
        return v

    async def get_order_by_client_oid(self, client_oid: str) -> dict:
        """Recupera uma ordem Futures pela chave idempotente do cliente.

        A KuCoin devolve o identificador no campo ``id`` neste endpoint,
        enquanto o restante do engine usa ``orderId``. O alias é
        normalizado aqui para manter um contrato interno único.
        """
        if not client_oid:
            return {}
        try:
            data = await self._get(
                "/api/v1/orders/byClientOid",
                {"clientOid": client_oid},
                auth=True,
            )
        except Exception as e:
            log.warning(f"get_order_by_client_oid {client_oid}: {e}")
            return {}
        if not isinstance(data, dict) or not data:
            return {}
        order_id = data.get("orderId") or data.get("id")
        if not order_id:
            return {}
        recovered = dict(data)
        recovered["orderId"] = str(order_id)
        recovered["clientOid"] = recovered.get("clientOid") or client_oid
        recovered["recoveredByClientOid"] = True
        return recovered

    async def _recover_ambiguous_order(self, endpoint: str, body: dict) -> dict:
        """Resolve POST de ordem cuja resposta pode ter sido perdida."""
        if endpoint != "/api/v1/orders":
            return {}
        client_oid = body.get("clientOid", "") if isinstance(body, dict) else ""
        if not client_oid:
            return {}
        # A indexação da ordem pode levar alguns milissegundos após o POST.
        for attempt in range(3):
            recovered = await self.get_order_by_client_oid(client_oid)
            if recovered:
                log.warning(
                    f"♻️ [ORDER RECOVERED] clientOid={client_oid} "
                    f"orderId={recovered['orderId']} após resposta ambígua"
                )
                return recovered
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
        return {}

    def _entry_safe_post(self, endpoint, body, url, **kwargs):
        if (getattr(self, 'entries_paused', False)
                and endpoint in ('/api/v1/orders', '/api/v1/st-orders')
                and body.get('reduceOnly') is not True
                and body.get('closeOrder') is not True):
            raise ValueError('New entry blocked by operator pause')
        return self._session.post(url, **kwargs)

    @asynccontextmanager
    async def _fenced_entry_post(self, endpoint, body, url, **kwargs):
        is_new_risk = (
            endpoint in ('/api/v1/orders', '/api/v1/st-orders')
            and body.get('reduceOnly') is not True
            and body.get('closeOrder') is not True
        )
        if is_new_risk:
            from bot.execution_ownership import validate_execution_ownership
            ownership = getattr(self, '_execution_ownership', None)
            if ownership is None:
                raise RuntimeError('OPEN_NEW_RISK missing execution ownership at transport boundary')
            await validate_execution_ownership(ownership)
        async with self._entry_safe_post(endpoint, body, url, **kwargs) as response:
            yield response

    async def _post(self, endpoint: str, body: dict, *, single_attempt: bool = False) -> dict:
        """
        NOTA DE IDEMPOTÊNCIA (ADV-02): body_str é serializado UMA VEZ,
        antes do loop de retry, e reenviado IDÊNTICO em todas as
        tentativas — o clientOid embutido em `body` nunca muda entre
        retries deste método. Isso já era verdade antes desta correção
        (nenhuma alteração de comportamento aqui); confirmado por
        teste (RATE-G/H/I).

        LIMITAÇÃO DOCUMENTADA: se a KuCoin processar a ordem mas a
        resposta HTTP se perder (timeout, conexão cortada), este
        método não tem como saber que a ordem já foi aceita — ele só
        vê uma exceção de rede e tenta de novo com o MESMO clientOid,
        o que é seguro (a exchange rejeita duplicata pelo clientOid).
        Essa garantia depende inteiramente do lado da exchange
        reconhecer clientOid repetido como idempotente — este projeto
        não tem como confirmar isso de forma independente sem acesso à
        KuCoin real (ver REAL_EXCHANGE_E2E = UNVERIFIED no restante da
        auditoria). Não invento uma garantia que não posso comprovar.
        """
        # Transport-level capability boundary. This executes before PAPER/LIVE
        # branching so a READ_ONLY runtime remains unable to mutate even if
        # valid credentials and live flags are accidentally configured.
        assert_exchange_mutation_allowed("POST", endpoint)
        if PAPER_TRADE:
            log.info("[PAPER] _post: exchange mutation skipped")
            return {}
        await self._ensure_session()
        url = REST_BASE + endpoint
        # separators=(",", ":") remove espaços — garante body idêntico entre assinatura e envio
        body_str = json.dumps(body, separators=(",", ":"))
        headers  = self._auth_headers("POST", endpoint, body_str)
        ambiguous = False
        for attempt in range(1 if single_attempt else 3):
            try:
                await self._throttle()
                async with self._rate_sem, self._fenced_entry_post(endpoint, body, url, data=body_str, headers=headers) as r:
                    # ══════════════════════════════════════════════════
                    # ADV-02 — HTTP 429 ERA PERDIDO EM _post()
                    #
                    # Mesmo bug já corrigido em _get() (Fase 4F): r.json()
                    # levanta ContentTypeError quando a KuCoin devolve
                    # HTTP 429 sem body JSON válido — a exceção caía no
                    # except genérico, fazendo retry cego sem nunca
                    # chamar _register_429(). Uma ordem rate-limitada
                    # nunca alimentava o mecanismo global de backoff.
                    #
                    # CASO A do pedido: HTTP status 429 puro.
                    # ══════════════════════════════════════════════════
                    if r.status == 429:
                        self._register_429()
                        _retry_after = self._parse_retry_after(
                            r.headers.get("Retry-After")
                        )
                        _w = _retry_after if _retry_after > 0 else self._backoff_seconds(attempt)
                        log.warning(
                            f"🚦 Rate limit KuCoin HTTP 429 em POST {endpoint} "
                            f"({self._rate_limit_hits} recentes) — "
                            f"aguardando {_w:.1f}s"
                            f"{' (Retry-After)' if _retry_after > 0 else ''}"
                        )
                        await asyncio.sleep(_w)
                        continue   # MESMO body_str/clientOid na próxima tentativa

                    try:
                        data = await r.json(content_type=None)
                    except Exception as _je:
                        log.warning(
                            f"KuCoin POST {endpoint}: resposta não-JSON "
                            f"(HTTP {r.status}): {_je}"
                        )
                        if r.status >= 500:
                            ambiguous = True
                            recovered = await self._recover_ambiguous_order(endpoint, body)
                            if recovered:
                                return recovered
                        await asyncio.sleep(self._backoff_seconds(attempt))
                        continue

                    if not isinstance(data, dict):
                        log.warning(f"KuCoin POST {endpoint}: payload inesperado")
                        return {}

                    if data.get("code") == "200000":
                        return data.get("data", {})
                    msg  = data.get("msg", "")
                    code = data.get("code", "")

                    # ══════════════════════════════════════════════════
                    # CASO B do pedido: HTTP 200 mas code="429000" no body.
                    # Mesma semântica já usada e testada em _get() — a
                    # KuCoin usa esse código para rate limit também em
                    # endpoints de escrita.
                    # ══════════════════════════════════════════════════
                    if code in ("429000", "429"):
                        self._register_429()
                        _retry_after = self._parse_retry_after(
                            r.headers.get("Retry-After")
                        )
                        _w = _retry_after if _retry_after > 0 else self._backoff_seconds(attempt)
                        log.warning(
                            f"🚦 Rate limit KuCoin (body) em POST {endpoint} "
                            f"({self._rate_limit_hits} recentes) — "
                            f"aguardando {_w:.1f}s"
                        )
                        await asyncio.sleep(_w)
                        continue

                    # Erros de autenticação: alterna v2↔v1 + re-sincroniza relógio
                    # (CASO E — erro definitivo de auth; NÃO é rate limit,
                    # tratamento pré-existente, inalterado)
                    if code in ("400004", "400005") and attempt < 2:
                        old_ver = self._api_version
                        self._api_version = "1" if old_ver == "2" else "2"
                        if code == "400005" and not self._time_synced:
                            await self.sync_time()
                        log.warning(
                            f"🔄 {code} com API v{old_ver} → tentando v{self._api_version}"
                        )
                        headers = self._auth_headers("POST", endpoint, body_str)
                        continue

                    # CASO E — erros definitivos: NÃO são rate limit,
                    # nenhum retry cego (comportamento pré-existente).
                    if code in ("400100", "300004", "200004"):
                        log.error(f"KuCoin POST {endpoint} erro permanente {code}: {msg}")
                        return {}
                    log.warning(f"KuCoin POST {endpoint}: {code} {msg}")
            except Exception as e:
                from bot.execution_ownership import StaleExecutionFence, ExecutionOwnershipUnavailable
                if isinstance(e, (StaleExecutionFence, ExecutionOwnershipUnavailable)):
                    raise
                # Network/timeout ambiguity is reconciled by clientOid.
                ambiguous = True
                recovered = await self._recover_ambiguous_order(endpoint, body)
                if recovered:
                    return recovered
                wait = 2 ** attempt
                log.warning(f"KuCoin POST {endpoint} tentativa {attempt+1}: {e} — retry em {wait}s")
                if not single_attempt:
                    await asyncio.sleep(wait)
        if ambiguous and endpoint == "/api/v1/orders":
            return {"clientOid": body.get("clientOid", ""), "_ambiguous": True}
        return {}

    # ── Saldo ─────────────────────────────────────────────────────
    async def get_balance(self) -> float:
        """Retorna saldo disponível em USDT na conta de futuros."""
        data = await self._get("/api/v1/account-overview", {"currency": "USDT"}, auth=True)
        if not isinstance(data, dict) or "availableBalance" not in data:
            raise RuntimeError("Futures availableBalance unavailable")
        try:
            if isinstance(data["availableBalance"], bool):
                raise ValueError("boolean balance")
            bal = float(data["availableBalance"])
            if not math.isfinite(bal):
                raise ValueError("nonfinite balance")
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Futures availableBalance invalid") from exc
        log.info(f"💰 Saldo USDT: ${bal:.4f}")
        return bal

    # ── Instrumentos ──────────────────────────────────────────────
    async def load_instruments(self):
        """
        Carrega contratos ativos da KuCoin e CONSTRÓI O MAPA DE SÍMBOLOS.

        CORRIGIDO: antes usava um mapa hardcoded com símbolos inventados
        (ex: BNBUSDTM não existe na KuCoin). Pares inexistentes falhavam
        silenciosamente, e só os que por acaso batiam apareciam nos logs.

        Agora: descobre os símbolos reais via /api/v1/contracts/active e
        casa cada par de cfg.SYMBOLS com o contrato correspondente pela
        baseCurrency, respeitando aliases (BTC↔XBT).
        """
        # Sincroniza relógio ANTES de qualquer chamada autenticada
        if not self._time_synced:
            await self.sync_time()

        data = await self._get("/api/v1/contracts/active")  # público, sem auth
        if isinstance(data, list):
            contracts = data
        elif isinstance(data, dict):
            contracts = data.get("dataList", data.get("items", []))
        else:
            contracts = []

        if not contracts:
            log.error("❌ Nenhum contrato retornado pela KuCoin")
            return self._instruments

        # Indexa contratos USDT-margined pela baseCurrency
        by_base = {}
        for c in contracts:
            sym = c.get("symbol", "")
            if not sym.endswith("USDTM"):
                continue
            if c.get("status", "Open") not in ("Open", "open", None):
                continue
            base = (c.get("baseCurrency") or sym[:-5]).upper()
            by_base[base] = c

        from bot.config import cfg as _cfg
        wanted    = list(getattr(_cfg, "SYMBOLS", []))
        matched   = []
        unmatched = []

        for std_sym in wanted:
            if not std_sym.endswith("USDT"):
                unmatched.append(std_sym)
                continue
            base = std_sym[:-4].upper()             # BTCUSDT → BTC
            kc_base = _BASE_ALIAS.get(base, base)   # BTC → XBT

            c = by_base.get(kc_base) or by_base.get(base)
            if not c:
                unmatched.append(std_sym)
                continue

            kc_sym = c["symbol"]
            # Registra nos mapas globais (usados por to_kucoin/to_standard)
            SYMBOL_MAP[std_sym]      = kc_sym
            SYMBOL_MAP_REV[kc_sym]   = std_sym

            lot_size = float(c.get("lotSize",    1))
            tick_sz  = float(c.get("tickSize",   0.01))
            mult     = float(c.get("multiplier", 0.001))
            max_lev  = float(c.get("maxLeverage", 0) or 0)

            self._instruments[std_sym] = {
                "minQty":      float(c.get("minQty", lot_size)),
                "lotSize":     lot_size,
                "qtyStep":     lot_size,
                "tickSize":    tick_sz,
                "multiplier":  mult,
                "maxLeverage": max_lev,
                # Native lot size is contracts; base minimum is diagnostic only.
                # A missing notional rule means no quote minimum, not base quantity.
                "minBaseQty":  float(c.get("minQty", lot_size)) * mult,
                "minNotional": float(c.get("minNotional", 0) or 0),  # USDT
                "kucoinSymbol": kc_sym,
            }
            matched.append(f"{std_sym}→{kc_sym}")

        log.info(f"📋 {len(matched)} pares mapeados na KuCoin: {', '.join(matched)}")
        if unmatched:
            log.warning(
                f"⚠️ {len(unmatched)} pares NÃO existem na KuCoin Futures "
                f"e serão ignorados: {', '.join(unmatched)}"
            )
        return self._instruments

    def get_instruments(self) -> dict:
        return self._instruments

    # ── Alavancagem ───────────────────────────────────────────────
    async def set_leverage(self, symbol: str, leverage: int):
        """
        Define alavancagem para um par.
        KuCoin Futures: POST /api/v2/changeCrossUserLeverage (cross margin)
        ou via parâmetro leverage diretamente na ordem (mais simples).
        CORRIGIDO: endpoint correto para KuCoin Futures v1.
        """
        # ══════════════════════════════════════════════════════════
        # BUG CRÍTICO CORRIGIDO — set_leverage TRAVAVA O STARTUP
        #
        # Este POST era feito para CADA par no _connect(). O endpoint
        # frequentemente falha (não existe em todas as versões da API), e
        # o _post tem 3 tentativas com backoff exponencial (1+2+4s).
        #
        # Com 12 pares: até 12 × 7s = 84 segundos de bloqueio, mais 0.3s
        # de sleep entre cada um. Como isso roda ANTES de connected=True,
        # o LOOP PRINCIPAL não começava — nenhum scan acontecia.
        #
        # Nos logs isso era invisível: o WebSocket roda em task separada
        # e continuava publicando normalmente, dando a impressão de que
        # o bot estava vivo enquanto o engine seguia bloqueado.
        #
        # A alavancagem já é enviada como parâmetro em cada ordem
        # (place_order → body["leverage"]), então esta chamada é
        # redundante. Vira no-op por padrão.
        # ══════════════════════════════════════════════════════════
        if PAPER_TRADE:
            log.info("[PAPER] set_leverage: exchange mutation skipped")
            return None
        if os.environ.get("KUCOIN_SET_LEVERAGE_ENDPOINT", "false").lower() == "true":
            kc_sym = to_kucoin(symbol)
            try:
                await asyncio.wait_for(
                    self._post("/api/v1/position/margin/auto-deposit-status", {
                        "symbol": kc_sym,
                        "status": False,
                    }),
                    timeout=3,
                )
            except Exception as e:
                log.debug(f"set_leverage {symbol}: {e}")

        log.debug(f"⚙️ Leverage {symbol}: {leverage}x (via parâmetro da ordem)")

    # ── Ordens ────────────────────────────────────────────────────
    def _round_price(self, price: float, symbol: str) -> str:
        """Arredonda preço ao tickSize correto do instrumento."""
        info      = self._instruments.get(symbol, {})
        tick_size = float(info.get("tickSize", 0.01))
        ticks     = round(price / tick_size)
        clean     = round(ticks * tick_size, 8)
        decimals  = max(0, -int(math.floor(math.log10(tick_size)))) if tick_size < 1 else 0
        return f"{clean:.{decimals}f}"

    def _round_qty(self, qty: float, symbol: str) -> int:
        """Boundary: base asset -> integer KuCoin contracts, exactly once."""
        return base_to_contracts(qty, self._instruments[symbol])

    def build_client_oid(self, symbol: str, side: str, qty: float,
                         idem_key: str = None, contracts: int = None) -> str:
        """Return the exact clientOid that ``place_order`` will submit.

        Callers can durably persist this identifier before any network
        dispatch. The calculation is pure and does not contact the exchange.
        """
        window = int(time.time() // 60)
        if idem_key:
            raw = idem_key
        elif PAPER_TRADE:
            raw = f"{symbol}_{side}_{qty}_{window}"
        else:
            contract_qty = contracts
            if contract_qty is None:
                contract_qty = self._round_qty(qty, symbol)
            raw = f"{symbol}_{side}_{contract_qty}_{window}"
        return f"bgx7-{hashlib.md5(raw.encode()).hexdigest()}"[:40]

    async def place_order(self, symbol: str, side: str, qty: float,
                          sl: float = 0, tp: float = 0,
                          instruments: dict = None,
                          reduce_only: bool = False,
                          idem_key: str = None,
                          single_submission: bool = False) -> dict:
        """
        Envia ordem a mercado com SL e TP opcionais.
        side: "Buy" ou "Sell" (mesmo padrão do BybitClient)
        qty: quantidade em unidade base (convertida para contratos internamente)
        """
        if PAPER_TRADE:
            log.info(f"[PAPER] {side} {qty} {symbol} SL={sl} TP={tp}")
            # clientOid consistente com o caminho real: mesmo formato
            # (prefixo bgx7- + hash), calculado sobre qty pois contracts
            # ainda não existe neste ponto do fluxo (round_qty é chamado
            # depois). Suficiente em PAPER_TRADE pois não há correlação
            # com uma ordem real a preservar.
            _oid_paper = self.build_client_oid(symbol, side, qty, idem_key)
            return {"orderId": f"paper_{int(time.time()*1000)}",
                    "clientOid": _oid_paper}

        if not API_KEY:
            log.warning("place_order: KUCOIN_API_KEY não configurado")
            return {}

        kc_sym    = to_kucoin(symbol)
        contracts = self._round_qty(qty, symbol)

        # KuCoin: "buy" = long, "sell" = short (lowercase)
        kc_side   = side.lower()

        # ══════════════════════════════════════════════════════════
        # P0 CORRIGIDO — IDEMPOTÊNCIA DE ORDEM
        #
        # O clientOid usava time.time() em ms, mudando a cada tentativa.
        # Se uma ordem executasse na exchange mas a resposta se perdesse
        # (timeout de rede), o retry do engine geraria um OID NOVO e a
        # KuCoin aceitaria como ordem distinta → POSIÇÃO DUPLICADA,
        # com o dobro da exposição pretendida.
        #
        # Agora o OID é determinístico por (símbolo, lado, qty, janela
        # de 60s). Retries dentro da mesma janela reusam o mesmo OID e a
        # exchange rejeita a duplicata — comportamento correto.
        #
        # idem_key permite ao chamador forçar um OID específico.
        # ══════════════════════════════════════════════════════════
        # ══════════════════════════════════════════════════════════
        # OBSERVABILIDADE (Fase 7) — GAP ENCONTRADO
        #
        # O clientOid era um hash MD5 puro, sem qualquer marca de
        # origem. Consequência prática, descoberta ao analisar um
        # print de tela do usuário: é IMPOSSÍVEL, olhando o histórico
        # de ordens na interface da KuCoin, distinguir uma ordem aberta
        # pelo bot de uma aberta manualmente pelo usuário. A única
        # fonte de verdade era o banco de dados interno do bot — que
        # não é o que aparece na exchange.
        #
        # Prefixo fixo torna toda ordem do bot auto-identificável
        # diretamente na tela "Ordens" da KuCoin (campo clientOid é
        # visível lá), sem precisar cruzar com logs internos.
        # ══════════════════════════════════════════════════════════
        _oid = self.build_client_oid(
            symbol, side, qty, idem_key, contracts=contracts
        )

        # BUG CORRIGIDO: lia os.environ diretamente com default "10", ignorando
        # o valor de config.py. Se LEVERAGE não estivesse setado no Railway,
        # a ordem ia com 10x mesmo com o config em 50x.
        from bot.config import cfg as _cfg
        _lev = int(_cfg.LEVERAGE)

        body: dict = {
            "clientOid": _oid,
            "symbol":    kc_sym,
            "side":      kc_side,
            "type":      "market",
            "size":      str(contracts),
            "leverage":  str(_lev),
        }

        # reduceOnly (auditoria #3): garante que a ordem SÓ pode reduzir uma
        # posição existente. Sem essa flag, se a qty do bot divergir da qty
        # real na exchange, a ordem de "fechamento" abriria posição na direção
        # oposta, dobrando a exposição em vez de zerá-la.
        if reduce_only:
            body["reduceOnly"] = True
            body["closeOrder"] = True   # KuCoin: fecha a posição do símbolo

        # ══════════════════════════════════════════════════════════════
        # 🔴 BUG CRÍTICO CORRIGIDO — SL/TP NÃO VÃO NA ORDEM DE ENTRADA
        #
        # O código anterior fazia:
        #     body["stop"]      = "down" if side=="Buy" else "up"
        #     body["stopPrice"] = SL
        #     body["takeProfit"]= TP
        #
        # Na KuCoin, os campos stop/stopPrice em /api/v1/orders NÃO anexam
        # um stop loss à posição — eles CONVERTEM a ordem inteira numa
        # STOP ORDER CONDICIONAL, que só é ativada quando o preço atinge
        # stopPrice.
        #
        # Efeito para um LONG: criava uma ordem de COMPRA que só dispararia
        # se o preço CAÍSSE até o nível do stop loss. Ou seja:
        #   1. a ordem não executava na hora (ficava pendente);
        #   2. se executasse, compraria no pior preço possível;
        #   3. e a posição ficaria SEM stop loss.
        # O campo "takeProfit" sequer existe nesse endpoint — era ignorado.
        #
        # CORREÇÃO: a entrada é uma ordem MARKET pura. SL e TP são anexados
        # logo depois via POST /api/v1/position/trading-stop, que é o
        # endpoint correto para stop de posição na KuCoin Futures.
        # ══════════════════════════════════════════════════════════════

        submitted_oid = body["clientOid"]
        registry = getattr(self, "_order_registry", None)
        managed_order = None
        if registry is not None and submitted_oid.startswith("bgx7-"):
            managed_order, _created = registry.get_or_create(
                submitted_oid, symbol, side, float(contracts)
            )
            managed_order.reduce_only = bool(reduce_only)
            managed_order.exposure_intent = "REDUCE" if reduce_only else "INCREASE"
            if reduce_only:
                engine = getattr(self, "_engine", None)
                local_position = (
                    (getattr(engine, "positions", {}) or {}).get(symbol)
                    if engine is not None else None
                )
                previous_qty = getattr(local_position, "qty", None)
                if previous_qty is not None:
                    try:
                        managed_order.previous_position_qty = max(
                            0.0, float(previous_qty)
                        )
                    except (TypeError, ValueError):
                        managed_order.previous_position_qty = None
            if managed_order.state == OrderState.CREATED:
                managed_order.transition(OrderState.SUBMITTING, source="LOCAL")

        post_options = {"single_attempt": True} if single_submission else {}
        # Last possible OPEN_NEW_RISK gate before the exchange mutation boundary.
        # reduceOnly exits intentionally bypass ownership so DB/fence failure cannot
        # prevent emergency risk reduction of an existing position.
        if not reduce_only:
            from bot.critical_state import critical_state
            from bot.runtime_readiness import assert_ready_for_new_entries
            from bot.execution_ownership import (
                acquire_execution_ownership,
                validate_execution_ownership,
                publish_valid_execution_ownership,
            )
            critical_state.assert_available_for_new_risk()
            engine = getattr(self, "_engine", None)
            if engine is None:
                raise RuntimeError("READY_FOR_NEW_ENTRIES=false: execution engine unavailable")
            # Ownership is one readiness component, so establish/validate it
            # before evaluating the canonical aggregate snapshot.
            ownership = getattr(self, "_execution_ownership", None)
            if ownership is None:
                ownership = await acquire_execution_ownership()
                self._execution_ownership = ownership
            await validate_execution_ownership(ownership)
            publish_valid_execution_ownership(engine, ownership, event="predispatch_validated")
            assert_ready_for_new_entries(engine)
        data     = await self._post("/api/v1/orders", body, **post_options)
        order_id = data.get("orderId", "")

        # Última barreira antes de alterar leverage (e gerar outro OID):
        # tenta resolver a ordem original pela chave oficial da KuCoin.
        if not order_id and data.get("_ambiguous"):
            recovered = await self._recover_ambiguous_order(
                "/api/v1/orders", {"clientOid": submitted_oid}
            )
            if recovered:
                data = recovered
                order_id = data["orderId"]

        if order_id:
            if registry is not None and managed_order is not None:
                registry.index_order_id(str(order_id), submitted_oid)
                try:
                    managed_order.transition(OrderState.SUBMITTED, order_id=str(order_id), source="REST")
                except InvalidTransition as exc:
                    log.warning("[ORDER_REGISTRY_ACK] clientOid=%s orderId=%s conflict=%s", submitted_oid, order_id, exc)
            # ══════════════════════════════════════════════════════
            # GAP DE OBSERVABILIDADE CORRIGIDO
            #
            # clientOid nunca aparecia junto do orderId no log. Isso
            # impedia a correlação clientOid → orderId exigida pela
            # Fase 5 do protocolo de prova E2E: sem os dois na mesma
            # linha, não dá para provar visualmente nos logs do
            # Railway que "esta ordem específica do bot" (identificada
            # pelo prefixo bgx7-) é a mesma que aparece na KuCoin com
            # este orderId.
            # ══════════════════════════════════════════════════════
            log.info(
                f"📤 [ORDER] clientOid={submitted_oid} orderId={order_id} "
                f"symbol={symbol} side={side} qty={contracts} "
                f"leverage={body['leverage']}x"
            )

            # ── Anexa SL/TP à POSIÇÃO (endpoint correto da KuCoin) ────
            # Só faz sentido em ordem de abertura (reduce_only=False).
            if not reduce_only and (sl > 0 or tp > 0):
                # Pequena espera para a posição existir antes do trading-stop
                await asyncio.sleep(0.4)
                ok = await self.set_position_stops(symbol, sl=sl, tp=tp)
                if not ok:
                    log.error(
                        f"🚨 {symbol}: posição ABERTA mas SL/TP NÃO foram "
                        f"anexados — posição desprotegida! Verifique manualmente."
                    )
                    data["sl_tp_failed"] = True
                else:
                    log.info(
                        f"🛡️ {symbol}: SL=${sl:.4f} TP=${tp:.4f} anexados à posição"
                    )
        else:
            log.error(f"❌ Ordem {symbol} rejeitada em todas as tentativas")

        # ══════════════════════════════════════════════════════════
        # BUG CORRIGIDO — clientOid REAL nunca era devolvido ao chamador
        #
        # engine.py usava _idem (a chave pré-hash, ex: "SOLUSDT_Buy_
        # 18.7_29804709") como se fosse o clientOid — mas o clientOid
        # REALMENTE enviado à KuCoin é _oid ("bgx7-<hash md5>"), gerado
        # aqui dentro e nunca propagado de volta.
        #
        # Consequência prática, capturada em teste E2E nesta sessão:
        # o log [ORDER] mostrava um clientOid diferente do log [FILLED]
        # para a MESMA ordem — quebrando exatamente a correlação que a
        # prova E2E do protocolo exige (Fase 5/7/12).
        # ══════════════════════════════════════════════════════════
        if isinstance(data, dict):
            data["clientOid"] = submitted_oid
        return data

    async def _position_exists(self, symbol: str) -> bool:
        """
        Confirma na exchange se já existe posição aberta no símbolo.

        Usado antes de qualquer RETRY de ordem de abertura: um timeout de
        rede não significa que a ordem falhou — ela pode ter executado.
        Reenviar sem verificar dobra a exposição.
        """
        try:
            positions = await self.get_positions()
            for p in positions:
                if p.get("symbol") == symbol and abs(float(p.get("size", 0) or 0)) > 0:
                    return True
        except Exception as e:
            # Na dúvida, assume que EXISTE — falhar para o lado seguro.
            log.warning(
                f"_position_exists {symbol}: {e} — assumindo que existe "
                f"para evitar ordem duplicada"
            )
            return True
        return False

    async def set_position_stops(self, symbol: str, sl: float = 0,
                                  tp: float = 0) -> bool:
        from bot.native_stop_repair import set_stops
        import sys
        return await set_stops(self, symbol, sl, tp, sys.modules[__name__], log)

    async def set_sl(self, symbol: str, sl: float, instruments: dict = None) -> bool:
        return await self.set_position_stops(symbol, sl=sl)

    # ── Cancelar ordens ───────────────────────────────────────────
    async def cancel_all_orders(self, symbol: str = "") -> bool:
        """
        Cancela todas as ordens abertas.
        KuCoin Futures: DELETE /api/v1/orders
        CORRIGIDO: removido código duplicado (POST + DELETE ao mesmo tempo).
        """
        assert_exchange_mutation_allowed("DELETE", "/api/v1/orders")
        if PAPER_TRADE:
            log.info("[PAPER] cancel_all_orders: exchange mutation skipped")
            return False
        await self._ensure_session()
        kc_sym   = to_kucoin(symbol) if symbol else ""
        params   = {"symbol": kc_sym} if kc_sym else {}
        endpoint = "/api/v1/orders"
        # Monta query string para incluir na assinatura (mesmo padrão do _get corrigido)
        if params:
            from urllib.parse import urlencode
            signed_endpoint = f"{endpoint}?{urlencode(params)}"
        else:
            signed_endpoint = endpoint
        headers = self._auth_headers("DELETE", signed_endpoint)
        url     = REST_BASE + endpoint
        try:
            async with self._session.delete(url, params=params, headers=headers) as r:
                d = await r.json()
                ok = d.get("code") == "200000"
                if ok:
                    log.info(f"✓ cancel_all_orders {symbol or 'todos'}")
                else:
                    log.warning(f"cancel_all_orders: {d.get('code')} {d.get('msg','')}")
                return ok
        except Exception as e:
            log.error(f"cancel_all_orders {symbol}: {e}")
            return False

    # ── Klines / Candles ──────────────────────────────────────────
    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list:
        """
        Busca klines históricas.
        KuCoin endpoint: GET /api/v1/kline/query
        interval: mesmo formato Bybit (15, 60, 240) — convertido internamente
        """
        kc_sym   = to_kucoin(symbol)
        gran     = INTERVAL_MAP.get(str(interval), int(interval))   # granularidade em minutos

        # Timestamps em MILISSEGUNDOS (exigência da KuCoin Futures).
        # int() explícito evita notação científica em números grandes.
        end_ts   = int(time.time() * 1000)
        span_ms  = int(gran) * 60 * 1000 * int(limit)   # minutos → ms × qtd candles

        # KuCoin limita o range por requisição. Cap de 200 dias evita
        # "from" muito antigo, que a API rejeita.
        MAX_SPAN_MS = 200 * 24 * 60 * 60 * 1000
        span_ms  = min(span_ms, MAX_SPAN_MS)
        start_ts = max(0, end_ts - span_ms)

        # KuCoin Futures kline endpoint correto
        # granularity em minutos: 1, 5, 15, 30, 60, 120, 240, 480, 720, 1440, 10080
        # CORRIGIDO: KuCoin Futures exige 'from'/'to' em MILISSEGUNDOS.
        # Erro anterior: "300000 Parameter 'from' must be milliseconds"
        data = await self._get("/api/v1/kline/query", {
            "symbol":      kc_sym,
            "granularity": str(gran),
            "from":        str(start_ts),   # milissegundos
            "to":          str(end_ts),     # milissegundos
        })

        klines = []
        raw    = data if isinstance(data, list) else []
        for k in raw:
            # KuCoin Futures kline: [timestamp_ms, open, high, low, close, volume, turnover]
            # Nota: KuCoin retorna do mais recente para o mais antigo — inverter no final
            try:
                _ts = int(float(k[0]))
                # KuCoin Futures retorna ts em ms; normaliza caso venha em segundos
                if _ts < 1e11:
                    _ts *= 1000
                klines.append({
                    "ts": _ts,
                    "o":  float(k[1]),
                    "h":  float(k[2]),
                    "l":  float(k[3]),
                    "c":  float(k[4]),
                    "v":  float(k[5]),
                })
            except (IndexError, ValueError, TypeError) as _e:
                # auditoria #10: descarte de candle malformado agora é contado.
                # Log em nível debug para não poluir, mas rastreável.
                self._malformed_klines = getattr(self, "_malformed_klines", 0) + 1
                if self._malformed_klines % 50 == 1:
                    log.debug(
                        f"kline malformada descartada ({self._malformed_klines} "
                        f"no total): {_e}"
                    )
                continue
        # ══════════════════════════════════════════════════════════
        # 🔴 BUG CRÍTICO CORRIGIDO — ORDENAÇÃO DOS CANDLES
        #
        # O código fazia reversed() INCONDICIONALMENTE, assumindo que a
        # KuCoin sempre retorna do mais recente para o mais antigo.
        #
        # Se a API retorna em ordem cronológica (ou muda de ordem entre
        # endpoints/versões), a inversão QUEBRA todos os indicadores:
        # o RSI de uma alta forte vira 0, as EMAs se invertem, o ADX
        # aponta a direção errada.
        #
        # EVIDÊNCIA (teste E2E contra mock com tendência de alta):
        #   RSI=0 em TODOS os 12 pares → bloqueio "RSI extremo"
        #   → nenhum sinal passava → NENHUMA ORDEM DESDE A MIGRAÇÃO
        #
        # Agora a ordem é DETERMINADA pelos timestamps, não assumida.
        # ══════════════════════════════════════════════════════════
        if len(klines) >= 2:
            if klines[0]["ts"] > klines[-1]["ts"]:
                klines = list(reversed(klines))   # veio do mais recente
            # Garantia final: ordenação cronológica estrita
            klines.sort(key=lambda k: k["ts"])

            # Sanidade: timestamps duplicados indicam dados corrompidos
            _ts = [k["ts"] for k in klines]
            if len(set(_ts)) != len(_ts):
                _vistos, _limpo = set(), []
                for k in klines:
                    if k["ts"] not in _vistos:
                        _vistos.add(k["ts"])
                        _limpo.append(k)
                log.warning(
                    f"{symbol} {interval}m: {len(klines)-len(_limpo)} candles "
                    f"duplicados removidos"
                )
                klines = _limpo

        # Atualiza cache
        key = (symbol, str(interval))
        if key not in self._kline_cache:
            self._kline_cache[key] = deque(maxlen=500)
        self._kline_cache[key].extend(klines[-limit:])
        return klines[-limit:]

    # Idade máxima aceitável do último candle antes de considerar o cache obsoleto.
    # Múltiplo do próprio intervalo: 15m → 45min, 1h → 3h, 4h → 12h.
    STALE_MULTIPLIER = 3

    def get_cached_klines(self, symbol: str, interval: str, limit: int = 200) -> list:
        """
        Retorna klines do cache WS.

        CORRIGIDO: antes devolvia o cache independentemente da idade. Se o
        WebSocket caísse (ou a subscrição falhasse, como acontecia com o
        tópico errado), o bot seguia calculando entry/SL/TP sobre preços
        congelados sem qualquer aviso.

        Agora: se o último candle for mais antigo que STALE_MULTIPLIER ×
        intervalo, retorna lista vazia — forçando o chamador a buscar via
        REST — e loga o problema.
        """
        key    = (symbol, str(interval))
        cached = list(self._kline_cache.get(key, []))
        if not cached:
            return []

        try:
            iv_min  = int(INTERVAL_MAP.get(str(interval), int(interval)))
            max_age = iv_min * 60 * self.STALE_MULTIPLIER
            age     = time.time() - (cached[-1]["ts"] / 1000)
            if age > max_age:
                # Loga no máximo 1x por minuto por par para não poluir
                k = f"{symbol}_{interval}"
                last = self._stale_logged.get(k, 0)
                if time.time() - last > 60:
                    self._stale_logged[k] = time.time()
                    # Expurgo: sem isso o dict só cresceria (leak lento)
                    if len(self._stale_logged) > 100:
                        _cut = time.time() - 3600
                        for _k in [x for x, t in list(self._stale_logged.items())
                                   if t < _cut]:
                            self._stale_logged.pop(_k, None)
                    log.warning(
                        f"⏳ Cache OBSOLETO {symbol} {interval}m: último candle "
                        f"há {age/60:.1f}min (máx {max_age/60:.0f}min) — "
                        f"buscando via REST"
                    )
                return []
        except Exception as e:
            log.debug(f"get_cached_klines staleness {symbol}: {e}")

        return cached[-limit:]

    # ── Ticker ────────────────────────────────────────────────────
    async def get_ticker(self, symbol: str) -> dict:
        kc_sym = to_kucoin(symbol)
        data   = await self._get("/api/v1/ticker", {"symbol": kc_sym})
        ticker = {
            "symbol":    symbol,
            "lastPrice": float(data.get("price",      0)),
            "bid":       float(data.get("bestBidPrice", 0)),
            "ask":       float(data.get("bestAskPrice", 0)),
            "volume":    float(data.get("volume",     0)),
            "turnover":  float(data.get("turnover",   0)),
        }
        self._ticker_cache[symbol] = ticker
        return ticker

    def get_cached_ticker(self, symbol: str) -> dict:
        return self._ticker_cache.get(symbol, {})

    # ── Open Interest ─────────────────────────────────────────────
    async def get_all_tickers(self) -> list:
        """
        Retorna ticker de todos os contratos ativos (volume 24h em USDT).

        BUG CORRIGIDO: market_data.update_volume_filter() chamava este método
        que NÃO EXISTIA no KuCoinClient — a exceção era engolida pelo except
        e o filtro de volume mínimo NUNCA funcionava. O bot podia operar
        pares ilíquidos sem qualquer bloqueio.

        Formato de saída compatível com o consumidor (chave turnover24h).
        """
        data = await self._get("/api/v1/contracts/active")
        contracts = data if isinstance(data, list) else data.get("dataList", [])
        out = []
        for c in contracts:
            kc_sym = c.get("symbol", "")
            if not kc_sym.endswith("USDTM"):
                continue
            try:
                out.append({
                    "symbol":       to_standard(kc_sym),
                    # KuCoin expõe volume em USDT como turnoverOf24h
                    "turnover24h":  float(c.get("turnoverOf24h", 0) or 0),
                    "volume24h":    float(c.get("volumeOf24h",   0) or 0),
                    "lastPrice":    float(c.get("lastTradePrice", 0) or 0),
                    "priceChgPct":  float(c.get("priceChgPct",   0) or 0),
                })
            except (ValueError, TypeError) as e:
                log.debug(f"get_all_tickers: contrato {kc_sym} ignorado: {e}")
                continue
        log.debug(f"get_all_tickers: {len(out)} contratos com volume")
        return out

    async def get_open_interest(self, symbol: str) -> dict:
        kc_sym = to_kucoin(symbol)
        data   = await self._get("/api/v1/contracts/" + kc_sym)
        return {
            "openInterest":       str(data.get("openInterest",     0)),
            "openInterestValue":  str(data.get("openInterestValue", 0)),
        }

    # ── Funding Rate ──────────────────────────────────────────────
    async def get_funding_rate(self, symbol: str) -> float:
        kc_sym = to_kucoin(symbol)
        data   = await self._get("/api/v1/funding-rate/" + kc_sym + "/current")
        return float(data.get("value", 0))

    # ── Orderbook ─────────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════
    # P0 (Auditoria Fase 6/8) — CONFIRMAÇÃO DE FILLED
    #
    # GAP ENCONTRADO: nenhum lugar do projeto consultava o status real
    # da ordem após o POST /api/v1/orders. O código tratava
    # "HTTP 200 + orderId" como sucesso e criava a posição interna
    # imediatamente — sem nunca verificar se a ordem foi de fato
    # FILLED, ficou NEW/parcial, ou foi CANCELED pela exchange.
    #
    # Confirmado por busca em todo o repositório: zero ocorrências de
    # FILLED, filledSize ou dealSize em bot/kucoin.py antes desta
    # correção. bot/order_state.py já definia os estados corretos
    # (CREATED→SUBMITTING→SUBMITTED→FILLED) mas OrderRegistry nunca
    # era alimentado dentro de _open() — máquina de estados existia
    # e ficava sem uso.
    #
    # Endpoint: GET /api/v1/orders/{orderId} (KuCoin Futures).
    # ══════════════════════════════════════════════════════════════
    async def get_order_status(self, order_id: str) -> dict:
        """
        Consulta o estado real de uma ordem pelo orderId.

        Retorna dict com pelo menos: status, isActive, filledSize,
        dealSize/dealValue, cancelExist. Em caso de PAPER_TRADE ou
        orderId sintético (ex: 'paper_...', 'EXISTING_POSITION'),
        retorna um status FILLED sintético — não é chamada real.
        """
        # ══════════════════════════════════════════════════════════
        # EXEC-02 (CRITICAL) — orderId VAZIO NÃO É FILL SINTÉTICO
        #
        # `not order_id` estava agrupado com PAPER_TRADE e os orderIds
        # sintéticos legítimos, retornando _synthetic=True — que
        # wait_for_fill() interpreta como filled=True.
        #
        # Cadeia do bug (reproduzida em teste):
        #   place_order falha nas 3 tentativas → data = {}
        #   → data["clientOid"] = _oid torna o dict NÃO-vazio
        #   → _open() faz _order.get("orderId", "") = ""
        #   → get_order_status("") → _synthetic → filled=True
        #   → Position criada e "✅ ABERTO" logado para uma ordem
        #     que a exchange NUNCA aceitou.
        #
        # orderId ausente significa DESCONHECIDO, nunca preenchido.
        # ══════════════════════════════════════════════════════════
        if not order_id:
            log.error(
                "get_order_status: orderId vazio — estado da ordem "
                "DESCONHECIDO (não é fill). Retornando status não-confirmado."
            )
            return {"status": "unknown", "isActive": True,
                    "filledSize": "0", "_unknown": True}

        if PAPER_TRADE or order_id in (
            "EXISTING_POSITION",
        ) or str(order_id).startswith(("paper_",)):
            return {"status": "done", "isActive": False,
                    "filledSize": "0", "_synthetic": True}
        try:
            data = await self._get(f"/api/v1/orders/{order_id}", auth=True)
            return data or {}
        except Exception as e:
            log.warning(f"get_order_status {order_id}: {e}")
            return {}

    async def wait_for_fill(self, order_id: str, timeout_s: float = 8.0,
                            poll_interval_s: float = 0.5) -> dict:
        """
        Faz polling de get_order_status até a ordem sair do estado
        ativo (FILLED, CANCELED ou REJECTED) ou até o timeout.

        Retorna {"filled": bool, "status": dict, "timed_out": bool}.
        Uma ordem MARKET na KuCoin executa quase instantaneamente —
        o timeout aqui serve apenas para detectar os casos anômalos
        em que ela fica presa (ex: liquidez insuficiente).
        """
        deadline = time.time() + timeout_s
        last = {}
        while time.time() < deadline:
            last = await self.get_order_status(order_id)
            if last.get("_synthetic"):
                return {"filled": True, "status": last, "timed_out": False}
            is_active = last.get("isActive", True)
            filled_sz = float(last.get("filledSize", 0) or 0)
            cancel_ex = last.get("cancelExist", False)
            if not is_active:
                return {
                    "filled": filled_sz > 0 and not cancel_ex,
                    "status": last, "timed_out": False,
                }
            await asyncio.sleep(poll_interval_s)
        log.warning(
            f"⏱️ wait_for_fill {order_id}: timeout após {timeout_s}s "
            f"— último status: {last}"
        )
        return {"filled": False, "status": last, "timed_out": True}

    async def get_orderbook(self, symbol: str, depth: int = 20) -> dict:
        kc_sym = to_kucoin(symbol)
        data   = await self._get("/api/v1/level2/depth20", {"symbol": kc_sym})
        ob = {
            "b": [[str(b[0]), str(b[1])] for b in data.get("bids", [])[:depth]],
            "a": [[str(a[0]), str(a[1])] for a in data.get("asks", [])[:depth]],
        }
        self._ob_cache[symbol] = ob
        return ob

    # ── Posições abertas ──────────────────────────────────────────
    async def get_positions(self) -> list:
        """
        Retorna posições abertas formatadas igual ao BybitClient.
        CORRIGIDO: trata resposta vazia e erro 400004 sem quebrar o engine.
        """
        # IMPORTANT: an empty successful KuCoin response means "flat", but
        # an empty dict from _get() can also mean REST/auth/rate-limit failure.
        # Those states must never be conflated because IntegrityGuard treats
        # a returned list as confirmed exchange exposure.
        data = await self._get("/api/v1/positions", auth=True)

        if isinstance(data, list):
            raw = data
        elif isinstance(data, dict) and isinstance(data.get("data"), list):
            raw = data["data"]
        else:
            raise RuntimeError(
                "POSITIONS_UNCONFIRMED: KuCoin positions payload unavailable "
                "or malformed"
            )

        positions = []

        for p in raw:
            try:
                qty = float(p.get("currentQty", 0))
                if qty == 0:
                    continue
                kc_sym = p.get("symbol", "")
                _entry = float(p.get("avgEntryPrice", 0))
                positions.append({
                    "symbol":           to_standard(kc_sym),
                    "side":             "Buy" if qty > 0 else "Sell",
                    "size":             abs(qty),
                    "entryPrice":       _entry,
                    # Alias para compatibilidade com código que esperava o
                    # formato Bybit (auditoria #2)
                    "avgPrice":         _entry,
                    "markPrice":        float(p.get("markPrice", 0)),
                    "unrealisedPnl":    float(p.get("unrealisedPnl", 0)),
                    "leverage":         float(p.get("realLeverage", 1)),
                    # ADICIONADO (auditoria #2): liquidationPrice não era
                    # exposto, fazendo o engine calcular SL com liq=0.
                    "liquidationPrice": float(p.get("liquidationPrice", 0)),
                    "liqPrice":         float(p.get("liquidationPrice", 0)),
                    # Stops efetivamente aplicados na exchange — permitem
                    # auditar se a posição está protegida de fato.
                    "stopLoss":         float(p.get("stopLoss",   0) or 0),
                    "takeProfit":       float(p.get("takeProfit", 0) or 0),
                    "posMargin":        float(p.get("posMargin", 0)),
                })
            except (ValueError, TypeError) as _e:
                # auditoria #10: posição com campos inválidos agora é logada
                log.warning(
                    f"posição descartada por campo inválido: {_e} | "
                    f"dados: {str(p)[:150]}"
                )
                continue
        return positions

    # ── WebSocket ─────────────────────────────────────────────────
    async def _seed_kline_cache(self, symbols: list, intervals: list):
        """
        Popula o cache de klines via REST no startup.

        O WS (limitCandle) só entrega candles FECHADOS — do zero, levaria
        horas ou dias para o cache ficar utilizável. Este seed carrega o
        histórico uma vez; o WS mantém atualizado depois.

        Executa com concorrência limitada para respeitar o rate limit.
        """
        _LIMITS = {"15": 200, "60": 150, "240": 120}
        sem = asyncio.Semaphore(3)
        ok = fail = 0

        async def _one(sym: str, iv: str):
            nonlocal ok, fail
            async with sem:
                try:
                    lim  = _LIMITS.get(str(iv), 120)
                    data = await self.get_klines(sym, str(iv), lim)
                    if data and len(data) >= 20:
                        self._kline_cache[(sym, str(iv))] = list(data)
                        ok += 1
                    else:
                        fail += 1
                        log.debug(f"seed {sym} {iv}m: apenas {len(data or [])} velas")
                except Exception as e:
                    fail += 1
                    log.debug(f"seed {sym} {iv}m: {e}")

        t0 = time.time()
        log.info(
            f"🌱 Populando cache via REST: {len(symbols)} pares × "
            f"{len(intervals)} intervalos..."
        )
        await asyncio.gather(
            *[_one(s, iv) for s in symbols for iv in intervals],
            return_exceptions=True,
        )
        total = sum(len(v) for v in self._kline_cache.values())
        log.info(
            f"🌱 Cache populado em {time.time()-t0:.1f}s: {ok} séries OK, "
            f"{fail} falhas, {total} candles no total"
        )
        if fail > ok:
            log.warning(
                f"⚠️ Mais falhas que sucessos no seed — o bot dependerá "
                f"de REST a cada scan"
            )

    async def _get_ws_token(self) -> str:
        """
        KuCoin WS requer token obtido via REST (válido 18h).
        POST /api/v1/bullet-private → retorna token + endpoint WS.
        """
        now = time.time()
        if self._ws_token and (now - self._ws_token_ts) < 60000:
            return self._ws_token   # reusa token válido

        await self._ensure_session()
        # Market-data websocket token is public in paper mode. No account
        # mutation/authentication is needed for simulated trading.
        endpoint = "/api/v1/bullet-public" if PAPER_TRADE else "/api/v1/bullet-private"
        headers  = {} if PAPER_TRADE else self._auth_headers("POST", endpoint, "")
        url      = REST_BASE + endpoint
        try:
            async with self._session.post(url, headers=headers) as r:
                data = (await r.json()).get("data", {})
            self._ws_token    = data.get("token", "")
            self._ws_token_ts = now

            # ══════════════════════════════════════════════════════
            # BUG CRÍTICO CORRIGIDO — ENDPOINT WS ERRADO
            #
            # A URL estava hardcoded como wss://ws-api.kucoin.com/endpoint,
            # que é o host do KuCoin SPOT. O Futures usa outro host.
            #
            # A resposta de bullet-private já traz o endereço correto em
            # instanceServers[0].endpoint — o código descartava esse campo.
            #
            # Efeito: o WS conectava (ou falhava em silêncio) no host
            # errado e NENHUM kline chegava. Daí o
            # "WS cache miss (15m=0 1h=0 4h=0)" em todos os pares, com o
            # bot dependendo 100% de REST a cada scan.
            # ══════════════════════════════════════════════════════
            servers = data.get("instanceServers") or []
            if servers:
                self._ws_endpoint = servers[0].get("endpoint", WS_BASE)
                # pingInterval vem em ms e define o ritmo exigido pela KuCoin
                self._ws_ping_ms  = int(servers[0].get("pingInterval", 18000))
            else:
                self._ws_endpoint = WS_BASE
                self._ws_ping_ms  = 18000

            if self._ws_token:
                log.info(
                    f"✓ WS token KuCoin obtido ({len(self._ws_token)} chars) "
                    f"| endpoint={self._ws_endpoint} "
                    f"| ping={self._ws_ping_ms}ms"
                )
            else:
                log.error("❌ WS token VAZIO — autenticação KuCoin falhou (verifique credenciais)")
            return self._ws_token
        except Exception as e:
            log.error(f"_get_ws_token: {e}")
            return ""

    async def start_websocket(self, symbols: list, intervals: list = None):
        """Inicia WebSocket para klines e tickers de todos os símbolos."""
        if not symbols:
            log.error("start_websocket: lista de símbolos vazia")
            return
        asyncio.create_task(self._ws_loop(symbols, intervals or ["15", "60"]))
        log.info(f"🔌 WebSocket KuCoin iniciando para {len(symbols)} símbolos")

    # ══════════════════════════════════════════════════════════════════
    # WEBSOCKET PRIVADO DE ORDENS (Fase Final — fecha o gap de
    # PRIVATE_WS_RECONCILIATION, marcado FAIL na auditoria anterior)
    #
    # FONTE OFICIAL (busca realizada nesta sessão, não inventada):
    #   docs.kucoin.com/futures — tópico /contractMarket/tradeOrders:{symbol}
    #   subject: "symbolOrderChange"
    #   campos:  orderId, clientOid, type (open/match/filled/canceled/
    #            update), status (open/match/done), filledSize,
    #            matchSize, matchPrice, side, size
    #
    # O token de autenticação é o MESMO de bullet-private já usado pelo
    # WS público (_get_ws_token) — a KuCoin usa um único token para
    # ambos os tipos de canal, diferenciados pelo campo privateChannel
    # na mensagem de subscribe.
    #
    # DESENHO: conexão SEPARADA do WS público de market data. Isolar
    # evita que uma falha aqui afete o fluxo de candles já validado, e
    # vice-versa.
    #
    # FONTE DE VERDADE: o WS é uma CONFIRMAÇÃO ADICIONAL, não a única.
    # wait_for_fill() (REST) continua sendo chamado em _open() — o WS
    # apenas acelera a detecção e mantém o OrderRegistry atualizado
    # entre polls. Um evento WS nunca sobrescreve um estado mais novo
    # (checado por updated_at) nem contorna o REST em caso de dúvida.
    # ══════════════════════════════════════════════════════════════════

    def start_private_websocket(self, order_registry, symbols: list):
        """
        Inicia a conexão privada de ordens em background.

        order_registry: bot.order_state.OrderRegistry — a máquina de
        estados JÁ existente (não cria uma segunda, conforme exigido).
        """
        if PAPER_TRADE:
            log.info("🔌 WS privado de ordens: PAPER_TRADE — não conecta")
            return
        if not API_KEY:
            log.warning("🔌 WS privado de ordens: sem credenciais — não conecta")
            return
        self._order_registry = order_registry
        asyncio.create_task(self._private_ws_loop(symbols))
        log.info(f"🔌 WS privado de ordens iniciando para {len(symbols)} símbolos")

    async def _private_ws_loop(self, symbols: list):
        """Loop de reconexão do canal privado de ordens."""
        import websockets

        backoff = 2
        while True:
            try:
                token = await self._get_ws_token()
                if not token:
                    await asyncio.sleep(backoff)
                    backoff = min(60, backoff * 2)
                    continue

                _ep = getattr(self, "_ws_endpoint", None) or WS_BASE
                ws_url = f"{_ep}?token={token}&connectId=bgx7-priv-{int(time.time())}"

                async with websockets.connect(
                    ws_url, ping_interval=None, close_timeout=5
                ) as ws:
                    backoff = 2
                    log.info("✅ WS privado de ordens conectado")

                    _kc_syms = [to_kucoin(s) for s in symbols]
                    for _sym in _kc_syms:
                        await ws.send(json.dumps({
                            "id":             str(int(time.time() * 1000000)),
                            "type":           "subscribe",
                            "topic":          f"/contractMarket/tradeOrders:{_sym}",
                            "privateChannel": True,   # canal PRIVADO — exige token autenticado
                            "response":       True,
                        }))
                        await asyncio.sleep(0.12)

                    async def _priv_ping():
                        while True:
                            await asyncio.sleep(18)
                            try:
                                await ws.send(json.dumps({
                                    "id": str(int(time.time() * 1000)),
                                    "type": "ping",
                                }))
                            except Exception:
                                break

                    _ping_task = asyncio.create_task(_priv_ping())
                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                                await self._handle_private_order_event(msg)
                            except json.JSONDecodeError:
                                continue
                            except Exception as e:
                                log.debug(f"WS privado parse: {e}")
                    finally:
                        if not _ping_task.done():
                            _ping_task.cancel()

            except Exception as e:
                log.warning(f"WS privado de ordens desconectado: {e} — reconectando em {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)

    async def _handle_private_order_event(self, msg: dict):
        """
        Processa um evento do canal privado /contractMarket/tradeOrders.

        Mapeamento de estados KuCoin → OrderState (Fase 3/4):
          type="open"                → SUBMITTED
          type="match" (parcial)     → PARTIALLY_FILLED
          status="done" + filled>0   → FILLED
          status="done" + filled=0   → CANCELLED (cancelado sem fill)
          type="canceled"            → CANCELLED

        NÃO sobrescreve um estado mais novo: compara updated_at antes
        de aplicar a transição (Fase 2, item 11 — "impedir eventos
        antigos de sobrescrever estado mais novo").
        """
        if msg.get("subject") != "symbolOrderChange":
            return
        data = msg.get("data", {}) or {}
        order_id   = data.get("orderId", "")
        client_oid = data.get("clientOid", "")
        if not order_id and not client_oid:
            return

        registry = getattr(self, "_order_registry", None)
        if registry is None:
            return

        # Correlação primária por orderId (Fase 2); fallback client_oid
        mo = registry.get_by_order_id(order_id) if order_id else None
        if mo is None and client_oid:
            mo = registry.get(client_oid)
        if mo is None:
            # Evento de uma ordem que este processo não rastreia (ex:
            # ordem manual do usuário, ou processo reiniciado). Não é
            # erro — apenas não há o que reconciliar aqui.
            log.debug(
                f"WS privado: evento para ordem não rastreada "
                f"orderId={order_id} clientOid={client_oid}"
            )
            return

        if order_id:
            registry.index_order_id(order_id, mo.client_oid)

        _evt_ts = float(data.get("ts", 0) or 0) / 1e9 if data.get("ts") else time.time()
        # Evento mais antigo que a última atualização conhecida: ignora
        # (Fase 8, Caso F — evento antigo não pode sobrescrever novo).
        if mo.updated_at and _evt_ts and _evt_ts < mo.updated_at - 1.0:
            log.debug(
                f"WS privado: evento antigo ignorado para "
                f"{mo.client_oid[:8]} (evt_ts={_evt_ts} < updated_at={mo.updated_at})"
            )
            return

        _type   = data.get("type", "")
        _status = data.get("status", "")
        filled  = float(data.get("filledSize", 0) or 0)
        match_sz = float(data.get("matchSize", 0) or 0)
        match_px = float(data.get("matchPrice", 0) or 0)

        try:
            if _type == "canceled" or (_status == "done" and filled == 0):
                mo.transition(OrderState.CANCELLED, source="WS")
            elif _status == "done" and filled > 0:
                mo.transition(
                    OrderState.FILLED, filled_qty=filled,
                    avg_price=match_px if match_px else mo.avg_price,
                    source="WS",
                )
                log.info(
                    f"✅ [FILLED] source=PRIVATE_WS "
                    f"clientOid={mo.client_oid} orderId={order_id} "
                    f"symbol={mo.symbol} filledSize={filled} "
                    f"matchPrice={match_px} topic=/contractMarket/"
                    f"tradeOrders:{data.get('symbol','?')}"
                )
            elif _type == "match":
                mo.transition(
                    OrderState.PARTIALLY_FILLED, filled_qty=filled,
                    avg_price=match_px if match_px else mo.avg_price,
                    source="WS",
                )
            elif _type == "open" and mo.state == OrderState.SUBMITTING:
                mo.transition(OrderState.SUBMITTED, order_id=order_id, source="WS")
        except InvalidTransition as e:
            # Transição impossível pelo evento WS — não é bug do WS
            # necessariamente, pode ser reconexão com evento fora de
            # ordem. Loga e mantém o estado atual (fail-safe).
            log.warning(f"WS privado: transição inválida ignorada: {e}")

        persist_callback = getattr(registry, "persist_callback", None)
        if callable(persist_callback):
            await persist_callback(mo)

    async def _ws_loop(self, symbols: list, intervals: list):
        """Loop principal de reconexão WebSocket com backoff exponencial."""
        import websockets

        while True:
            try:
                token = await self._get_ws_token()
                if not token:
                    await asyncio.sleep(10)
                    continue

                # Usa o endpoint retornado pela própria KuCoin (Futures),
                # não o host do Spot que estava hardcoded.
                _ep = getattr(self, "_ws_endpoint", None) or WS_BASE
                ws_url = f"{_ep}?token={token}&connectId=bgx-{int(time.time())}"

                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                    self._ws_retry = 0
                    self._connected = True
                    log.info(f"✅ WebSocket KuCoin conectado")

                    # Subscrever klines por símbolo e intervalo (batches de 10)
                    # ══════════════════════════════════════════════════
                    # BUG CORRIGIDO — FORMATO DA SUBSCRIÇÃO (code=400)
                    #
                    # O código juntava TÓPICOS DIFERENTES com vírgula:
                    #   "/contractMarket/limitCandle:X_15min,/contractMarket/tickerV2:Y"
                    #
                    # Na KuCoin a vírgula separa SÍMBOLOS dentro do MESMO
                    # canal, não tópicos distintos:
                    #   ✓ /contractMarket/tickerV2:XBTUSDTM,ETHUSDTM
                    #   ✗ /contractMarket/limitCandle:A,/contractMarket/tickerV2:B
                    #
                    # Resultado: TODAS as subscrições eram rejeitadas com
                    # code=400 e o cache de klines ficava permanentemente
                    # vazio ("WS cache miss 15m=0 1h=0 4h=0").
                    #
                    # Agora: limitCandle é enviado UM POR MENSAGEM (o
                    # intervalo faz parte do sufixo do símbolo) e o ticker
                    # agrupa os símbolos num único tópico.
                    # ══════════════════════════════════════════════════
                    subs = []
                    for sym in symbols:
                        kc_sym = to_kucoin(sym)
                        for interval in intervals:
                            ws_iv = WS_INTERVAL_MAP.get(str(interval))
                            if not ws_iv:
                                log.warning(f"intervalo {interval} sem mapeamento WS — pulando")
                                continue
                            subs.append(f"/contractMarket/limitCandle:{kc_sym}_{ws_iv}")

                    # Ticker: um único tópico com todos os símbolos
                    _tick_syms = ",".join(to_kucoin(s) for s in symbols)
                    if _tick_syms:
                        subs.append(f"/contractMarket/tickerV2:{_tick_syms}")

                    # BUG: os contadores eram zerados APÓS o envio, apagando
                    # os acks que já haviam chegado durante a subscrição.
                    # Precisam ser inicializados ANTES.
                    self._ws_acks   = 0
                    self._ws_errors = 0
                    _n_subs = len(subs)
                    log.info(f"📡 Subscrevendo {_n_subs} tópicos no WS...")

                    # Uma mensagem por tópico — a KuCoin não aceita
                    # canais diferentes na mesma subscrição.
                    for _t in subs:
                        await ws.send(json.dumps({
                            "id":             str(int(time.time() * 1000000)),
                            "type":           "subscribe",
                            "topic":          _t,
                            "privateChannel": False,
                            "response":       True,
                        }))
                        # Espaçamento leve: a KuCoin limita ~100 msgs/10s
                        await asyncio.sleep(0.12)

                    async def _report_subs():
                        # Margem generosa: os acks podem chegar depois do
                        # envio da última subscrição.
                        await asyncio.sleep(_n_subs * 0.12 + 12)
                        a = getattr(self, "_ws_acks", 0)
                        e = getattr(self, "_ws_errors", 0)

                        if e:
                            log.error(
                                f"❌ WS: {e} subscrições REJEITADAS de {_n_subs} "
                                f"({a} aceitas) — cache ficará vazio nesses tópicos"
                            )
                        elif a == 0:
                            log.error(
                                f"❌ WS: NENHUM ack para {_n_subs} subscrições — "
                                f"klines virão só por REST"
                            )
                        elif a < _n_subs:
                            log.warning(
                                f"⚠️ WS: {a}/{_n_subs} subscrições confirmadas "
                                f"({_n_subs - a} sem ack) — esses pares/intervalos "
                                f"dependerão de REST"
                            )
                        else:
                            log.info(f"✅ WS: {a}/{_n_subs} subscrições confirmadas")

                        # Diagnóstico do cache: os acks confirmam a subscrição,
                        # mas só a chegada de candles prova que os dados fluem.
                        await asyncio.sleep(30)
                        _keys = list(self._kline_cache.keys())
                        _vazios = [k for k in _keys if not self._kline_cache.get(k)]
                        _total_candles = sum(len(v) for v in self._kline_cache.values())
                        if _total_candles == 0:
                            log.error(
                                f"❌ WS: 30s após subscrever, cache VAZIO "
                                f"({len(_keys)} chaves). Subscrição aceita mas "
                                f"nenhum candle chegou."
                            )
                        else:
                            log.info(
                                f"📊 WS cache: {_total_candles} candles em "
                                f"{len(_keys) - len(_vazios)}/{len(_keys)} séries"
                            )

                    asyncio.create_task(_report_subs())

                    # ══════════════════════════════════════════════════
                    # SEED DO CACHE VIA REST (essencial)
                    #
                    # /contractMarket/limitCandle só envia um candle quando
                    # ele FECHA. Partindo do zero, o cache levaria:
                    #     15M → 15 horas para 60 velas
                    #     1H  → 50 horas para 50 velas
                    #     4H  → 8 dias   para 50 velas
                    #
                    # Ou seja, o WS sozinho nunca encheria o cache em tempo
                    # útil — e cada scan caía em "WS cache miss" + REST.
                    #
                    # O seed carrega o histórico uma vez via REST; o WS
                    # mantém atualizado a partir dali. Roda em background
                    # para não atrasar a conexão.
                    # ══════════════════════════════════════════════════
                    if not getattr(self, "_cache_seeded", False):
                        self._cache_seeded = True
                        asyncio.create_task(self._seed_kline_cache(symbols, intervals))

                    # Ping de aplicação a cada 20s (exigência da KuCoin).
                    #
                    # P1 CORRIGIDO — VAZAMENTO DE TASK:
                    # a task era criada a cada reconexão sem cancelar a
                    # anterior. Após N reconexões, N tasks de ping ativas
                    # tentando usar websockets já fechados — consumo de
                    # memória e ruído crescentes em operação 24/7.
                    async def _ping():
                        while True:
                            # Intervalo vindo da própria KuCoin (pingInterval),
                            # com margem de segurança de 20%.
                            await asyncio.sleep(
                                max(5, getattr(self, "_ws_ping_ms", 18000) / 1000 * 0.8)
                            )
                            try:
                                await ws.send(json.dumps({
                                    "id":   str(int(time.time() * 1000)),
                                    "type": "ping",
                                }))
                            except Exception:
                                break

                    # Cancela ping órfão de uma conexão anterior
                    _old_ping = getattr(self, "_ping_task", None)
                    if _old_ping and not _old_ping.done():
                        _old_ping.cancel()
                    self._ping_task = asyncio.create_task(_ping())

                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                                await self._handle_ws_message(msg)
                            except json.JSONDecodeError as e:
                                # Mensagem malformada não deve derrubar o loop
                                log.debug(f"WS JSON inválido: {e}")
                            except Exception as e:
                                log.debug(f"WS parse: {e}")
                    finally:
                        # Garante que o ping morre junto com a conexão
                        if self._ping_task and not self._ping_task.done():
                            self._ping_task.cancel()

            except Exception as e:
                self._ws_retry += 1
                wait = min(2 ** self._ws_retry, 60)
                log.warning(
                    f"⚡ WebSocket KuCoin desconectado: {e} "
                    f"— tentativa #{self._ws_retry}, reconectando em {wait}s"
                )
                self._connected = False
                await asyncio.sleep(wait)

    async def _handle_ws_message(self, msg: dict):
        """Processa mensagens WebSocket e atualiza caches."""
        topic  = msg.get("topic", "")
        data   = msg.get("data", {})
        m_type = msg.get("type", "")

        # ══════════════════════════════════════════════════════════
        # BUG CORRIGIDO — ERRO DE SUBSCRIÇÃO IGNORADO
        #
        # Se a KuCoin rejeita um tópico (nome inválido, símbolo
        # inexistente, formato de intervalo errado), ela responde
        # {"type":"error","code":...,"data":"..."}. O código descartava
        # essa mensagem, então uma subscrição rejeitada ficava INVISÍVEL
        # — o cache seguia vazio ("WS cache miss 15m=0 1h=0 4h=0") sem
        # que ninguém soubesse o motivo.
        # ══════════════════════════════════════════════════════════
        if m_type == "error":
            log.error(
                f"❌ WS KuCoin REJEITOU subscrição: "
                f"code={msg.get('code')} data={str(msg.get('data'))[:200]} "
                f"| id={msg.get('id')}"
            )
            self._ws_errors = getattr(self, "_ws_errors", 0) + 1
            return

        if m_type == "ack":
            # Confirmação de subscrição aceita — conta para diagnóstico
            self._ws_acks = getattr(self, "_ws_acks", 0) + 1
            log.debug(f"✓ WS ack #{self._ws_acks} (id={msg.get('id')})")
            return

        if m_type in ("welcome", "pong"):
            return

        # ── Candles / Klines ──────────────────────────────────────
        if "andle" in topic and "candles" in data:
            # topic: /contractMarket/limitCandle:XBTUSDTM_15min
            parts     = topic.split(":")[-1].split("_")
            kc_sym    = "_".join(parts[:-1])
            ws_iv     = parts[-1]                    # ex: "15min"
            std_sym   = to_standard(kc_sym)

            # Converte o intervalo do formato WS (15min) para o interno (15)
            interval = WS_INTERVAL_MAP_REV.get(ws_iv, ws_iv)

            candles = data["candles"]
            # KuCoin WS: [start_time(SEGUNDOS), open, close, high, low, volume, amount]
            # Atenção: a ordem difere do REST, que é [ts, open, high, low, close, vol]
            _ts = int(float(candles[0]))
            # CORRIGIDO: WS envia em SEGUNDOS (10 dígitos) e o REST em ms (13).
            # Sem normalizar, a comparação do candle atual nunca batia e o cache
            # acumulava duplicatas em vez de atualizar o candle em formação.
            if _ts < 1e11:
                _ts *= 1000

            kline = {
                "ts": _ts,
                "o":  float(candles[1]),
                "c":  float(candles[2]),   # close vem ANTES de high/low no WS
                "h":  float(candles[3]),
                "l":  float(candles[4]),
                "v":  float(candles[5]),
            }
            self._last_ws_update = time.time()
            key = (std_sym, str(interval))
            if key not in self._kline_cache:
                self._kline_cache[key] = deque(maxlen=500)
            cache = self._kline_cache[key]

            # ══════════════════════════════════════════════════════
            # BUG CORRIGIDO (Fase 4G, cenário E) — EVENTOS FORA DE ORDEM
            #
            # O código só comparava com o ÚLTIMO candle do cache. Um
            # evento atrasado (comum após reconexão ou congestionamento)
            # era ANEXADO NO FIM, quebrando a cronologia da série.
            #
            # Consequência: EMAs, RSI, ADX e ATR calculados sobre uma
            # série desordenada produzem valores incorretos — a mesma
            # classe de falha do bug de candles invertidos.
            #
            # Agora: candle existente é atualizado onde está; candle
            # atrasado é inserido na posição cronológica correta.
            # ══════════════════════════════════════════════════════
            _ts_novo = kline["ts"]

            if cache and cache[-1]["ts"] == _ts_novo:
                cache[-1] = kline                    # caso comum: candle atual
            elif cache and _ts_novo > cache[-1]["ts"]:
                cache.append(kline)                  # caso comum: candle novo
            elif not cache:
                cache.append(kline)
            else:
                # Evento ATRASADO ou duplicado de um candle anterior.
                _idx = None
                for _i in range(len(cache) - 1, -1, -1):
                    if cache[_i]["ts"] == _ts_novo:
                        _idx = _i
                        break
                    if cache[_i]["ts"] < _ts_novo:
                        break

                if _idx is not None:
                    cache[_idx] = kline              # atualiza no lugar
                else:
                    # Reinsere mantendo a ordem cronológica. deque não
                    # tem insert eficiente em posição arbitrária, então
                    # reconstruímos ordenado.
                    _itens = list(cache)
                    _itens.append(kline)
                    _itens.sort(key=lambda k: k["ts"])
                    cache.clear()
                    cache.extend(_itens)
                    log.debug(
                        f"WS {std_sym} {interval}m: candle atrasado "
                        f"reinserido em ordem (ts={_ts_novo})"
                    )

        # ── Ticker ────────────────────────────────────────────────
        elif "icker" in topic:
            kc_sym  = topic.split(":")[-1]
            std_sym = to_standard(kc_sym)
            self._ticker_cache[std_sym] = {
                "symbol":    std_sym,
                "lastPrice": float(data.get("price",        0)),
                "bid":       float(data.get("bestBidPrice", 0)),
                "ask":       float(data.get("bestAskPrice", 0)),
                "volume":    float(data.get("size",         0)),
            }

    # ── Cache stats ───────────────────────────────────────────────
    def get_cache_stats(self) -> dict:
        _age = (time.time() - self._last_ws_update) if self._last_ws_update else -1
        return {
            "ws_last_update_s": round(_age, 1) if _age >= 0 else None,
            "ws_healthy":       bool(self._last_ws_update and _age < 180),
            "kline_pairs": len(self._kline_cache),
            "tickers":     len(self._ticker_cache),
            "instruments": len(self._instruments),
            "ws_connected": self._connected,
            "ws_retries":   self._ws_retry,
        }

    # ── Encerramento ─────────────────────────────────────────────
    async def sync_time(self) -> bool:
        """
        Sincroniza o relógio local com o servidor KuCoin.
        KuCoin rejeita requisições com timestamp fora de ±5s da hora do servidor,
        retornando 400005 Invalid KC-API-SIGN (mensagem enganosa).
        Railway/containers frequentemente têm drift de relógio.
        """
        try:
            await self._ensure_session()
            local_before = int(time.time() * 1000)
            async with self._session.get(REST_BASE + "/api/v1/timestamp") as r:
                data = await r.json()
            local_after = int(time.time() * 1000)
            server_ts   = int(data.get("data", 0))
            if server_ts <= 0:
                log.warning("sync_time: servidor não retornou timestamp")
                return False
            # Compensa latência da requisição (metade do round-trip)
            local_mid           = (local_before + local_after) // 2
            self._time_offset_ms = server_ts - local_mid
            self._time_synced    = True
            log.info(
                f"🕐 Tempo sincronizado com KuCoin | offset={self._time_offset_ms}ms"
                + ("  ⚠️ DRIFT ALTO!" if abs(self._time_offset_ms) > 3000 else "")
            )
            return True
        except Exception as e:
            log.warning(f"sync_time falhou: {e}")
            return False

    def _now_ms(self) -> int:
        """Timestamp em ms ajustado pelo offset do servidor KuCoin."""
        return int(time.time() * 1000) + self._time_offset_ms

    async def ping(self) -> bool:
        """
        Verifica conectividade com a KuCoin API.
        Usa endpoint público de tempo do servidor (sem autenticação).
        Adicionado para compatibilidade com engine.py que chama client.ping().
        """
        try:
            data = await self._get("/api/v1/timestamp")
            return bool(data)
        except Exception as e:
            log.warning(f"KuCoin ping falhou: {e}")
            return False

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        log.info("👋 KuCoinClient encerrado")
