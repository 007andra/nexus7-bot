import os
"""
NEXUS-7 — AI DECISION ENGINE
Implementa as seções 1-24 da especificação.

Pipeline:
    MARKET DATA → DATA VALIDATION → REGIME DETECTION → MTF ANALYSIS
    → ENSEMBLE → FUSION → EV → NO-TRADE CHECKS → DECISION

PRINCÍPIOS INEGOCIÁVEIS:
  • Ficar fora do mercado é decisão válida (seção 1)
  • Dado ausente nunca é inventado (seção 14)
  • A IA autoriza, o Risk Engine decide (seção 9)
  • Qualquer inconsistência crítica → WAIT + BLOCK (seção 22)
"""
import time
from typing import List, Optional, Dict, Any

import numpy as np

from bot.nexus_types import (
    Decision, Regime, SetupGrade, Divergence, BreakoutType,
    ModelOutput, DataQuality, NexusDecision,
)
from bot.nexus_models import run_ensemble
from bot.nexus_probability import heuristic_win_probability
from bot.logger import log
from bot.config import cfg


# ══════════════════════════════════════════════════════════════════
# PESOS DO SCORE (seção 6) — somam 100%
# ══════════════════════════════════════════════════════════════════
WEIGHTS = {
    "TREND_ALIGNMENT": 0.15,
    "MOMENTUM":        0.10,
    "VOLUME":          0.10,
    "MARKET_STRUCTURE":0.15,
    "VOLATILITY":      0.10,
    "DERIVATIVES":     0.10,
    "MICROSTRUCTURE":  0.10,
    "MULTI_TIMEFRAME": 0.10,
    "RISK_REWARD":     0.10,
}

# Threshold configurável (seção 6).
#
# Escala de grades: 90+ = A+ | 85-89 = A | 75-84 = B | 65-74 = C | <65 = NO_TRADE
#
# Default 60: aceita setups abaixo da faixa C. A spec sugere operar
# apenas A+/A (85+), então este valor é uma escolha deliberada de
# priorizar frequência de operação sobre seletividade máxima.
# Ajustável a qualquer momento via NEXUS_MIN_SCORE.
MIN_SCORE = float(os.environ.get("NEXUS_MIN_SCORE", str(cfg.MIN_ENTRY_SCORE)))

# ══════════════════════════════════════════════════════════════════
# IDADE MÁXIMA DOS DADOS (seção 22)
#
# CALIBRAÇÃO CORRIGIDA: o valor era 300s (5 min), mas o bot analisa
# candles de 15 MINUTOS. Um candle recém-fechado tem naturalmente até
# 900s de idade — e era marcado como obsoleto, derrubando a qualidade
# dos dados e fazendo o NEXUS vetar TODOS os sinais.
#
# O valor correto tem que ser proporcional ao timeframe: 2 candles de
# 15M = 1800s. Acima disso, os dados estão de fato defasados.
# ══════════════════════════════════════════════════════════════════
MAX_DATA_AGE_S = float(__import__("os").environ.get("NEXUS_MAX_DATA_AGE", "1800"))


# ══════════════════════════════════════════════════════════════════
# 1. DATA VALIDATION (seções 14 e 22)
# ══════════════════════════════════════════════════════════════════
def validate_data(symbol: str, k15: list, k1h: list, k4h: list,
                  ticker: dict = None, funding: Optional[float] = None,
                  oi: Optional[dict] = None,
                  orderbook: dict = None) -> DataQuality:
    """
    Audita a integridade dos dados ANTES de qualquer análise.

    Nada é preenchido por estimativa. Cada ausência entra em
    'unavailable' e reduz o score. Abaixo de 60 → execução bloqueada.
    """
    dq = DataQuality()

    # ── Candles: quantidade mínima por timeframe ─────────────────
    for name, kl, minimum in (("k15", k15, 60), ("k1h", k1h, 40), ("k4h", k4h, 20)):
        if not kl:
            dq.mark_unavailable(f"{name}:vazio", 30)
        elif len(kl) < minimum:
            dq.mark_unavailable(f"{name}:{len(kl)}<{minimum}", 20)

    # ── Frescor do último candle (seção 22: preço antigo → BLOCK) ─
    if k15:
        try:
            ts = k15[-1].get("ts", 0)
            if ts:
                age = time.time() - (ts / 1000 if ts > 1e11 else ts)
                # Penalidade proporcional ao atraso, não fixa em 40.
                # Um candle 15M recém-fechado tem naturalmente até 900s;
                # penalizar 40 pontos por isso derrubava a qualidade
                # abaixo do mínimo e vetava sinais válidos.
                if age > MAX_DATA_AGE_S:
                    _excesso = age / MAX_DATA_AGE_S
                    _pen = min(40.0, 10.0 * _excesso)
                    dq.mark_stale("k15", age, _pen)
        except Exception as e:
            dq.mark_error(f"timestamp k15 ilegível: {e}", 15)

    # ── Integridade OHLC (high >= low, close dentro do range) ────
    if k15:
        try:
            bad = 0
            for c in k15[-20:]:
                h, l, cl = float(c["h"]), float(c["l"]), float(c["c"])
                if h < l or cl > h or cl < l or h <= 0 or l <= 0:
                    bad += 1
            if bad:
                dq.mark_error(f"{bad} candles com OHLC inválido", 10 * min(bad, 3))
        except Exception as e:
            dq.mark_error(f"OHLC ilegível: {e}", 15)

    # ── Dados OPCIONAIS ──────────────────────────────────────────
    # CALIBRAÇÃO CORRIGIDA: a soma das penalidades de dados opcionais
    # era 25 pontos. Com qualquer falha adicional (ex: candle levemente
    # antigo, -40), a qualidade caía abaixo de 60 e o NEXUS vetava TODOS
    # os sinais — mesmo com os candles perfeitos.
    #
    # Estes dados enriquecem a análise mas NÃO são essenciais: a decisão
    # principal vem dos candles. Penalidades reduzidas e o total de
    # opcionais limitado a 12 pontos.
    _opcionais = 0.0
    if ticker is None or not ticker.get("lastPrice"):
        dq.unavailable.append("ticker");        _opcionais += 4
    if funding is None:
        dq.unavailable.append("funding");       _opcionais += 3
    if oi is None:
        dq.unavailable.append("open_interest"); _opcionais += 3
    if orderbook is None or not orderbook.get("b"):
        dq.unavailable.append("orderbook");     _opcionais += 2
    dq.score = max(0.0, dq.score - min(12.0, _opcionais))

    return dq


# ══════════════════════════════════════════════════════════════════
# 2. MARKET REGIME DETECTION (seção 4)
# ══════════════════════════════════════════════════════════════════
def detect_regime(closes: list, highs: list, lows: list,
                  volumes: list) -> tuple:
    """
    Classifica o regime e retorna (Regime, detalhes).
    A estratégia priorizada muda conforme o regime (seção 4).
    """
    from bot.indicators import adx as adx_fn, atr as atr_fn, choppiness, bollinger

    if len(closes) < 30:
        return Regime.UNKNOWN, {}

    try:
        a       = adx_fn(highs, lows, closes)
        adx_v   = float(a.get("adx", 0))
        adx_dir = a.get("direction", "LONG")

        atr_arr = atr_fn(highs, lows, closes)
        atr_now = float(atr_arr[-1])
        atr_avg = float(np.mean(atr_arr[-20:])) if len(atr_arr) >= 20 else atr_now
        atr_pct = atr_now / closes[-1] * 100 if closes[-1] else 0.0
        expansion = (atr_now / atr_avg) if atr_avg > 0 else 1.0

        ch = choppiness(highs, lows, closes)
        ci = float(ch.get("ci", 50))
        bb = bollinger(closes)

        vol_now = float(volumes[-1]) if volumes else 0.0
        vol_avg = float(np.mean(volumes[-21:-1])) if len(volumes) > 21 else vol_now
        vol_mult = (vol_now / vol_avg) if vol_avg > 0 else 1.0

        px        = float(closes[-1])
        prev_high = float(max(highs[-21:-1])) if len(highs) > 21 else px
        prev_low  = float(min(lows[-21:-1]))  if len(lows) > 21 else px

        details = {"adx": adx_v, "ci": ci, "atr_pct": round(atr_pct, 3),
                   "expansion": round(expansion, 2), "vol_mult": round(vol_mult, 2),
                   "bb_squeeze": bb.get("squeezed")}

        # Ordem importa: do mais específico ao mais genérico
        if expansion > 2.5 and atr_pct > 4.0:
            return Regime.EXTREME_EVENT, details
        if px > prev_high and vol_mult > 1.5:
            return Regime.BREAKOUT, details
        if px < prev_low and vol_mult > 1.5:
            return Regime.BREAKDOWN, details
        if ci > 61.8 and adx_v < 20:
            return Regime.CHOPPY, details
        if adx_v > 25 and adx_dir == "LONG":
            return Regime.TRENDING_BULL, details
        if adx_v > 25 and adx_dir == "SHORT":
            return Regime.TRENDING_BEAR, details
        if atr_pct > 3.0:
            return Regime.HIGH_VOLATILITY, details
        if bb.get("squeezed") and atr_pct < 0.8:
            return Regime.LOW_VOLATILITY, details
        if adx_v < 20 and vol_mult > 1.2:
            return Regime.ACCUMULATION, details
        if adx_v < 20:
            return Regime.RANGE, details
        return Regime.RANGE, details
    except Exception as e:
        log.debug(f"detect_regime: {e}")
        return Regime.UNKNOWN, {}


# Compatibilidade de cada modelo com cada regime (seção 4).
# Peso 0.0 = modelo irrelevante naquele regime e é ignorado.
REGIME_MODEL_WEIGHTS = {
    Regime.TRENDING_BULL:   {"TREND":1.3,"MOMENTUM":1.2,"MEAN_REVERSION":0.2,"BREAKOUT":1.0,"STRUCTURE":1.2,"DERIVATIVES":0.9},
    Regime.TRENDING_BEAR:   {"TREND":1.3,"MOMENTUM":1.2,"MEAN_REVERSION":0.2,"BREAKOUT":1.0,"STRUCTURE":1.2,"DERIVATIVES":0.9},
    Regime.RANGE:           {"TREND":0.4,"MOMENTUM":0.7,"MEAN_REVERSION":1.4,"BREAKOUT":0.5,"STRUCTURE":1.0,"DERIVATIVES":1.0},
    Regime.BREAKOUT:        {"TREND":1.1,"MOMENTUM":1.1,"MEAN_REVERSION":0.1,"BREAKOUT":1.5,"STRUCTURE":1.2,"DERIVATIVES":1.1},
    Regime.BREAKDOWN:       {"TREND":1.1,"MOMENTUM":1.1,"MEAN_REVERSION":0.1,"BREAKOUT":1.5,"STRUCTURE":1.2,"DERIVATIVES":1.1},
    Regime.HIGH_VOLATILITY: {"TREND":0.8,"MOMENTUM":0.7,"MEAN_REVERSION":0.6,"BREAKOUT":0.8,"STRUCTURE":0.9,"DERIVATIVES":1.0},
    Regime.LOW_VOLATILITY:  {"TREND":0.6,"MOMENTUM":0.6,"MEAN_REVERSION":1.1,"BREAKOUT":0.7,"STRUCTURE":0.9,"DERIVATIVES":0.9},
    Regime.ACCUMULATION:    {"TREND":0.7,"MOMENTUM":0.8,"MEAN_REVERSION":1.1,"BREAKOUT":1.0,"STRUCTURE":1.1,"DERIVATIVES":1.0},
    Regime.DISTRIBUTION:    {"TREND":0.7,"MOMENTUM":0.8,"MEAN_REVERSION":1.1,"BREAKOUT":1.0,"STRUCTURE":1.1,"DERIVATIVES":1.0},
    Regime.CHOPPY:          {"TREND":0.2,"MOMENTUM":0.3,"MEAN_REVERSION":0.5,"BREAKOUT":0.2,"STRUCTURE":0.5,"DERIVATIVES":0.6},
    Regime.EXTREME_EVENT:   {"TREND":0.1,"MOMENTUM":0.1,"MEAN_REVERSION":0.1,"BREAKOUT":0.1,"STRUCTURE":0.2,"DERIVATIVES":0.3},
    Regime.UNKNOWN:         {"TREND":0.5,"MOMENTUM":0.5,"MEAN_REVERSION":0.5,"BREAKOUT":0.5,"STRUCTURE":0.5,"DERIVATIVES":0.5},
}


def regime_compatibility(regime: Regime, direction: Decision) -> float:
    """0-100: quão compatível é a direção proposta com o regime atual."""
    if regime in (Regime.TRENDING_BULL, Regime.BREAKOUT):
        return 95.0 if direction == Decision.LONG else 25.0
    if regime in (Regime.TRENDING_BEAR, Regime.BREAKDOWN):
        return 95.0 if direction == Decision.SHORT else 25.0
    if regime in (Regime.RANGE, Regime.ACCUMULATION, Regime.DISTRIBUTION):
        return 60.0
    if regime == Regime.HIGH_VOLATILITY: return 40.0
    if regime == Regime.LOW_VOLATILITY:  return 45.0
    if regime == Regime.CHOPPY:          return 15.0
    if regime == Regime.EXTREME_EVENT:   return 0.0
    return 35.0


# ══════════════════════════════════════════════════════════════════
# 3. MULTI-TIMEFRAME (seção 3)
# ══════════════════════════════════════════════════════════════════
def analyze_mtf(k15: list, k1h: list, k4h: list) -> dict:
    """
    Alinhamento entre timeframes. Conflito forte → reduz confiança
    ou retorna WAIT (seção 3).
    """
    from bot.indicators import ema as ema_fn

    def tf_bias(kl, min_len=25):
        if not kl or len(kl) < min_len:
            return None      # DATA_UNAVAILABLE — não inventa neutro
        c = [float(k["c"]) for k in kl]
        try:
            e20, e50 = float(ema_fn(c, 20)[-1]), float(ema_fn(c, 50)[-1])
            px = c[-1]
            if px > e20 > e50: return Decision.LONG
            if px < e20 < e50: return Decision.SHORT
            return Decision.WAIT
        except Exception:
            return None

    b4h, b1h, b15 = tf_bias(k4h, 25), tf_bias(k1h, 25), tf_bias(k15, 25)
    biases = [b for b in (b4h, b1h, b15) if b is not None]

    if not biases:
        return {"aligned": False, "direction": Decision.WAIT, "score": 0.0,
                "conflict": True, "available": False,
                "detail": "DATA_UNAVAILABLE: nenhum timeframe utilizável"}

    longs  = sum(1 for b in biases if b == Decision.LONG)
    shorts = sum(1 for b in biases if b == Decision.SHORT)
    n      = len(biases)

    if longs == n:
        direction, score, conflict = Decision.LONG, 100.0, False
    elif shorts == n:
        direction, score, conflict = Decision.SHORT, 100.0, False
    elif longs > shorts and shorts == 0:
        direction, score, conflict = Decision.LONG, 70.0, False
    elif shorts > longs and longs == 0:
        direction, score, conflict = Decision.SHORT, 70.0, False
    elif longs > 0 and shorts > 0:
        # Conflito real entre timeframes → seção 3 manda esperar
        direction, score, conflict = Decision.WAIT, 0.0, True
    else:
        direction, score, conflict = Decision.WAIT, 30.0, False

    return {"aligned": score >= 70, "direction": direction, "score": score,
            "conflict": conflict, "available": True,
            "detail": f"4H={b4h.value if b4h else 'N/A'} "
                      f"1H={b1h.value if b1h else 'N/A'} "
                      f"15M={b15.value if b15 else 'N/A'}"}


# ══════════════════════════════════════════════════════════════════
# 4. EXPECTED VALUE (seção 5)
# ══════════════════════════════════════════════════════════════════
def expected_value(win_prob: float, entry: float, sl: float, tp: float,
                   taker_fee: float = 0.0006,
                   slippage: float = 0.0005) -> dict:
    """
    EV = (P_ganho × ganho_líquido) − (P_perda × perda_líquida)

    Custos incluídos nas DUAS pontas: taxa de entrada, taxa de saída e
    slippage. Um R:R bruto de 2.0 pode virar EV negativo depois deles.
    """
    if entry <= 0 or sl <= 0 or tp <= 0:
        return {"ev": 0.0, "ev_pct": 0.0, "valid": False,
                "reason": "preços inválidos"}

    gain_gross = abs(tp - entry) / entry
    loss_gross = abs(entry - sl) / entry
    if loss_gross <= 0:
        return {"ev": 0.0, "ev_pct": 0.0, "valid": False,
                "reason": "distância de stop nula"}

    cost = (taker_fee * 2) + (slippage * 2)
    gain_net = gain_gross - cost
    loss_net = loss_gross + cost

    p = max(0.0, min(1.0, win_prob))
    ev = (p * gain_net) - ((1 - p) * loss_net)
    rr_net = (gain_net / loss_net) if loss_net > 0 else 0.0

    return {
        "ev":          round(ev, 6),
        "ev_pct":      round(ev * 100, 4),
        "gain_net":    round(gain_net, 6),
        "loss_net":    round(loss_net, 6),
        "rr_net":      round(rr_net, 3),
        "cost":        round(cost, 6),
        "win_prob":    round(p, 4),
        "valid":       ev > 0,
        "reason":      "EV positivo" if ev > 0 else "EV negativo após custos",
    }


# ══════════════════════════════════════════════════════════════════
# 5. FUSÃO E DECISÃO (seções 6, 17, 39)
# ══════════════════════════════════════════════════════════════════
def _fuse(models: List[ModelOutput], regime: Regime) -> dict:
    """
    Combina os modelos ponderando por compatibilidade com o regime.

    Modelos com available=False são EXCLUÍDOS (seção 14) — não entram
    como neutro. Consenso forte → HIGH_CONVICTION; conflito → WAIT.
    """
    rw = REGIME_MODEL_WEIGHTS.get(regime, REGIME_MODEL_WEIGHTS[Regime.UNKNOWN])

    long_w = short_w = total_w = 0.0
    risk_scores = []
    used, skipped = [], []

    for m in models:
        if not m.available:
            skipped.append(m.name)
            continue
        if m.name == "RISK_REGIME":
            risk_scores.append(m.risk_score)
            continue

        w = rw.get(m.name, 1.0)
        if w <= 0:
            skipped.append(f"{m.name}(peso 0 em {regime.value})")
            continue

        contrib = m.confidence * w
        total_w += 100 * w
        if m.direction == Decision.LONG:
            long_w += contrib
        elif m.direction == Decision.SHORT:
            short_w += contrib
        risk_scores.append(m.risk_score)
        used.append(m.name)

    if total_w <= 0:
        return {"direction": Decision.WAIT, "confidence": 0.0,
                "conviction": "NONE", "risk": 100.0,
                "used": used, "skipped": skipped,
                "reason": "nenhum modelo utilizável"}

    long_pct  = long_w  / total_w * 100
    short_pct = short_w / total_w * 100
    avg_risk  = float(np.mean(risk_scores)) if risk_scores else 50.0

    if long_pct > short_pct:
        direction, confidence = Decision.LONG,  long_pct
    elif short_pct > long_pct:
        direction, confidence = Decision.SHORT, short_pct
    else:
        direction, confidence = Decision.WAIT, 0.0

    # Conflito: os dois lados com força relevante → baixa convicção
    minor = min(long_pct, short_pct)
    major = max(long_pct, short_pct)
    if minor > 0 and major > 0 and minor / major > 0.5:
        conviction = "LOW"
        confidence *= 0.5
    elif confidence >= 60:
        conviction = "HIGH"
    elif confidence >= 35:
        conviction = "MEDIUM"
    else:
        conviction = "LOW"

    return {"direction": direction, "confidence": round(confidence, 2),
            "conviction": conviction, "risk": round(avg_risk, 2),
            "long_pct": round(long_pct, 2), "short_pct": round(short_pct, 2),
            "used": used, "skipped": skipped, "reason": ""}


def _score_components(models: List[ModelOutput], mtf: dict,
                       rr_net: float, direction: Decision,
                       regime: Regime) -> dict:
    """Calcula os 9 componentes ponderados do score (seção 6)."""
    by = {m.name: m for m in models}

    def dir_conf(name: str) -> float:
        m = by.get(name)
        if not m or not m.available:
            return 0.0
        if m.direction == direction:
            return m.confidence
        if m.direction == Decision.WAIT:
            return m.confidence * 0.3
        return 0.0    # modelo apontando na direção oposta não pontua

    trend_s  = dir_conf("TREND")
    mom_s    = dir_conf("MOMENTUM")
    struct_s = dir_conf("STRUCTURE")
    deriv_s  = dir_conf("DERIVATIVES")

    bo = by.get("BREAKOUT")
    vol_s = bo.details.get("vol_mult", 1.0) * 40 if (bo and bo.available) else 0.0
    vol_s = min(100.0, vol_s)

    rr_model = by.get("RISK_REGIME")
    volat_s  = (100 - rr_model.risk_score) if (rr_model and rr_model.available) else 0.0

    mtf_s = mtf.get("score", 0.0)

    # R:R líquido: 2.0 → 100 pontos
    rr_s = min(100.0, max(0.0, rr_net / 2.0 * 100))

    # Microestrutura ainda não alimentada — seção 14: reporta 0, não inventa
    micro_s = 0.0

    comps = {
        "TREND_ALIGNMENT":  trend_s,
        "MOMENTUM":         mom_s,
        "VOLUME":           vol_s,
        "MARKET_STRUCTURE": struct_s,
        "VOLATILITY":       volat_s,
        "DERIVATIVES":      deriv_s,
        "MICROSTRUCTURE":   micro_s,
        "MULTI_TIMEFRAME":  mtf_s,
        "RISK_REWARD":      rr_s,
    }

    # ══════════════════════════════════════════════════════════════
    # BUG DE DESIGN CORRIGIDO — COMPONENTE SEM DADOS PUXAVA O SCORE
    #
    # DERIVATIVES (funding/OI) e MICROSTRUCTURE (order book) entravam
    # como ZERO quando os dados não estavam disponíveis. Juntos valem
    # 20% do score — o teto real virava 80, não 100, e nenhum setup
    # alcançava o threshold.
    #
    # Isso contraria a seção 14 da spec: dado ausente não deve ser
    # tratado como sinal negativo. O correto é EXCLUIR o componente e
    # renormalizar os pesos dos que restaram, exatamente como já é
    # feito com os modelos do ensemble.
    #
    # Componentes indisponíveis são listados para auditoria.
    # ══════════════════════════════════════════════════════════════
    _sem_dados = set()
    if not (by.get("DERIVATIVES") and by["DERIVATIVES"].available):
        _sem_dados.add("DERIVATIVES")
    if micro_s <= 0:
        _sem_dados.add("MICROSTRUCTURE")   # não alimentado nesta versão

    _ativos = {k: v for k, v in comps.items() if k not in _sem_dados}
    _peso_total = sum(WEIGHTS[k] for k in _ativos) or 1.0

    # Renormaliza: os pesos dos componentes ativos somam 1.0
    total = sum(_ativos[k] * (WEIGHTS[k] / _peso_total) for k in _ativos)

    return {
        "components":  {k: round(v, 1) for k, v in comps.items()},
        "unavailable": sorted(_sem_dados),
        "weight_used": round(_peso_total, 3),
        "total":       round(total, 2),
    }


# ══════════════════════════════════════════════════════════════════
# 6. MOTOR PRINCIPAL
# ══════════════════════════════════════════════════════════════════
def decide(symbol: str, k15: list, k1h: list, k4h: list,
           entry: float = 0.0, sl: float = 0.0, tp: float = 0.0,
           ticker: dict = None, funding: Optional[float] = None,
           oi: Optional[dict] = None, oi_delta: Optional[float] = None,
           orderbook: dict = None, ls_ratio: Optional[float] = None,
           news_score: Optional[float] = None,
           min_score: float = None) -> NexusDecision:
    """
    Decisão completa do NEXUS AI.

    Retorna SEMPRE um NexusDecision. execution_allowed=True é apenas a
    autorização da IA — o Risk Engine ainda decide (seção 9).

    Qualquer condição de no-trade (seção 12) retorna WAIT com o motivo
    explícito em reasoning.
    """
    threshold = min_score if min_score is not None else MIN_SCORE
    warnings: List[str] = []
    reasoning: List[str] = []

    # ── PASSO 1: validação de dados (seções 14, 22) ──────────────
    dq = validate_data(symbol, k15, k1h, k4h, ticker, funding, oi, orderbook)
    if dq.unavailable:
        warnings.append(f"DATA_UNAVAILABLE: {', '.join(dq.unavailable)}")
    if dq.stale:
        warnings.append(f"DADOS ANTIGOS: {', '.join(dq.stale)}")
    if dq.errors:
        warnings.extend(dq.errors)

    if not dq.is_acceptable:
        return NexusDecision.wait(
            symbol, f"Qualidade de dados {dq.score:.0f}/100 abaixo do mínimo (60)",
            dq.score, warnings)

    if not k15 or len(k15) < 60:
        return NexusDecision.wait(symbol, "Candles 15M insuficientes", dq.score, warnings)

    # ── PASSO 2: extrai séries ───────────────────────────────────
    try:
        closes  = [float(k["c"]) for k in k15]
        highs   = [float(k["h"]) for k in k15]
        lows    = [float(k["l"]) for k in k15]
        volumes = [float(k.get("v", 0)) for k in k15]
    except Exception as e:
        return NexusDecision.wait(symbol, f"Candles ilegíveis: {e}", 0.0, warnings)

    # ── PASSO 3: regime (seção 4) ────────────────────────────────
    regime, regime_details = detect_regime(closes, highs, lows, volumes)
    reasoning.append(f"Regime: {regime.value}")

    if regime == Regime.EXTREME_EVENT:
        return NexusDecision.wait(
            symbol, "EXTREME_EVENT: volatilidade anormal — operação bloqueada",
            dq.score, warnings + ["Aguardando normalização do mercado"])

    if regime == Regime.CHOPPY:
        # Em mercado errático o threshold sobe 15 pontos sobre a base.
        # Antes era fixo em 92, o que com base 60 significaria nunca
        # operar em CHOPPY. Agora é proporcional ao threshold escolhido.
        threshold = threshold + 15.0
        warnings.append(f"CHOPPY: threshold elevado para {threshold:.0f}")

    # ── PASSO 4: multi-timeframe (seção 3) ───────────────────────
    mtf = analyze_mtf(k15, k1h, k4h)
    reasoning.append(f"MTF: {mtf['detail']}")
    if mtf.get("conflict"):
        return NexusDecision.wait(
            symbol, f"Conflito entre timeframes ({mtf['detail']}) — seção 3",
            dq.score, warnings)

    # ── PASSO 5: ensemble (seção 17) ─────────────────────────────
    models = run_ensemble(closes, highs, lows, volumes,
                          funding=funding, oi_delta=oi_delta, ls_ratio=ls_ratio)
    fusion = _fuse(models, regime)
    reasoning.append(
        f"Ensemble: {fusion['direction'].value} conf={fusion['confidence']:.1f} "
        f"({fusion['conviction']}) | modelos={len(fusion['used'])}"
    )
    if fusion["skipped"]:
        warnings.append(f"Modelos ignorados: {', '.join(fusion['skipped'])}")

    # Snapshot dos modelos — anexado a TODOS os retornos daqui em diante,
    # para que a observabilidade (seção 23) não se perca quando a decisão
    # é WAIT. Saber POR QUE não operou é tão importante quanto o trade.
    _models_snapshot = [
        {"name": m.name, "direction": m.direction.value,
         "confidence": round(m.confidence, 1), "risk": round(m.risk_score, 1),
         "available": m.available, "reason": m.reason}
        for m in models
    ]

    def _wait(reason: str) -> NexusDecision:
        d = NexusDecision.wait(symbol, reason, dq.score, warnings)
        d.models        = _models_snapshot
        d.market_regime = regime.value
        d.reasoning     = reasoning + [reason]
        return d

    direction = fusion["direction"]
    if direction == Decision.WAIT:
        return _wait(f"Ensemble sem direção definida "
                     f"({fusion['reason'] or 'sinais conflitantes'})")

    # Ensemble deve concordar com o MTF
    if mtf["available"] and mtf["direction"] != Decision.WAIT and mtf["direction"] != direction:
        return _wait(f"Ensemble ({direction.value}) diverge do MTF ({mtf['direction'].value})")

    # ── PASSO 6: compatibilidade com o regime ────────────────────
    compat = regime_compatibility(regime, direction)
    if compat < 30:
        return _wait(f"{direction.value} incompatível com regime "
                     f"{regime.value} (compat={compat:.0f})")

    # ── PASSO 7: R:R e EV (seção 5) ──────────────────────────────
    if entry <= 0 or sl <= 0 or tp <= 0:
        return _wait("Níveis de entrada/SL/TP não fornecidos")

    # Stop tecnicamente válido? (seção 12)
    if direction == Decision.LONG and not (sl < entry < tp):
        return _wait(f"Stop inválido para LONG: sl={sl} entry={entry} tp={tp}")
    if direction == Decision.SHORT and not (tp < entry < sl):
        return _wait(f"Stop inválido para SHORT: tp={tp} entry={entry} sl={sl}")

    # Heurística de EV versionada; ainda não é uma probabilidade calibrada.
    win_prob = heuristic_win_probability(fusion["confidence"])
    ev = expected_value(win_prob, entry, sl, tp)

    if not ev["valid"]:
        return _wait(f"EV negativo após custos: {ev['ev_pct']:.3f}% "
                     f"(R:R líquido {ev['rr_net']:.2f})")

    # ══════════════════════════════════════════════════════════════
    # INCONSISTÊNCIA DE DESIGN CORRIGIDA
    #
    # A estratégia aprova com R:R BRUTO >= MIN_RR_RATIO (2.0), mas o
    # NEXUS exigia R:R LÍQUIDO >= o MESMO valor. Como taxas e slippage
    # corroem 15-25% do R:R, TODO sinal aprovado pela estratégia era
    # vetado aqui — nenhuma ordem passava.
    #
    # Comparar bruto com líquido usando o mesmo limiar é um erro de
    # unidade. O R:R líquido tem seu próprio piso, derivado do bruto
    # descontando o custo típico (configurável).
    #
    # O que realmente protege é o EV positivo, já validado acima.
    # ══════════════════════════════════════════════════════════════
    _rr_liq_min = float(os.environ.get(
        "NEXUS_MIN_RR_NET",
        str(round(cfg.MIN_RR_RATIO * 0.80, 2))     # 2.0 → 1.60
    ))
    if ev["rr_net"] < _rr_liq_min:
        return _wait(
            f"R:R líquido {ev['rr_net']:.2f} < mínimo líquido {_rr_liq_min:.2f} "
            f"(bruto exigido: {cfg.MIN_RR_RATIO})"
        )

    # ── PASSO 8: score final (seção 6) ───────────────────────────
    sc     = _score_components(models, mtf, ev["rr_net"], direction, regime)
    score  = sc["total"]

    # Penalidade de risco (seção 39: score − risk penalties)
    risk_penalty = fusion["risk"] * 0.20
    score = max(0.0, score - risk_penalty)

    # ── PASSO 9: notícias (seções 34, 35, 39) ────────────────────
    if news_score is not None:
        if direction == Decision.LONG and news_score <= -50:
            return _wait(f"Sinal LONG mas notícias fortemente bearish "
                         f"({news_score:+.0f}) — seção 34")
        if direction == Decision.SHORT and news_score >= 50:
            return _wait(f"Sinal SHORT mas notícias fortemente bullish "
                         f"({news_score:+.0f}) — seção 34")
        aligned = (direction == Decision.LONG and news_score > 0) or \
                  (direction == Decision.SHORT and news_score < 0)
        adj = min(5.0, abs(news_score) / 20)
        score += adj if aligned else -adj
        reasoning.append(f"News score {news_score:+.0f} ({'alinhado' if aligned else 'contrário'})")

    # Qualidade de dados degradada reduz o score proporcionalmente
    score *= (dq.score / 100.0)
    score  = round(max(0.0, min(100.0, score)), 2)
    grade  = SetupGrade.from_score(score)

    reasoning.append(f"Score {score:.1f} ({grade.value}) | threshold {threshold:.0f}")
    reasoning.append(f"EV {ev['ev_pct']:+.3f}% | R:R líquido {ev['rr_net']:.2f}")

    # ── PASSO 10: decisão final ──────────────────────────────────
    allowed = score >= threshold
    if not allowed:
        reasoning.append(f"REJEITADO: score {score:.1f} < {threshold:.0f}")

    bo = next((m for m in models if m.name == "BREAKOUT"), None)
    mo = next((m for m in models if m.name == "MOMENTUM"), None)

    return NexusDecision(
        symbol            = symbol,
        decision          = direction.value if allowed else Decision.WAIT.value,
        confidence        = round(fusion["confidence"], 2),
        setup_quality     = score,
        market_regime     = regime.value,
        regime_compat     = round(compat, 1),
        entry             = entry,
        stop_loss         = sl,
        take_profit       = tp,
        risk_reward       = ev["rr_net"],
        expected_value    = ev["ev_pct"],
        setup_grade       = grade.value,
        reasoning         = reasoning,
        warnings          = warnings,
        data_quality      = round(dq.score, 1),
        execution_allowed = allowed,
        news_sentiment    = news_score,
        breakout_type     = (bo.details.get("breakout_type", BreakoutType.NONE.value)
                             if bo and bo.available else BreakoutType.NONE.value),
        divergence        = (mo.details.get("divergence", Divergence.NONE.value)
                             if mo and mo.available else Divergence.NONE.value),
        models            = [{"name": m.name, "direction": m.direction.value,
                              "confidence": round(m.confidence, 1),
                              "risk": round(m.risk_score, 1),
                              "available": m.available, "reason": m.reason}
                             for m in models],
    )


# ══════════════════════════════════════════════════════════════════
# 7. POSITION MONITOR (seção 11)
# ══════════════════════════════════════════════════════════════════
def monitor_position(symbol: str, direction: str, entry: float, sl: float,
                     tp: float, current: float, k15: list,
                     funding: Optional[float] = None) -> dict:
    """
    Reavalia uma posição aberta e recomenda ação.

    NUNCA recomenda mover o stop de forma a AUMENTAR o risco original
    (seção 11) — apenas na direção que reduz exposição.
    """
    from bot.nexus_types import PositionAction

    out = {"action": PositionAction.HOLD.value, "reason": "", "urgency": "LOW"}
    if not k15 or len(k15) < 30 or entry <= 0:
        out["reason"] = "DATA_UNAVAILABLE: dados insuficientes para reavaliar"
        return out

    try:
        closes  = [float(k["c"]) for k in k15]
        highs   = [float(k["h"]) for k in k15]
        lows    = [float(k["l"]) for k in k15]
        volumes = [float(k.get("v", 0)) for k in k15]

        is_long = direction.upper() in ("LONG", "BUY")
        risk    = abs(entry - sl)
        if risk <= 0:
            out["reason"] = "distância de stop inválida"
            return out

        progress = ((current - entry) if is_long else (entry - current)) / risk

        regime, _ = detect_regime(closes, highs, lows, volumes)
        models    = run_ensemble(closes, highs, lows, volumes, funding=funding)
        fusion    = _fuse(models, regime)

        opposite = (Decision.SHORT if is_long else Decision.LONG)

        # Regime extremo → sair
        if regime == Regime.EXTREME_EVENT:
            return {"action": PositionAction.EXIT.value,
                    "reason": "EXTREME_EVENT durante posição aberta",
                    "urgency": "HIGH"}

        # Ensemble virou contra com convicção alta
        if fusion["direction"] == opposite and fusion["confidence"] > 60:
            if progress > 0.5:
                return {"action": PositionAction.TAKE_PARTIAL.value,
                        "reason": f"Ensemble inverteu ({fusion['confidence']:.0f}%) com +{progress:.1f}R",
                        "urgency": "MEDIUM"}
            return {"action": PositionAction.EXIT.value,
                    "reason": f"Ensemble inverteu com convicção {fusion['confidence']:.0f}%",
                    "urgency": "HIGH"}

        # Progresso bom → proteger
        if progress >= 2.0:
            return {"action": PositionAction.TRAIL_STOP.value,
                    "reason": f"+{progress:.1f}R — ativar trailing",
                    "urgency": "LOW"}
        if progress >= 1.0:
            return {"action": PositionAction.MOVE_STOP.value,
                    "reason": f"+{progress:.1f}R — mover stop para break-even",
                    "urgency": "LOW", "suggested_sl": entry}

        # Funding corroendo posição longa
        if funding is not None and is_long and funding > 0.001:
            return {"action": PositionAction.REDUCE.value,
                    "reason": f"Funding {funding*100:.3f}% muito alto para LONG",
                    "urgency": "MEDIUM"}

        out["reason"] = f"Posição saudável ({progress:+.2f}R), regime {regime.value}"
        return out
    except Exception as e:
        log.error(f"monitor_position {symbol}: {e}")
        return {"action": PositionAction.HOLD.value,
                "reason": f"erro na reavaliação: {e}", "urgency": "LOW"}
