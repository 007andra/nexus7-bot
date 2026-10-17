import os
import asyncio
import time
from bot import telegram_transport
from bot.config import cfg
from bot.logger import log


# ── Rate limiting global ─────────────────────────────────────────
# Telegram permite ~30 msgs/segundo por bot, mas na prática
# muitas msgs seguidas causa HTTP 429 (Too Many Requests)
# Solução: fila com delay mínimo entre mensagens + deduplicação

_notify_queue: asyncio.Queue = None
_last_sent_time: float = 0.0
_warned_no_creds: bool = False
_last_sent_hash: str   = ""
_MIN_INTERVAL: float   = 3.0   # mínimo 3s entre mensagens
_DEDUP_WINDOW: float   = 30.0  # ignorar msg idêntica nos últimos 30s


def _get_queue() -> asyncio.Queue:
    """Lazy init da fila para evitar problemas de event loop."""
    global _notify_queue
    if _notify_queue is None:
        _notify_queue = asyncio.Queue(maxsize=50)
    return _notify_queue


async def notify(text: str):
    """
    Envia mensagem para o Telegram com:
      - Rate limiting: mínimo 3s entre msgs
      - Deduplicação: msgs idênticas ignoradas por 30s
      - Entrega pelo transporte canônico (bot.telegram_transport): retry
        limitado com backoff+jitter, 429 retry_after, circuit breaker
      - Retorna DeliveryResult (verdadeiro só com ok:true do Telegram)
    """
    if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT:
        # Avisa uma única vez: sem isso, a ausência de mensagens era
        # indistinguível de "o bot não tem nada para reportar".
        global _warned_no_creds
        if not _warned_no_creds:
            _warned_no_creds = True
            log.warning(
                f"⚠️ Telegram DESATIVADO — "
                f"TELEGRAM_TOKEN={'ok' if cfg.TELEGRAM_TOKEN else 'AUSENTE'} "
                f"TELEGRAM_CHAT={'ok' if cfg.TELEGRAM_CHAT else 'AUSENTE'}"
            )
        return telegram_transport.DeliveryResult(False, "DISABLED", skipped="NO_CREDENTIALS")

    global _last_sent_time, _last_sent_hash

    # Deduplicação: ignorar mensagem idêntica recente
    msg_hash = str(hash(text[:100]))
    now = time.time()
    if msg_hash == _last_sent_hash and (now - _last_sent_time) < _DEDUP_WINDOW:
        log.debug(f"Telegram: mensagem duplicada ignorada (dedup {_DEDUP_WINDOW}s)")
        return telegram_transport.DeliveryResult(False, "DEDUP", skipped="DEDUP")

    # Rate limiting: garantir intervalo mínimo
    elapsed = now - _last_sent_time
    if elapsed < _MIN_INTERVAL:
        await asyncio.sleep(_MIN_INTERVAL - elapsed)

    # Canonical transport: same timeouts, retry policy, classification and
    # circuit breaker as every other emitter. Never raises.
    result = await telegram_transport.deliver(text, parse_mode="Markdown", source="notify_legacy")
    if result.sent:
        _last_sent_time = time.time()
        _last_sent_hash = msg_hash
    return result



# ── BOT ONLINE ────────────────────────────────────────────────────
async def online_msg(saldo: float, poder: float, pares: int, max_pos: int) -> str:
    return (
        f"🤖 *BGX Capital — ONLINE*\n"
        f"`{'━'*28}`\n"
        f"💰 Saldo:        `${saldo:,.2f} USDT`\n"
        f"⚡ Poder compra: `${poder:,.2f} USDT`\n"
        f"🔍 Pares:        `{pares} ativos`\n"
        f"📊 Max posições: `{max_pos}`\n"
        f"`{'━'*28}`\n"
        f"_Bot iniciado e escaneando o mercado..._"
    )


def _venue_label() -> str:
    """Human label of the active execution venue (never a hard-coded exchange)."""
    try:
        from bot import exchange

        return f"{exchange.EXCHANGE_NAME.capitalize()} {exchange.EXCHANGE_PRODUCT}"
    except Exception:  # noqa: BLE001 - notification text must not raise
        return "exchange"


# ── SINAL DE ENTRADA ──────────────────────────────────────────────
async def signal_msg(sig) -> str:
    icon = "🟢🚀" if sig.direction == "LONG" else "🔴🩸"
    dir_label = "COMPRA (LONG)" if sig.direction == "LONG" else "VENDA (SHORT)"
    return (
        f"{icon} *SINAL — {dir_label}*\n"
        f"`{'━'*28}`\n"
        f"📍 Par:          `{sig.symbol}`\n"
        f"💰 Entrada:      `${sig.entry:,.4f}`\n"
        f"🛑 Stop Loss:    `${sig.sl:,.4f}`\n"
        f"🎯 Take Profit:  `${sig.tp:,.4f}`\n"
        f"📊 R/R:          `1:{sig.rr:.1f}`\n"
        f"🧠 Score:        `{sig.score}/100`\n"
        f"💡 _{sig.reason}_\n"
        f"`{'━'*28}`\n"
        f"⚡ _Ordem enviada para a {_venue_label()}_"
    )


# ── ORDEM ABERTA ──────────────────────────────────────────────────
async def order_opened_msg(sig, qty: float, saldo: float, poder: float) -> str:
    icon = "🟢" if sig.direction == "LONG" else "🔴"
    return (
        f"{icon} *ORDEM ABERTA*\n"
        f"`{'━'*28}`\n"
        f"📍 Par:          `{sig.symbol}`\n"
        f"🧭 Direção:      `{sig.direction}`\n"
        f"💰 Entrada:      `${sig.entry:,.4f}`\n"
        f"📦 Quantidade:   `{qty}`\n"
        f"🛑 Stop Loss:    `${sig.sl:,.4f}`\n"
        f"🎯 Take Profit:  `${sig.tp:,.4f}`\n"
        f"📊 R/R:          `1:{sig.rr:.1f}`\n"
        f"🧠 Score:        `{sig.score}/100`\n"
        f"`{'━'*28}`\n"
        f"💼 Saldo atual:  `${saldo:,.2f} USDT`\n"
        f"⚡ Poder compra: `${poder:,.2f} USDT`"
    )


# ── TRADE FECHADO ─────────────────────────────────────────────────
async def close_msg(symbol: str, direction: str, pnl: float, pnl_pct: float,
                    exit_price: float, saldo: float = 0, poder: float = 0) -> str:
    icon = "💰✅" if pnl > 0 else "📉❌"
    resultado = "LUCRO" if pnl > 0 else "PREJUÍZO"
    return (
        f"{icon} *TRADE FECHADO — {resultado}*\n"
        f"`{'━'*28}`\n"
        f"📍 Par:          `{symbol}`\n"
        f"🧭 Direção:      `{direction}`\n"
        f"🏁 Saída:        `${exit_price:,.4f}`\n"
        f"💵 PnL:          `{'+' if pnl >= 0 else ''}${pnl:,.2f} USDT`\n"
        f"📈 PnL %:        `{'+' if pnl_pct >= 0 else ''}{pnl_pct:,.2f}%`\n"
        f"`{'━'*28}`\n"
        f"💼 Saldo atual:  `${saldo:,.2f} USDT`\n"
        f"⚡ Poder compra: `${poder:,.2f} USDT`\n"
        f"_Operação finalizada_"
    )


# ── RELATÓRIO DIÁRIO ──────────────────────────────────────────────
async def daily_report_msg(pnl_dia: float, saldo: float, poder: float,
                            trades: int, wins: int, meta: float, stop: float) -> str:
    icon = "📈" if pnl_dia >= 0 else "📉"
    win_rate = round(wins / trades * 100) if trades > 0 else 0
    pct_meta = round(pnl_dia / meta * 100) if meta > 0 else 0
    return (
        f"{icon} *RELATÓRIO DIÁRIO — BGX Capital*\n"
        f"`{'━'*28}`\n"
        f"💵 PnL do dia:   `{'+' if pnl_dia >= 0 else ''}${pnl_dia:,.2f} USDT`\n"
        f"🎯 Meta diária:  `${meta:,.0f}` ({pct_meta}% atingido)\n"
        f"📊 Trades:       `{trades}` ({wins} wins — {win_rate}% win rate)\n"
        f"`{'━'*28}`\n"
        f"💼 Saldo:        `${saldo:,.2f} USDT`\n"
        f"⚡ Poder compra: `${poder:,.2f} USDT`\n"
        f"🛑 Stop diário:  `${stop:,.0f}`\n"
        f"_Próximo relatório em 24h_"
    )


# ── META DIÁRIA ───────────────────────────────────────────────────
async def daily_target_msg(pnl: float, meta: float, saldo: float, poder: float) -> str:
    return (
        f"🎯 *META DIÁRIA BATIDA!*\n"
        f"`{'━'*28}`\n"
        f"💵 Lucro do dia: `+${pnl:,.2f} USDT`\n"
        f"🏆 Meta:         `${meta:,.0f} USDT`\n"
        f"💼 Saldo:        `${saldo:,.2f} USDT`\n"
        f"⚡ Poder compra: `${poder:,.2f} USDT`\n"
        f"`{'━'*28}`\n"
        f"_Modo conservador ativado até meia-noite UTC_"
    )


# ── STOP LOSS DIÁRIO ──────────────────────────────────────────────
async def daily_stop_msg(pnl: float, stop: float, saldo: float) -> str:
    return (
        f"🛑 *STOP-LOSS DIÁRIO ATINGIDO*\n"
        f"`{'━'*28}`\n"
        f"📉 Perda do dia: `${pnl:,.2f} USDT`\n"
        f"🚫 Limite:       `-${stop:,.0f} USDT`\n"
        f"💼 Saldo:        `${saldo:,.2f} USDT`\n"
        f"`{'━'*28}`\n"
        f"_Bot pausado até meia-noite UTC_"
    )


# ── DRAWDOWN ──────────────────────────────────────────────────────
async def drawdown_msg(dd_pct: float, saldo: float) -> str:
    return (
        f"⚠️ *DRAWDOWN ELEVADO*\n"
        f"`{'━'*28}`\n"
        f"📉 Drawdown:     `{dd_pct:.1%}`\n"
        f"💼 Saldo:        `${saldo:,.2f} USDT`\n"
        f"`{'━'*28}`\n"
        f"_Bot pausado para proteção de capital_"
    )


# ── PERDAS CONSECUTIVAS ───────────────────────────────────────────
async def consecutive_losses_msg(n: int, saldo: float, poder: float) -> str:
    return (
        f"⚠️ *{n} PERDAS CONSECUTIVAS*\n"
        f"`{'━'*28}`\n"
        f"💼 Saldo:        `${saldo:,.2f} USDT`\n"
        f"⚡ Poder compra: `${poder:,.2f} USDT`\n"
        f"`{'━'*28}`\n"
        f"_Bot continua operando com cautela_"
    )

# ── SPOOFING DETECTADO ────────────────────────────────────────────
async def spoofing_alert_msg(symbol: str, detail: str) -> str:
    return (
        f"🚨 *SPOOFING DETECTADO — {symbol}*\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"⚠️ Manipulação de orderbook identificada\n"
        f"📋 Detalhe: `{detail}`\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"_Entrada bloqueada por proteção anti-spoofing_"
    )


# ── ICEBERG DETECTADO ─────────────────────────────────────────────
async def iceberg_alert_msg(symbol: str, side: str, price: float) -> str:
    icon = "🐋" 
    return (
        f"{icon} *ICEBERG DETECTADO — {symbol}*\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"🔍 Ordem oculta no lado: `{side}`\n"
        f"📍 Nível de preço: `${price:,.4f}`\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"_Grande player identificado neste nível_"
    )


# ── ABSORÇÃO DETECTADA ────────────────────────────────────────────
async def absorption_alert_msg(symbol: str, direction: str,
                                strength: float, detail: str) -> str:
    icon = "🟢" if direction == "BULL" else "🔴"
    return (
        f"{icon} *ABSORÇÃO {direction} — {symbol}*\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"💪 Intensidade: `{strength:.0%}`\n"
        f"📋 `{detail}`\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"_Grande player absorvendo pressão oposta_"
    )

# ── SESSÃO DE MERCADO ─────────────────────────────────────────────
async def session_change_msg(session: str, quality: int,
                              emoji: str, description: str) -> str:
    return (
        f"{emoji} *SESSÃO: {session}*\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"📊 Qualidade: `{quality}%`\n"
        f"📋 _{description}_\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`"
    )


# ── CORRELAÇÃO BLOQUEADA ──────────────────────────────────────────
async def correlation_block_msg(symbol: str, conflict: str,
                                 group: str) -> str:
    return (
        f"⚠️ *CORRELAÇÃO BLOQUEADA — {symbol}*\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"📊 Grupo: `{group}`\n"
        f"🔗 Conflito: _{conflict}_\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"_Entrada bloqueada para evitar exposição dupla_"
    )


# ── TWITTER SENTIMENT ─────────────────────────────────────────────
async def twitter_sentiment_msg(sentiment: str, bull: int,
                                 bear: int, trending: list) -> str:
    icon = "🐂" if sentiment == "BULLISH" else "🐻" if sentiment == "BEARISH" else "🐦"
    return (
        f"{icon} *TWITTER/X — {sentiment}*\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"🟢 Bullish: `{bull}` menções\n"
        f"🔴 Bearish: `{bear}` menções\n"
        f"📈 Trending: _{', '.join(trending[:3]) if trending else 'sem dados'}_"
    )

# ── NOTÍCIA DE ALTO IMPACTO ───────────────────────────────────────
async def high_impact_news_msg(title: str, source: str, sentiment: str,
                                relevance: int, score_pts: int) -> str:
    icon = "🟢📰" if sentiment == "BULLISH" else "🔴📰" if sentiment == "BEARISH" else "📰"
    return (
        f"{icon} *NOTÍCIA ALTO IMPACTO*\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"📋 `{title[:100]}`\n"
        f"📰 Fonte: `{source}`\n"
        f"🎯 Sentimento: `{sentiment}`\n"
        f"⭐ Relevância: `{relevance}/100`\n"
        f"📊 Score: `{score_pts:+d}pts`\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`"
    )


# ── RESUMO DO PIPELINE DE NOTÍCIAS ───────────────────────────────
async def news_summary_msg(total: int, bull: int, bear: int,
                            sources: list, top_news: list) -> str:
    return (
        f"📰 *NEWS PIPELINE — BGX Capital*\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"📊 Total: `{total}` notícias processadas\n"
        f"🟢 Bullish: `{bull}` | 🔴 Bearish: `{bear}`\n"
        f"📡 Fontes: _{', '.join(sources[:4])}_\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        + "\n".join([
            f"• [{n['sentiment'][0]}] `{n['source']}` — {n['title'][:60]}"
            for n in top_news[:3]
        ])
    )


# ══════════════════════════════════════════════════════════════════
# NEXUS AI — Notificações no Telegram (seção 23: observabilidade)
# ══════════════════════════════════════════════════════════════════
#
# O bot escaneia ~12 pares a cada 20s. Notificar cada avaliação daria
# ~2.160 mensagens/hora e o Telegram bloquearia o bot em minutos
# (limite prático: ~20 msg/min por chat).
#
# Por isso há deduplicação: o mesmo veto, para o mesmo par, com o
# mesmo motivo, só é reenviado após NEXUS_VETO_COOLDOWN segundos.
# Se o MOTIVO mudar, a mensagem sai na hora — mudança de motivo é
# informação nova.
_nexus_veto_cache: dict = {}     # (symbol, motivo_curto) → timestamp
_nexus_score_cache: dict = {}    # symbol → (timestamp, fingerprint)
_nexus_msg_count:  dict = {}     # minuto → contagem (guarda de rate limit)

NEXUS_VETO_COOLDOWN = int(os.environ.get("NEXUS_VETO_COOLDOWN", "900"))   # 15min
NEXUS_SCORE_COOLDOWN = int(os.environ.get("NEXUS_SCORE_COOLDOWN", "180")) # 3min
NEXUS_MAX_MSG_MIN   = int(os.environ.get("NEXUS_MAX_MSG_MIN", "12"))      # msgs/min


def _nexus_rate_ok() -> bool:
    """Guarda de rate limit: no máximo NEXUS_MAX_MSG_MIN mensagens/minuto."""
    import time as _t
    minute = int(_t.time() // 60)
    # limpa minutos antigos
    for k in [k for k in _nexus_msg_count if k < minute - 2]:
        _nexus_msg_count.pop(k, None)
    cur = _nexus_msg_count.get(minute, 0)
    if cur >= NEXUS_MAX_MSG_MIN:
        return False
    _nexus_msg_count[minute] = cur + 1
    return True


def _grade_icon(grade: str) -> str:
    return {"A+": "🏆", "A": "🥇", "B": "🥈", "C": "🥉"}.get(grade, "⚪")


async def nexus_approved_msg(d: dict) -> str:
    """Mensagem de APROVAÇÃO do NEXUS AI — setup autorizado."""
    icon = "🟢" if d.get("decision") == "LONG" else "🔴"
    models = d.get("models", [])
    used   = [m for m in models if m.get("available")]

    lines = []
    for m in used:
        if m.get("name") == "RISK_REGIME":
            continue
        dir_ic = {"LONG": "🟩", "SHORT": "🟥"}.get(m.get("direction"), "⬜")
        lines.append(f"  {dir_ic} `{m['name'][:12]:<12}` {m.get('confidence',0):>5.1f}")

    return (
        f"🧠 *NEXUS AI — SETUP APROVADO* {icon}\n"
        f"`{'━'*28}`\n"
        f"📍 Par:        `{d.get('symbol')}`\n"
        f"🧭 Direção:    `{d.get('decision')}`\n"
        f"{_grade_icon(d.get('setup_grade',''))} Grade:      "
        f"`{d.get('setup_grade')}` (score `{d.get('setup_quality',0):.1f}`)\n"
        f"🎯 Confiança:  `{d.get('confidence',0):.0f}%`\n"
        f"🌐 Regime:     `{d.get('market_regime')}`\n"
        f"🔗 Compat:     `{d.get('regime_compat',0):.0f}%`\n"
        f"`{'─'*28}`\n"
        f"💰 Entrada:    `${d.get('entry',0):,.4f}`\n"
        f"🛑 Stop:       `${d.get('stop_loss',0):,.4f}`\n"
        f"🎯 Alvo:       `${d.get('take_profit',0):,.4f}`\n"
        f"⚖️ R:R líq:    `{d.get('risk_reward',0):.2f}`\n"
        f"📈 EV:         `{d.get('expected_value',0):+.3f}%`\n"
        f"📡 Dados:      `{d.get('data_quality',0):.0f}/100`\n"
        f"`{'─'*28}`\n"
        f"*Modelos ({len(used)} ativos):*\n" + "\n".join(lines) + "\n"
        f"`{'━'*28}`\n"
        f"_{'; '.join(d.get('reasoning', [])[-2:])}_"
    )


def _nexus_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _nexus_plain(value, limit: int = 150) -> str:
    text = str(value or "").replace("`", "'").replace("*", "").replace("_", " ")
    return text[:limit]


async def nexus_score_msg(d: dict) -> str:
    """Compact score telemetry for non-terminal NEXUS WAIT evaluations."""
    final_score = _nexus_number(d.get("final_score"))
    estimated = _nexus_number(d.get("estimated_final"))
    if final_score is not None and final_score > 0:
        score_text = f"{final_score:.1f}/100 (final)"
    elif estimated is not None:
        score_text = f"{estimated:.1f}/100 (estimado)"
    else:
        score_text = "N/A"

    confidence = _nexus_number(d.get("confidence"))
    rr_net = _nexus_number(d.get("rr_net"))
    ev_pct = _nexus_number(d.get("ev_pct"))
    dq = _nexus_number(d.get("data_quality"))
    reason = _nexus_plain(d.get("reason") or "aguardando confirmação")
    mtf = _nexus_plain(d.get("mtf") or "N/A", 90)

    return (
        f"🧠 *NEXUS AI — SCORE*\n"
        f"📍 Par: `{d.get('symbol', '?')}`\n"
        f"⏳ Estado: `WAIT`\n"
        f"📊 Score: `{score_text}`\n"
        f"🎯 Confiança: `{'N/A' if confidence is None else f'{confidence:.1f}%'}`\n"
        f"⚖️ R:R líq: `{'N/A' if rr_net is None else f'{rr_net:.2f}'}`\n"
        f"📈 EV: `{'N/A' if ev_pct is None else f'{ev_pct:+.3f}%'}`\n"
        f"📡 Dados: `{'N/A' if dq is None else f'{dq:.0f}/100'}`\n"
        f"🌐 Regime: `{d.get('regime', 'N/A')}`\n"
        f"🧭 MTF: `{mtf}`\n"
        f"💡 {reason}"
    )


async def notify_nexus_score(d: dict) -> bool:
    """Send deduplicated WAIT score telemetry without changing trading state."""
    if os.environ.get("NEXUS_SCORE_TELEGRAM", "true").lower() != "true":
        return False

    decision = str(d.get("decision") or "").upper()
    if decision != "WAIT":
        return False

    sym = str(d.get("symbol") or "?")
    final_score = _nexus_number(d.get("final_score"))
    estimated = _nexus_number(d.get("estimated_final"))
    display_score = final_score if final_score is not None and final_score > 0 else estimated
    confidence = _nexus_number(d.get("confidence"))
    rr_net = _nexus_number(d.get("rr_net"))
    reason = _nexus_plain(d.get("reason"), 80)
    fingerprint = (
        decision,
        None if display_score is None else round(display_score, 1),
        None if confidence is None else round(confidence),
        None if rr_net is None else round(rr_net, 2),
        reason,
    )

    now = time.time()
    last = _nexus_score_cache.get(sym)
    if last and last[1] == fingerprint and (now - last[0]) < NEXUS_SCORE_COOLDOWN:
        return False

    if not _nexus_rate_ok():
        log.info("[NEXUS_TELEGRAM_SCORE] symbol=%s decision=WAIT sent=false reason=rate_limit", sym)
        return False

    _nexus_score_cache[sym] = (now, fingerprint)
    try:
        result = await notify(await nexus_score_msg(d))
        if not telegram_transport.delivered(result):
            log.info(
                "[NEXUS_TELEGRAM_SCORE] symbol=%s decision=WAIT sent=false class=%s skipped=%s",
                sym, getattr(result, "cls", "NA"), getattr(result, "skipped", None) or "NA",
            )
            return False
        log.info(
            "[NEXUS_TELEGRAM_SCORE] symbol=%s decision=WAIT sent=true score=%s",
            sym,
            "N/A" if display_score is None else f"{display_score:.1f}",
        )
        return True
    except Exception as exc:
        log.warning(
            "[NEXUS_TELEGRAM_SCORE] symbol=%s decision=WAIT sent=false error=%s",
            sym,
            type(exc).__name__,
        )
        return False

async def nexus_veto_msg(d: dict) -> str:
    """Mensagem de VETO — compacta, pois é o caso mais frequente."""
    reason = (d.get("reasoning") or ["sem motivo"])[-1]
    warns  = d.get("warnings") or []
    grade  = d.get("setup_grade", "—")

    extra = ""
    if d.get("risk_reward"):
        extra += f"⚖️ R:R líq: `{d['risk_reward']:.2f}`  "
    if d.get("expected_value"):
        extra += f"📈 EV: `{d['expected_value']:+.3f}%`"
    if extra:
        extra = f"{extra}\n"

    warn_line = ""
    if warns:
        warn_line = f"⚠️ _{warns[0][:70]}_\n"

    return (
        f"🚫 *NEXUS AI — VETO*\n"
        f"📍 `{d.get('symbol')}`  "
        f"{_grade_icon(grade)} `{grade}` "
        f"score `{d.get('setup_quality',0):.1f}`\n"
        f"🌐 `{d.get('market_regime')}`  "
        f"📡 dados `{d.get('data_quality',0):.0f}`\n"
        f"{extra}{warn_line}"
        f"❌ _{reason[:150]}_"
    )


async def notify_nexus(d: dict, approved: bool) -> bool:
    """
    Envia a decisão do NEXUS AI ao Telegram com deduplicação.

    Aprovações passam sempre (são raras e importantes).
    Vetos são deduplicados por (símbolo + motivo) para não inundar o chat.

    Retorna True se a mensagem foi enviada.
    """
    import time as _t
    if os.environ.get("NEXUS_TELEGRAM", "true").lower() != "true":
        return False

    try:
        if approved:
            if not _nexus_rate_ok():
                log.warning("Telegram: rate limit atingido — aprovação não enviada")
                return False
            await notify(await nexus_approved_msg(d))
            return True

        # ── Veto: deduplicação por símbolo + motivo ──────────────
        sym    = d.get("symbol", "?")
        reason = (d.get("reasoning") or ["?"])[-1]
        key    = (sym, reason[:60])
        now    = _t.time()
        last   = _nexus_veto_cache.get(key, 0)

        if now - last < NEXUS_VETO_COOLDOWN:
            return False   # mesmo veto, mesmo motivo, ainda em cooldown

        if not _nexus_rate_ok():
            return False   # guarda de rate limit

        _nexus_veto_cache[key] = now
        # Expurgo do cache para não crescer indefinidamente
        if len(_nexus_veto_cache) > 500:
            for k in [k for k, t in list(_nexus_veto_cache.items())
                      if now - t > NEXUS_VETO_COOLDOWN * 2]:
                _nexus_veto_cache.pop(k, None)

        await notify(await nexus_veto_msg(d))
        return True
    except Exception as e:
        log.debug(f"notify_nexus: {e}")
        return False


async def test_telegram() -> dict:
    """
    Verifica a conexão com o Telegram no startup via getMe.

    Sem isso, a ausência de mensagens tinha várias causas possíveis
    (token errado, chat_id errado, bot bloqueado, rede) e nenhuma
    aparecia nos logs.
    """
    if not cfg.TELEGRAM_TOKEN:
        log.warning("⚠️ Telegram: TELEGRAM_TOKEN ausente — notificações OFF")
        return {"ok": False, "reason": "TELEGRAM_TOKEN ausente"}
    if not cfg.TELEGRAM_CHAT:
        log.warning("⚠️ Telegram: TELEGRAM_CHAT ausente — notificações OFF")
        return {"ok": False, "reason": "TELEGRAM_CHAT ausente"}

    # Same canonical transport as delivery (IPv4, split timeouts, classified).
    # Never logs or returns the token, the URL or the full chat id.
    chat = telegram_transport.mask_chat(cfg.TELEGRAM_CHAT)
    res = await asyncio.to_thread(telegram_transport.attempt_once, "getMe", {}, cfg.TELEGRAM_TOKEN)
    if res.cls == telegram_transport.OK:
        nome = (res.result or {}).get("username", "?")
        log.info(f"✅ Telegram OK — bot @{nome}, chat {chat}")
        return {"ok": True, "bot": nome, "chat": chat}
    if res.cls == telegram_transport.CLIENT_ERROR:
        log.error(f"❌ Telegram: token REJEITADO (HTTP {res.status})")
        return {"ok": False, "reason": f"token inválido (HTTP {res.status})"}
    log.error(f"❌ Telegram: falha ao verificar (class={res.cls} latency_ms={res.latency_ms})")
    return {"ok": False, "reason": res.cls}
