"""
NEXUS-7 — Ensemble de modelos independentes (seção 17)

Sete modelos analisam o mesmo mercado por óticas diferentes:

    MODEL A — Trend            (EMAs, ADX, VWAP)
    MODEL B — Momentum         (RSI, MACD, ROC)
    MODEL C — Mean Reversion   (Bollinger, RSI extremos)
    MODEL D — Breakout         (rompimento + confirmação, seção 7)
    MODEL E — Market Structure  (HH/HL, BOS, CHoCH)
    MODEL F — Derivatives       (funding, OI, long/short)
    MODEL G — Risk/Regime       (volatilidade, choppiness)

Cada um retorna ModelOutput independente. Nenhum enxerga o resultado
do outro — isso é essencial: modelos correlacionados dariam falsa
sensação de consenso.

Seção 14: modelo sem dados retorna available=False e é EXCLUÍDO da
fusão, em vez de contribuir com valor neutro inventado.
"""
import numpy as np
from typing import List, Optional

from bot.nexus_types import ModelOutput, Decision, Divergence, BreakoutType
from bot.indicators import (
    ema, rsi, macd, atr, adx, bollinger, choppiness, vwap, smc_analysis
)
from bot.logger import log


# ══════════════════════════════════════════════════════════════════
# MODEL A — TREND
# ══════════════════════════════════════════════════════════════════
def model_trend(closes: List[float], highs: List[float], lows: List[float],
                volumes: List[float]) -> ModelOutput:
    """
    Alinhamento de EMAs (9/20/50/200) + força de tendência via ADX.

    Confiança é proporcional ao número de EMAs alinhadas E à força do
    ADX. Alinhamento sem ADX (< 20) é tendência fraca — confiança baixa.
    """
    m = ModelOutput(name="TREND")
    if len(closes) < 60:
        m.available = False
        m.reason = "dados insuficientes (<60 candles)"
        return m

    try:
        e9   = float(ema(closes, 9)[-1])
        e20  = float(ema(closes, 20)[-1])
        e50  = float(ema(closes, 50)[-1])
        e200 = float(ema(closes, 200)[-1]) if len(closes) >= 200 else None
        px   = float(closes[-1])

        a      = adx(highs, lows, closes)
        adx_v  = float(a.get("adx", 0))

        bull = [px > e9, e9 > e20, e20 > e50]
        bear = [px < e9, e9 < e20, e20 < e50]
        if e200 is not None:
            bull.append(e50 > e200)
            bear.append(e50 < e200)

        n_bull, n_bear, n_tot = sum(bull), sum(bear), len(bull)

        # Confiança = alinhamento (0-70) escalado pela força do ADX (0-100%)
        adx_factor = min(1.0, adx_v / 30.0)
        if n_bull == n_tot:
            m.direction  = Decision.LONG
            m.confidence = 70 * adx_factor + 15
        elif n_bear == n_tot:
            m.direction  = Decision.SHORT
            m.confidence = 70 * adx_factor + 15
        elif n_bull >= n_tot - 1:
            m.direction  = Decision.LONG
            m.confidence = 45 * adx_factor
        elif n_bear >= n_tot - 1:
            m.direction  = Decision.SHORT
            m.confidence = 45 * adx_factor
        else:
            m.direction  = Decision.WAIT
            m.confidence = 0.0

        # ADX < 20 = ausência de tendência → penaliza fortemente
        if adx_v < 20:
            m.confidence *= 0.4
            m.risk_score = 60.0
        else:
            m.risk_score = max(0.0, 40.0 - adx_v)

        m.reason  = f"EMAs {n_bull}/{n_tot} bull, ADX={adx_v:.1f}"
        m.details = {"adx": adx_v, "ema9": e9, "ema20": e20, "ema50": e50,
                     "ema200": e200, "bull_aligned": n_bull, "total": n_tot}
    except Exception as e:
        m.available = False
        m.reason    = f"erro: {e}"
        log.debug(f"model_trend: {e}")
    return m


# ══════════════════════════════════════════════════════════════════
# MODEL B — MOMENTUM
# ══════════════════════════════════════════════════════════════════
def model_momentum(closes: List[float]) -> ModelOutput:
    """RSI + MACD + ROC. Detecta também divergências (seção 8)."""
    m = ModelOutput(name="MOMENTUM")
    if len(closes) < 40:
        m.available = False
        m.reason = "dados insuficientes (<40 candles)"
        return m

    try:
        r_arr = rsi(closes, 14)
        r     = float(r_arr[-1])
        macd_line, sig_line, hist = macd(closes)
        h     = float(hist[-1])
        h_prev= float(hist[-2]) if len(hist) > 1 else h
        roc   = (closes[-1] / closes[-10] - 1) * 100 if len(closes) >= 10 else 0.0

        score = 0.0
        # RSI: zona de força sem estar esticado
        if 50 < r < 70:   score += 30
        elif 30 < r < 50: score -= 30
        elif r >= 70:     score += 10   # sobrecomprado: momentum alto mas risco
        elif r <= 30:     score -= 10

        # MACD: histograma e sua aceleração
        if h > 0:          score += 25
        else:              score -= 25
        if h > h_prev:     score += 15
        else:              score -= 15

        # ROC
        if roc > 0.5:      score += 20
        elif roc < -0.5:   score -= 20

        if score > 30:
            m.direction, m.confidence = Decision.LONG,  min(100, abs(score))
        elif score < -30:
            m.direction, m.confidence = Decision.SHORT, min(100, abs(score))
        else:
            m.direction, m.confidence = Decision.WAIT, 0.0

        # Momentum esticado = risco de reversão
        m.risk_score = 70.0 if (r > 78 or r < 22) else 25.0
        m.reason  = f"RSI={r:.1f} MACD_h={h:+.5f} ROC={roc:+.2f}%"
        m.details = {"rsi": r, "macd_hist": h, "roc": roc,
                     "divergence": _detect_divergence(closes, r_arr).value}
    except Exception as e:
        m.available = False
        m.reason    = f"erro: {e}"
        log.debug(f"model_momentum: {e}")
    return m


def _detect_divergence(closes: List[float], rsi_arr) -> Divergence:
    """
    Divergência preço × RSI (seção 8).
    Nunca é gatilho isolado — apenas ajusta o confidence.
    """
    try:
        if len(closes) < 30 or len(rsi_arr) < 30:
            return Divergence.NONE
        c = np.array(closes[-30:], dtype=float)
        r = np.array(rsi_arr[-30:], dtype=float)
        mid = 15
        p1, p2 = float(c[:mid].max()), float(c[mid:].max())
        r1, r2 = float(r[:mid].max()), float(r[mid:].max())
        l1, l2 = float(c[:mid].min()), float(c[mid:].min())
        rl1, rl2 = float(r[:mid].min()), float(r[mid:].min())

        if p2 > p1 and r2 < r1:   return Divergence.BEARISH        # HH preço, LH RSI
        if l2 < l1 and rl2 > rl1: return Divergence.BULLISH        # LL preço, HL RSI
        if l2 > l1 and rl2 < rl1: return Divergence.HIDDEN_BULLISH
        if p2 < p1 and r2 > r1:   return Divergence.HIDDEN_BEARISH
        return Divergence.NONE
    except Exception:
        return Divergence.NONE


# ══════════════════════════════════════════════════════════════════
# MODEL C — MEAN REVERSION
# ══════════════════════════════════════════════════════════════════
def model_mean_reversion(closes: List[float], highs: List[float],
                          lows: List[float]) -> ModelOutput:
    """
    Reversão nos extremos das Bollinger + RSI.

    Só é relevante em regime RANGE. Em tendência forte, o próprio
    fusion engine reduz o peso deste modelo (seção 4).
    """
    m = ModelOutput(name="MEAN_REVERSION")
    if len(closes) < 30:
        m.available = False
        m.reason = "dados insuficientes (<30 candles)"
        return m

    try:
        bb  = bollinger(closes)
        pct = float(bb.get("price_pct", 50))   # 0=banda inf, 100=banda sup
        r   = float(rsi(closes, 14)[-1])
        ch  = choppiness(highs, lows, closes)

        score = 0.0
        if pct <= 5:    score += 45
        elif pct <= 20: score += 25
        elif pct >= 95: score -= 45
        elif pct >= 80: score -= 25

        if r <= 25:   score += 35
        elif r <= 35: score += 15
        elif r >= 75: score -= 35
        elif r >= 65: score -= 15

        if score > 40:
            m.direction, m.confidence = Decision.LONG,  min(100, abs(score))
        elif score < -40:
            m.direction, m.confidence = Decision.SHORT, min(100, abs(score))
        else:
            m.direction, m.confidence = Decision.WAIT, 0.0

        # Mean reversion contra tendência forte é perigoso
        m.risk_score = 30.0 if ch.get("chop") else 65.0
        m.reason  = f"BB_pct={pct:.0f}% RSI={r:.1f}"
        m.details = {"bb_pct": pct, "rsi": r, "squeezed": bb.get("squeezed")}
    except Exception as e:
        m.available = False
        m.reason    = f"erro: {e}"
        log.debug(f"model_mean_reversion: {e}")
    return m


# ══════════════════════════════════════════════════════════════════
# MODEL D — BREAKOUT (seção 7)
# ══════════════════════════════════════════════════════════════════
def model_breakout(closes: List[float], highs: List[float], lows: List[float],
                   volumes: List[float], oi_delta: Optional[float] = None) -> ModelOutput:
    """
    Rompimento COM confirmação obrigatória.

    Diferencia TRUE_BREAKOUT / LIQUIDITY_SWEEP / FALSE_BREAKOUT:
      - fechamento além da região (não apenas o pavio)
      - expansão de volume
      - comportamento do OI (quando disponível)
      - tamanho do pavio de rejeição

    Um rompimento com pavio grande e volume fraco é sweep de liquidez,
    não rompimento — e recebe direção CONTRÁRIA ao rompimento aparente.
    """
    m = ModelOutput(name="BREAKOUT")
    if len(closes) < 30 or len(volumes) < 21:
        m.available = False
        m.reason = "dados insuficientes"
        return m

    try:
        lookback = 20
        prev_high = float(max(highs[-lookback-1:-1]))
        prev_low  = float(min(lows[-lookback-1:-1]))
        c, h, l   = float(closes[-1]), float(highs[-1]), float(lows[-1])
        rng       = max(h - l, 1e-12)

        vol_now = float(volumes[-1])
        vol_avg = float(np.mean(volumes[-21:-1])) or 1.0
        vol_mult = vol_now / vol_avg

        bt   = BreakoutType.NONE
        conf = 0.0
        direction = Decision.WAIT

        if c > prev_high:                      # rompimento de alta
            upper_wick = (h - c) / rng
            if vol_mult >= 1.5 and upper_wick < 0.35:
                bt, direction = BreakoutType.TRUE_BREAKOUT, Decision.LONG
                conf = min(95, 45 + vol_mult * 18)
            elif upper_wick > 0.55:
                bt, direction = BreakoutType.LIQUIDITY_SWEEP, Decision.SHORT
                conf = 55
            else:
                bt, direction = BreakoutType.FALSE_BREAKOUT, Decision.WAIT
                conf = 0
        elif c < prev_low:                     # rompimento de baixa
            lower_wick = (c - l) / rng
            if vol_mult >= 1.5 and lower_wick < 0.35:
                bt, direction = BreakoutType.TRUE_BREAKOUT, Decision.SHORT
                conf = min(95, 45 + vol_mult * 18)
            elif lower_wick > 0.55:
                bt, direction = BreakoutType.LIQUIDITY_SWEEP, Decision.LONG
                conf = 55
            else:
                bt, direction = BreakoutType.FALSE_BREAKOUT, Decision.WAIT
                conf = 0

        # OI confirma: rompimento real vem com abertura de posições
        if oi_delta is not None and bt == BreakoutType.TRUE_BREAKOUT:
            if oi_delta > 0.005:
                conf = min(100, conf + 12)
            elif oi_delta < -0.005:
                conf *= 0.6   # OI caindo = fechamento de posições, não convicção

        m.direction  = direction
        m.confidence = conf
        m.risk_score = 20.0 if bt == BreakoutType.TRUE_BREAKOUT else 75.0
        m.reason     = f"{bt.value} vol×{vol_mult:.2f}"
        m.details    = {"breakout_type": bt.value, "vol_mult": round(vol_mult, 2),
                        "prev_high": prev_high, "prev_low": prev_low,
                        "oi_delta": oi_delta}
    except Exception as e:
        m.available = False
        m.reason    = f"erro: {e}"
        log.debug(f"model_breakout: {e}")
    return m


# ══════════════════════════════════════════════════════════════════
# MODEL E — MARKET STRUCTURE
# ══════════════════════════════════════════════════════════════════
def model_structure(closes: List[float], highs: List[float],
                    lows: List[float]) -> ModelOutput:
    """HH/HL vs LH/LL, BOS e CHoCH via smc_analysis."""
    m = ModelOutput(name="STRUCTURE")
    if len(closes) < 20:
        m.available = False
        m.reason = "dados insuficientes (<20 candles)"
        return m

    try:
        s     = smc_analysis(highs, lows, closes)
        st    = s.get("structure", "NEUTRAL")
        bos   = bool(s.get("bos"))
        choch = bool(s.get("choch"))
        bdir  = s.get("bos_dir", "NONE")

        # ══════════════════════════════════════════════════════════
        # BUG CORRIGIDO — VOCABULÁRIO INCOMPATÍVEL
        #
        # smc_analysis() retorna "UPTREND"/"DOWNTREND"/"RANGING", mas
        # este modelo comparava com "BULLISH"/"BEARISH" — valores que a
        # função NUNCA produz. Nenhuma comparação casava, e o modelo de
        # estrutura (15% do score, o maior peso junto com tendência)
        # retornava WAIT/confiança 0 em TODAS as situações.
        #
        # Visível nos logs: "STRUCTURE WAIT conf=0.0  UPTREND +BOS" —
        # a estrutura era de alta E tinha rompimento confirmado, mas o
        # modelo não contribuía nada.
        # ══════════════════════════════════════════════════════════
        if st in ("UPTREND", "BULLISH"):
            m.direction, m.confidence = Decision.LONG, 70.0
        elif st in ("DOWNTREND", "BEARISH"):
            m.direction, m.confidence = Decision.SHORT, 70.0
        else:   # RANGING, REVERSAL, UNKNOWN
            m.direction, m.confidence = Decision.WAIT, 0.0

        # bos_dir vem como "BULLISH"/"BEARISH" do smc_analysis, não
        # "LONG"/"SHORT" — a condição nunca era satisfeita e o reforço
        # por rompimento confirmado nunca se aplicava.
        if bos and bdir in ("LONG", "SHORT", "BULLISH", "BEARISH"):
            bos_dec = (Decision.LONG if bdir in ("LONG", "BULLISH")
                       else Decision.SHORT)
            if bos_dec == m.direction:
                m.confidence = min(100, m.confidence + 20)
            else:
                m.confidence *= 0.5   # BOS contra a estrutura = conflito

        # CHoCH = mudança de caráter → estrutura em transição, menos confiável
        m.risk_score = 70.0 if choch else 25.0
        if choch:
            m.confidence *= 0.6

        m.reason  = f"{st}{' +BOS' if bos else ''}{' +CHoCH' if choch else ''}"
        m.details = s
    except Exception as e:
        m.available = False
        m.reason    = f"erro: {e}"
        log.debug(f"model_structure: {e}")
    return m


# ══════════════════════════════════════════════════════════════════
# MODEL F — DERIVATIVES
# ══════════════════════════════════════════════════════════════════
def model_derivatives(funding: Optional[float] = None,
                      oi_delta: Optional[float] = None,
                      ls_ratio: Optional[float] = None) -> ModelOutput:
    """
    Funding, OI delta e long/short ratio.

    Seção 14: se NENHUM dado de derivativos estiver disponível, o modelo
    retorna available=False — não contribui com neutro inventado.

    Lógica contrarian: funding muito positivo = excesso de longs
    alavancados = risco de long squeeze.
    """
    m = ModelOutput(name="DERIVATIVES")
    if funding is None and oi_delta is None and ls_ratio is None:
        m.available = False
        m.reason = "DATA_UNAVAILABLE: sem dados de derivativos"
        return m

    try:
        score = 0.0
        parts = []

        if funding is not None:
            if funding > 0.0005:    score -= 35; parts.append(f"funding {funding*100:+.3f}% (longs pagando caro)")
            elif funding > 0.0002:  score -= 15; parts.append(f"funding {funding*100:+.3f}%")
            elif funding < -0.0005: score += 35; parts.append(f"funding {funding*100:+.3f}% (shorts pagando caro)")
            elif funding < -0.0002: score += 15; parts.append(f"funding {funding*100:+.3f}%")
            else:                   parts.append(f"funding neutro {funding*100:+.3f}%")

        if oi_delta is not None:
            if oi_delta > 0.01:    score += 20; parts.append(f"OI +{oi_delta*100:.2f}%")
            elif oi_delta < -0.01: score -= 20; parts.append(f"OI {oi_delta*100:.2f}%")

        if ls_ratio is not None:
            if ls_ratio > 2.0:   score -= 25; parts.append(f"L/S {ls_ratio:.2f} (crowded long)")
            elif ls_ratio < 0.5: score += 25; parts.append(f"L/S {ls_ratio:.2f} (crowded short)")

        if score > 25:
            m.direction, m.confidence = Decision.LONG,  min(100, abs(score))
        elif score < -25:
            m.direction, m.confidence = Decision.SHORT, min(100, abs(score))
        else:
            m.direction, m.confidence = Decision.WAIT, 0.0

        m.risk_score = min(100.0, abs(funding or 0) * 60000)
        m.reason  = " | ".join(parts) or "sem sinal"
        m.details = {"funding": funding, "oi_delta": oi_delta, "ls_ratio": ls_ratio}
    except Exception as e:
        m.available = False
        m.reason    = f"erro: {e}"
        log.debug(f"model_derivatives: {e}")
    return m


# ══════════════════════════════════════════════════════════════════
# MODEL G — RISK / REGIME
# ══════════════════════════════════════════════════════════════════
def model_risk_regime(closes: List[float], highs: List[float],
                      lows: List[float]) -> ModelOutput:
    """
    Não indica direção — mede a QUALIDADE do ambiente para operar.

    Sempre retorna WAIT como direção; sua contribuição é o risk_score,
    usado pelo fusion engine para penalizar o score final.
    """
    m = ModelOutput(name="RISK_REGIME", direction=Decision.WAIT)
    if len(closes) < 30:
        m.available = False
        m.reason = "dados insuficientes (<30 candles)"
        return m

    try:
        a       = atr(highs, lows, closes)
        atr_now = float(a[-1])
        atr_avg = float(np.mean(a[-20:])) if len(a) >= 20 else atr_now
        atr_pct = atr_now / closes[-1] * 100 if closes[-1] else 0.0

        ch    = choppiness(highs, lows, closes)
        ci    = float(ch.get("ci", 50))
        bb    = bollinger(closes)
        adx_v = float(adx(highs, lows, closes).get("adx", 0))

        risk = 0.0
        tags = []
        if ci > 61.8:              risk += 40; tags.append(f"choppy CI={ci:.0f}")
        if adx_v < 20:             risk += 25; tags.append(f"sem tendência ADX={adx_v:.0f}")
        if atr_pct > 3.0:          risk += 25; tags.append(f"vol alta ATR={atr_pct:.2f}%")
        elif atr_pct < 0.3:        risk += 20; tags.append(f"vol baixa ATR={atr_pct:.2f}%")
        if atr_avg > 0 and atr_now > atr_avg * 2.0:
            risk += 30; tags.append("expansão abrupta de volatilidade")
        if bb.get("squeezed"):     risk += 10; tags.append("BB squeeze")

        m.risk_score = min(100.0, risk)
        m.confidence = max(0.0, 100.0 - m.risk_score)
        m.reason     = " | ".join(tags) or "ambiente favorável"
        m.details    = {"atr_pct": round(atr_pct, 3), "ci": ci, "adx": adx_v,
                        "atr_expansion": round(atr_now / atr_avg, 2) if atr_avg else 1.0}
    except Exception as e:
        m.available = False
        m.reason    = f"erro: {e}"
        log.debug(f"model_risk_regime: {e}")
    return m


def run_ensemble(closes, highs, lows, volumes,
                 funding=None, oi_delta=None, ls_ratio=None) -> List[ModelOutput]:
    """Executa os 7 modelos. Cada um é independente e isolado por try/except."""
    return [
        model_trend(closes, highs, lows, volumes),
        model_momentum(closes),
        model_mean_reversion(closes, highs, lows),
        model_breakout(closes, highs, lows, volumes, oi_delta),
        model_structure(closes, highs, lows),
        model_derivatives(funding, oi_delta, ls_ratio),
        model_risk_regime(closes, highs, lows),
    ]
