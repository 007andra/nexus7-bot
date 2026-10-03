import time
"""
BGX Capital Strategy v11.0
Multi-Timeframe: 4H → 1H → 15M
Entrada antecipada: BOS_BREAK > MOMENTUM > PULLBACK
Indicadores: ADX, BB Width, Choppiness, VWAP, SMC, Delta, OB Imbalance
"""
import os
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, Tuple
from bot.indicators import (
    ema, rsi, macd, atr,
    adx as adx_fn, bollinger, choppiness as chop_fn,
    vwap as vwap_fn, volume_profile,
    orderbook_imbalance, delta_footprint, smc_analysis,
)
from bot.logger import log
from bot.config import cfg
from bot.mtf_strategy_observability import observe_analyze_mtf



# ─── Níveis Técnicos Reais ────────────────────────────────────────
def find_support_resistance(highs: list, lows: list, closes: list, atr_v: float) -> dict:
    """
    Encontra suporte e resistência REAIS baseados em swing points.
    Trader profissional: SL SEMPRE atrás de um nível técnico real.
    """
    h = np.array(highs[-50:], dtype=float)
    l = np.array(lows[-50:],  dtype=float)
    c = np.array(closes[-50:],dtype=float)
    price = c[-1]

    # Swing highs: pico local (maior que os 2 vizinhos de cada lado)
    swing_highs = []
    swing_lows  = []
    for i in range(2, len(h)-2):
        if h[i] > h[i-1] and h[i] > h[i-2] and h[i] > h[i+1] and h[i] > h[i+2]:
            swing_highs.append(float(h[i]))
        if l[i] < l[i-1] and l[i] < l[i-2] and l[i] < l[i+1] and l[i] < l[i+2]:
            swing_lows.append(float(l[i]))

    # Suportes: swing lows abaixo do preço atual
    supports = sorted([s for s in swing_lows if s < price], reverse=True)
    # Resistências: swing highs acima do preço atual
    resistances = sorted([r for r in swing_highs if r > price])

    # Fallback: se não há níveis técnicos, usa ATR
    nearest_support    = supports[0]    if supports    else price - atr_v * 1.5
    nearest_resistance = resistances[0] if resistances else price + atr_v * 3.0

    # Segundo nível para TP2
    second_resistance = resistances[1] if len(resistances) > 1 else nearest_resistance + atr_v * 1.5
    second_support    = supports[1]    if len(supports)    > 1 else nearest_support    - atr_v * 1.5

    return {
        "nearest_support":    nearest_support,
        "nearest_resistance": nearest_resistance,
        "second_support":     second_support,
        "second_resistance":  second_resistance,
        "supports":           supports[:3],
        "resistances":        resistances[:3],
    }


def calc_sl_tp(direction: str, entry: float, levels: dict, atr_v: float) -> dict:
    """
    Calcula SL e TP1/TP2 baseados em níveis técnicos reais.

    LONG:
      SL  = abaixo do suporte mais próximo (+ buffer de 0.2x ATR)
      TP1 = primeira resistência (fecha 50% da posição)
      TP2 = segunda resistência (fecha os outros 50%)

    SHORT:
      SL  = acima da resistência mais próxima (+ buffer de 0.2x ATR)
      TP1 = primeiro suporte (fecha 50% da posição)
      TP2 = segundo suporte (fecha os outros 50%)
    """
    buffer = atr_v * 0.2   # pequeno buffer para evitar stop hunt

    if direction == "LONG":
        sl  = levels["nearest_support"]  - buffer
        tp1 = levels["nearest_resistance"]
        tp2 = levels["second_resistance"]
    else:
        sl  = levels["nearest_resistance"] + buffer
        tp1 = levels["nearest_support"]
        tp2 = levels["second_support"]

    # Validações de qualidade
    risk    = abs(entry - sl)
    reward1 = abs(tp1 - entry)
    reward2 = abs(tp2 - entry)

    # Se RR do TP1 < 1.0, empurra TP1 para mínimo de 1:1
    if risk > 0 and reward1 / risk < 1.0:
        if direction == "LONG":
            tp1 = entry + risk * 1.2
        else:
            tp1 = entry - risk * 1.2

    # TP2 deve ser pelo menos 2x o risco
    if risk > 0 and reward2 / risk < 2.0:
        if direction == "LONG":
            tp2 = entry + risk * 2.5
        else:
            tp2 = entry - risk * 2.5

    rr1 = round(abs(tp1 - entry) / risk, 2) if risk > 0 else 0
    rr2 = round(abs(tp2 - entry) / risk, 2) if risk > 0 else 0

    return {
        "sl":  round(sl,  6),
        "tp1": round(tp1, 6),
        "tp2": round(tp2, 6),
        "rr1": rr1,
        "rr2": rr2,
        "risk": round(risk, 6),
    }


# Signal pre-filter cost assumption (purpose: reject setups whose target move
# cannot cover costs). Deliberately separate from the per-candidate execution
# cost authority (bot.execution_cost) used by NEXUS EV/R:R, sizing and loss
# ceilings; values unchanged pending OOS evidence. Default 6 bps is the
# exchange-agnostic conservative taker fee, not a KuCoin-specific rate.
TAKER_FEE   = float(os.environ.get('TAKER_FEE', '0.0006'))
SLIPPAGE    = 0.00020
FUNDING_FEE = 0.00010
TOTAL_COST  = (TAKER_FEE + SLIPPAGE) * 2 + FUNDING_FEE


def _rr_from_unrounded_levels(entry: float, raw_sl: float, raw_tp: float) -> float:
    """Compute R:R before exchange-price rounding can distort the ratio."""
    risk = abs(raw_sl - entry)
    if risk <= 0:
        return 0.0
    return abs(raw_tp - entry) / risk


@dataclass
class Signal:
    symbol:       str
    direction:    str
    entry:        float
    sl:           float
    tp:           float
    confidence:   float
    reason:       str  = ""
    score:        int  = 0
    tf_4h:        str  = ""
    tf_1h:        str  = ""
    tf_15m:       str  = ""
    expected_pnl: float = 0.0
    total_fees:   float = 0.0
    entry_type:       str   = "PULLBACK"
    regime:           str   = "RANGING"   # regime de mercado detectado no 4H
    choch_exit_price: float = 0.0         # preço de saída por invalidação CHoCH (0 = não ativo)
    stagnation_bars:  int   = 0           # candles sem movimento (para saída por tempo)
    tp1:          float = 0.0   # TP parcial 50% — primeiro alvo técnico
    tp2:          float = 0.0   # TP final  50% — segundo alvo técnico
    rr1:          float = 0.0   # R/R do TP1
    rr2:          float = 0.0   # R/R do TP2
    rr:           float = field(init=False)

    def __post_init__(self):
        risk   = abs(self.entry - self.sl)
        reward = abs(self.tp - self.entry)
        self.rr = round(reward / risk, 2) if risk > 0 else 0
        # Sincroniza tp1/tp2 se não fornecidos
        if self.tp1 == 0.0:
            self.tp1 = self.tp
        if self.tp2 == 0.0:
            self.tp2 = self.tp
        # Validações de integridade — usando ValueError (funciona mesmo com python -O)
        if self.entry <= 0:
            raise ValueError(f"Signal.entry inválido: {self.entry}")
        if self.sl <= 0:
            raise ValueError(f"Signal.sl inválido: {self.sl}")
        if self.tp <= 0:
            raise ValueError(f"Signal.tp inválido: {self.tp}")
        if self.direction not in ("LONG", "SHORT"):
            raise ValueError(f"Signal.direction inválido: {self.direction}")
        if self.direction == "LONG":
            if not (self.sl < self.entry < self.tp):
                raise ValueError(
                    f"LONG inválido: sl={self.sl} entry={self.entry} tp={self.tp}"
                )
        else:
            if not (self.tp < self.entry < self.sl):
                raise ValueError(
                    f"SHORT inválido: tp={self.tp} entry={self.entry} sl={self.sl}"
                )


# ─── Regime Detector ─────────────────────────────────────────────
def detect_regime(closes, highs, lows, atr_v) -> str:
    if len(closes) < 20:
        return "UNKNOWN"
    price   = closes[-1]
    atr_arr = atr(highs, lows, closes)
    atr_avg = float(np.mean(atr_arr[-20:])) if len(atr_arr) >= 20 else atr_v
    try:
        ci       = chop_fn(highs, lows, closes)
        adx_data = adx_fn(highs, lows, closes)
        adx_v    = adx_data["adx"]
        e20      = float(ema(closes, 20)[-1])
        e50      = float(ema(closes, min(50, len(closes)-1))[-1])
        if atr_v < atr_avg * 0.60:
            return "COMPRESSED"
        if adx_v > 20:
            return "TRENDING_UP" if price > e20 else "TRENDING_DOWN"
        if e20 > e50 * 1.001 and price > e20:
            return "TRENDING_UP"
        if e20 < e50 * 0.999 and price < e20:
            return "TRENDING_DOWN"
        if ci["chop"]:
            return "RANGING"
        if abs(e20 - e50) / e50 > 0.005:
            return "TRENDING_UP" if e20 > e50 else "TRENDING_DOWN"
        return "RANGING"
    except Exception:
        try:
            e20 = float(ema(closes, 20)[-1])
            e50 = float(ema(closes, min(50, len(closes)-1))[-1])
            if e20 > e50 and price > e20: return "TRENDING_UP"
            if e20 < e50 and price < e20: return "TRENDING_DOWN"
        except Exception as _e:
            log.warning(f"detecção de regime falhou — usando NEUTRAL: {_e}")
        return "RANGING"


# ─── Entry Type Detector ─────────────────────────────────────────
def detect_entry(closes, highs, lows, opens, volumes, direction, atr_v) -> Tuple[bool, str]:
    """
    Detecta o melhor tipo de entrada (do mais antecipado ao mais conservador).
    BOS_BREAK → MOMENTUM → PULLBACK → NONE
    """
    if len(closes) < 6:
        return False, "NONE"

    # Usar candle [-2] (fechado) para evitar repainting
    # closes[-1] já é o candle fechado pois analyze_mtf faz k15[:-1]
    price = closes[-1]

    # 1) BOS Entry: preço fecha acima do swing high (LONG) ou abaixo do swing low (SHORT)
    # LOGIC-1: lookback aumentado de 7 para 20 candles para reduzir falsos BOS por ruído.
    # Exige também que o breakout seja > 0.1× ATR (filtra micro-rompimentos)
    bos_lookback = 20
    swing_high = max(highs[-bos_lookback:-1]) if len(highs) > bos_lookback else max(highs[:-1])
    swing_low  = min(lows[-bos_lookback:-1])  if len(lows)  > bos_lookback else min(lows[:-1])

    if direction == "LONG"  and price > swing_high and (price - swing_high) > atr_v * 0.1:
        return True, "BOS_BREAK"
    if direction == "SHORT" and price < swing_low  and (swing_low - price)  > atr_v * 0.1:
        return True, "BOS_BREAK"

    # 2) Momentum: vela forte na direção sem esperar pullback
    body_size = abs(closes[-1] - opens[-1]) if opens else abs(closes[-1] - closes[-2])
    if direction == "LONG"  and closes[-1] > closes[-2] and body_size > atr_v * 0.25:
        return True, "MOMENTUM"
    if direction == "SHORT" and closes[-1] < closes[-2] and body_size > atr_v * 0.25:
        return True, "MOMENTUM"

    # 3) Pullback clássico — FIX-4: lookback 6 → 20 candles
    # 6 velas = 1.5h no 15M = micro-range sem significado estrutural
    # 20 velas = 5h no 15M = suporte/resistência real com significado
    pb_window = 20
    if direction == "LONG":
        local_min = min(lows[-pb_window:]) if len(lows) >= pb_window else min(lows)
        local_max = max(highs[-pb_window:]) if len(highs) >= pb_window else max(highs)
        if ((local_max - local_min) >= atr_v * 0.35
                and price > local_min + atr_v * 0.12):
            return True, "PULLBACK"
    else:
        local_max = max(highs[-pb_window:]) if len(highs) >= pb_window else max(highs)
        local_min = min(lows[-pb_window:]) if len(lows) >= pb_window else min(lows)
        if ((local_max - local_min) >= atr_v * 0.35
                and price < local_max - atr_v * 0.12):
            return True, "PULLBACK"

    return False, "NONE"


# ─── Score de Confluência ─────────────────────────────────────────
# ── Buffer de scores avaliados (diagnóstico) ─────────────────────
# Guarda os últimos scores de TODOS os pares analisados, mesmo os que
# não viram sinal. Sem isso é impossível responder "quão perto o bot
# está de operar?" quando nada passa no filtro.
_SCORE_LOG: list = []
_SCORE_LOG_MAX = 300


def record_score(symbol: str, combined: int, s4h: int, s1h: int, s15: int):
    """Registra um score avaliado no buffer de diagnóstico."""
    global _SCORE_LOG
    _SCORE_LOG.append({
        "symbol": symbol, "score": combined,
        "s4h": s4h, "s1h": s1h, "s15": s15,
        "ts": time.time(),
    })
    if len(_SCORE_LOG) > _SCORE_LOG_MAX:
        _SCORE_LOG = _SCORE_LOG[-_SCORE_LOG_MAX:]


def get_score_log(limit: int = 300) -> list:
    """Últimos scores avaliados, do mais recente para o mais antigo."""
    return list(reversed(_SCORE_LOG[-limit:]))


def score_tf(closes, highs, lows, opens, volumes, direction,
             atr_v, atr_avg, orderbook=None) -> dict:
    """
    Score 0-100 por timeframe.
    TENDÊNCIA   30pts: ADX + EMA + SMC (HH/HL, BOS)
    VOLUME      20pts: vol ratio + VWAP + OB imbalance
    MOMENTUM    20pts: RSI + MACD + Delta footprint
    VOLATILIDADE15pts: ATR + BB Width + Choppiness
    ESTRUTURA   15pts: SMC struct + anti-choch + anti-fake
    """
    price = closes[-1]
    if price <= 0 or atr_v <= 0:
        return {"ok": False, "total": 0}

    vols    = np.array(volumes, dtype=float)
    avg_vol = vols[-21:-1].mean() if len(vols) > 21 else (vols.mean() or 1)
    vol_r   = float(vols[-1] / avg_vol)

    # ── TENDÊNCIA (30 pts) ───────────────────────────────────────
    # ADX
    try:
        adx_data  = adx_fn(highs, lows, closes)
        adx_v     = adx_data["adx"]
        adx_trend = adx_v > 25
        adx_dir   = adx_data["direction"]
        adx_aligned = adx_dir == direction
    except Exception:
        adx_v = 20; adx_trend = False; adx_aligned = True

    # EMA stack
    e20  = float(ema(closes, 20)[-1])
    e50  = float(ema(closes, 50)[-1])
    e200 = float(ema(closes, min(200, len(closes)-1))[-1])
    bull = not np.isnan(e20) and not np.isnan(e50) and e20 > e50 and price > e20
    bear = not np.isnan(e20) and not np.isnan(e50) and e20 < e50 and price < e20
    full_stack = (bull and e50 > e200) or (bear and e50 < e200)
    aligned    = (direction == "LONG" and bull) or (direction == "SHORT" and bear)

    # SMC
    try:
        smc = smc_analysis(highs, lows, closes)
    except Exception:
        smc = {"structure":"UNKNOWN","hh":False,"hl":False,"lh":False,"ll":False,
               "bos":False,"bos_dir":"NONE","choch":False}

    # VWAP
    try:
        vwap_v = vwap_fn(highs, lows, closes, volumes)
        vwap_ok= (direction == "LONG" and price > vwap_v) or \
                 (direction == "SHORT" and price < vwap_v)
    except Exception:
        vwap_v = price; vwap_ok = True

    trend_s = 0
    # ADX contribution
    if adx_v > 30 and aligned:     trend_s += 10
    elif adx_v > 25 and aligned:   trend_s += 7
    elif adx_v > 20 and aligned:   trend_s += 4
    # EMA stack
    if full_stack and aligned:     trend_s += 10
    elif aligned:                  trend_s += 6
    # SMC BOS
    if smc["bos"] and direction in smc["bos_dir"]: trend_s += 6
    if (direction=="LONG" and smc["hh"] and smc["hl"]) or \
       (direction=="SHORT" and smc["lh"] and smc["ll"]): trend_s += 4
    # VWAP
    if vwap_ok: trend_s += 3
    # Penaliza ADX lateral e CHoCH
    if adx_v < 20:    trend_s = min(trend_s, 10)
    if smc["choch"]:  trend_s = max(0, trend_s - 5)
    trend_s = max(0, min(30, trend_s))

    # ── VOLUME (20 pts) ──────────────────────────────────────────
    try:
        ob = orderbook_imbalance(orderbook) if orderbook else {"bias":"NEUTRAL"}
        ob_ok = (direction=="LONG" and ob["bias"]=="BID_HEAVY") or \
                (direction=="SHORT" and ob["bias"]=="ASK_HEAVY")
    except Exception:
        ob_ok = False

    try:
        vp    = volume_profile(highs, lows, volumes)
        poc_ok= (direction=="LONG" and price > vp["poc"]) or \
                (direction=="SHORT" and price < vp["poc"])
    except Exception:
        poc_ok = True

    bodies_ok = [(closes[i] > opens[i]) == (direction=="LONG") for i in range(-3, 0)]
    if vol_r >= 1.8 and all(bodies_ok):         vol_s = 20
    elif vol_r >= 1.4 and sum(bodies_ok) >= 2:  vol_s = 14
    elif vol_r >= 1.1:                           vol_s = 9
    elif vol_r >= 0.8:                           vol_s = 5
    else:                                        vol_s = 2
    if ob_ok:  vol_s = min(20, vol_s + 2)
    if poc_ok: vol_s = min(20, vol_s + 2)
    vol_s = max(0, vol_s)

    # ── MOMENTUM (20 pts) ────────────────────────────────────────
    rsi_v = float(rsi(closes)[-1])
    try:
        _, _, hist = macd(closes)
        h0 = float(hist[-1]) if not np.isnan(hist[-1]) else 0
        h1 = float(hist[-2]) if len(hist)>1 and not np.isnan(hist[-2]) else h0
    except Exception:
        h0 = 0; h1 = 0

    try:
        fp = delta_footprint(closes, list(vols), opens)
        fp_ok = (direction=="LONG" and fp["bias"]=="BULLISH") or \
                (direction=="SHORT" and fp["bias"]=="BEARISH")
        fp_div = fp["divergence"]
    except Exception:
        fp_ok = True; fp_div = False

    if direction == "LONG":
        if 40 <= rsi_v <= 80:   rsi_s = 10
        elif 33 <= rsi_v < 40:  rsi_s = 6
        elif 80 < rsi_v <= 90:  rsi_s = 4
        elif rsi_v > 90:        rsi_s = 2
        else:                   rsi_s = 0
        if h0 > 0 and h0 > h1: macd_s = 10
        elif h0 > 0:            macd_s = 6
        elif h0 > h1:           macd_s = 4
        else:                   macd_s = 0
    else:
        if 20 <= rsi_v <= 60:   rsi_s = 10
        elif 60 < rsi_v <= 67:  rsi_s = 6
        elif 10 <= rsi_v < 20:  rsi_s = 4
        elif rsi_v < 10:        rsi_s = 2
        else:                   rsi_s = 0
        if h0 < 0 and h0 < h1: macd_s = 10
        elif h0 < 0:            macd_s = 6
        elif h0 < h1:           macd_s = 4
        else:                   macd_s = 0

    momentum_s = rsi_s + macd_s
    if fp_ok and not fp_div: momentum_s = min(20, momentum_s + 3)
    if fp_div:               momentum_s = max(0, momentum_s - 3)
    momentum_s = max(0, min(20, momentum_s))

    # ── VOLATILIDADE (15 pts) ────────────────────────────────────
    atr_pct       = atr_v / price * 100
    atr_expanding = atr_v > atr_avg * 1.03

    try:
        bb      = bollinger(closes)
        bb_squeeze = bb["squeezed"]
        bb_width   = bb["width"]
    except Exception:
        bb_squeeze = False; bb_width = 3.0

    try:
        ci_data  = chop_fn(highs, lows, closes)
        ci_chop  = ci_data["chop"]
        ci_trend = ci_data["trending"]
        ci_v     = ci_data["ci"]
    except Exception:
        ci_chop = False; ci_trend = True; ci_v = 50

    if atr_expanding and ci_trend and not bb_squeeze:   atr_s = 15
    elif atr_expanding and not ci_chop:                 atr_s = 11
    elif not ci_chop and 0.15 <= atr_pct <= 5.0:        atr_s = 8
    elif ci_chop:                                        atr_s = 3
    else:                                                atr_s = 5
    atr_s = max(0, min(15, atr_s))

    # ── ESTRUTURA (15 pts) ───────────────────────────────────────
    body     = abs(closes[-1] - opens[-1])
    cr       = highs[-1] - lows[-1]
    wick_r   = 1 - (body / cr) if cr > 0 else 1
    struct_s = 15
    if wick_r > 0.72:                             struct_s -= 5
    if vol_r > 3.0 and body < atr_v * 0.10:      struct_s -= 5
    if smc["choch"]:                              struct_s -= 4
    ph = max(highs[-6:-1]) if len(highs) > 6 else highs[-1]
    pl = min(lows[-6:-1])  if len(lows)  > 6 else lows[-1]
    if direction=="LONG"  and highs[-1]>ph and closes[-1]<ph: struct_s -= 5
    if direction=="SHORT" and lows[-1]<pl  and closes[-1]>pl: struct_s -= 5
    if ci_chop: struct_s = max(0, struct_s - 3)
    struct_s = max(0, min(15, struct_s))

    total = trend_s + vol_s + momentum_s + atr_s + struct_s

    return {
        "ok": True, "total": total,
        "trend_s": trend_s, "vol_s": vol_s,
        "momentum_s": momentum_s, "atr_s": atr_s, "struct_s": struct_s,
        "rsi_v": rsi_v, "rsi_s": rsi_s, "macd_s": macd_s,
        "adx_v": adx_v, "adx_trending": adx_trend, "adx_ranging": adx_v < 20,
        # BUG CORRIGIDO: adx_aligned (ADX apontando na mesma direção do sinal)
        # era calculado mas nunca exposto nem usado. Uma tendência forte na
        # direção CONTRÁRIA ao sinal passava despercebida na confluência.
        "adx_aligned": adx_aligned,
        "ci_chop": ci_chop, "ci_trend": ci_trend, "ci_v": ci_v,
        "bb_squeeze": bb_squeeze, "bb_width": bb_width,
        "vwap": vwap_v, "vwap_ok": vwap_ok,
        "smc_structure": smc["structure"], "bos": smc["bos"],
        "choch": smc["choch"], "hh": smc["hh"], "hl": smc["hl"],
        "ob_bias": ob.get("bias","NEUTRAL") if orderbook else "N/A",
        "fp_ok": fp_ok, "fp_div": fp_div,
        "vol_r": vol_r, "atr_v": atr_v, "atr_expanding": atr_expanding,
        "aligned": aligned, "bull": bull, "bear": bear, "full": full_stack,
        "price": price,
        "summary": (
            f"T{trend_s}+V{vol_s}+M{momentum_s}+A{atr_s}+S{struct_s}={total} "
            f"ADX{adx_v:.0f} CI{ci_v:.0f} RSI{rsi_v:.0f}"
        ),
    }


# ─── Analyzer Principal ───────────────────────────────────────────
class Analyzer:
    @observe_analyze_mtf
    def analyze_mtf(self, symbol, k15, k1h, k4h,
                    min_score=60, fee_mult=2.0, vol_mult=1.0) -> Optional[Signal]:
        if len(k4h) < 10 or len(k1h) < 15 or len(k15) < 20:
            log.info(
                f"⛔ [{symbol}] CANDLES INSUFICIENTES: "
                f"4h={len(k4h)}/10 1h={len(k1h)}/15 15m={len(k15)}/20"
            )
            return None

        def ga(kl):
            return ([float(k["c"]) for k in kl], [float(k["h"]) for k in kl],
                    [float(k["l"]) for k in kl], [float(k["o"]) for k in kl],
                    [k["v"] for k in kl])

        # ── Confirmação de candle fechado ────────────────────────
        # O candle [-1] está ABERTO (ainda se formando).
        # Usar [-2] como último candle confirmado evita sinais que "reapintam":
        # um sinal gerado em candle aberto pode desaparecer antes do fechamento.
        # Excluímos o candle atual de TODOS os timeframes por consistência.
        c4h,h4h,l4h,o4h,v4h = ga(k4h[:-1] if len(k4h) > 10 else k4h)
        c1h,h1h,l1h,o1h,v1h = ga(k1h[:-1] if len(k1h) > 15 else k1h)
        c15,h15,l15,o15,v15 = ga(k15[:-1] if len(k15) > 20 else k15)
        price = c15[-1]   # último candle FECHADO do 15M

        def get_atr(h, l, c):
            a = atr(h, l, c)
            return float(a[-1]), float(np.mean(a[-20:])) if len(a) >= 20 else float(a[-1])

        atr_4h, avg_4h = get_atr(h4h, l4h, c4h)
        atr_1h, avg_1h = get_atr(h1h, l1h, c1h)
        atr_15, avg_15 = get_atr(h15, l15, c15)

        # ── PASSO 1: Regime 4H ──────────────────────────────────
        regime = detect_regime(c4h, h4h, l4h, atr_4h)
        # FIX-1: Bloquear RANGING, COMPRESSED e CHOPPY — sistema é trend-follow
        # Em lateralização (60-70% do tempo) o edge desaparece sistematicamente
        # Saídas antecipadas do analyze_mtf: eram todas log.debug ou
        # sem log. Um par podia ser descartado aqui e o operador via
        # apenas "nenhum sinal", sem saber o motivo.
        if regime in ("COMPRESSED", "RANGING", "CHOPPY"):
            log.info(
                f"⛔ [{symbol}] REGIME {regime} no 4H — estratégia é "
                f"trend-follow, só opera TRENDING_UP/DOWN"
            )
            return None

        # ── PASSO 2: Direção (4H + 1H) ─────────────────────────
        e20_4h = float(ema(c4h, 20)[-1])
        e50_4h = float(ema(c4h, 50)[-1])
        e20_1h = float(ema(c1h, 20)[-1])
        e50_1h = float(ema(c1h, 50)[-1])

        bull_4h = e20_4h > e50_4h and c4h[-1] > e20_4h
        bear_4h = e20_4h < e50_4h and c4h[-1] < e20_4h
        bull_1h = e20_1h > e50_1h and c1h[-1] > e20_1h
        bear_1h = e20_1h < e50_1h and c1h[-1] < e20_1h

        # FIX-3: Direção sem zona cinzenta — 4H E 1H obrigatoriamente alinhados
        # Versão anterior permitia entrar se 1H estava em alta mas 4H era neutro
        # Isso gerava entradas em correções de tendências maiores contrárias
        if bull_4h and bull_1h:
            direction = "LONG"
        elif bear_4h and bear_1h:
            direction = "SHORT"
        else:
            # Qualquer ambiguidade: não entra
            log.info(
                f"⛔ [{symbol}] 4H/1H NÃO ALINHADOS "
                f"(bull4h={bull_4h} bear4h={bear_4h} bull1h={bull_1h} bear1h={bear_1h}) → HOLD"
            )
            return None

        # ── PASSO 3: Score confluência ──────────────────────────
        s4h = score_tf(c4h, h4h, l4h, o4h, v4h, direction, atr_4h, avg_4h)
        s1h = score_tf(c1h, h1h, l1h, o1h, v1h, direction, atr_1h, avg_1h)
        s15 = score_tf(c15, h15, l15, o15, v15, direction, atr_15, avg_15)

        if not s4h["ok"] or not s1h["ok"] or not s15["ok"]:
            log.info(
                f"⛔ [{symbol}] SCORE INVÁLIDO: "
                f"4H_ok={s4h['ok']} 1H_ok={s1h['ok']} 15M_ok={s15['ok']}"
            )
            return None

        # Peso: 4H=25%, 1H=30%, 15M=45% (15M tem mais peso no timing)
        combined = round(s4h["total"]*0.25 + s1h["total"]*0.30 + s15["total"]*0.45)

        # Registra TODO score avaliado, inclusive os que darão HOLD.
        # Antes, o histórico só recebia sinais aprovados — então ficava
        # sempre vazio quando nenhum passava, que é justamente o caso em
        # que a informação é mais necessária.
        record_score(symbol, combined, s4h["total"], s1h["total"], s15["total"])

        if combined < min_score:
            log.info(
                f"[{symbol}] Score={combined}/100 < {min_score} → HOLD "
                f"| 4H:{s4h['total']} 1H:{s1h['total']} 15M:{s15['total']} "
                f"| ADX={s15['adx_v']:.0f} CI={s15['ci_v']:.0f} "
                f"RSI={s15['rsi_v']:.0f} vol={s15['vol_r']:.2f}x "
                f"regime={regime}"
            )
            return None

        # ── PASSO 4: Bloqueios críticos ─────────────────────────
        # RSI extremo no 15M
        # Bloqueios pós-score: eram todos log.debug, invisíveis com
        # LOG_LEVEL=INFO. Um sinal com score 79 era descartado aqui sem
        # deixar rastro, dando a impressão de que o score era o problema.
        if s15["rsi_v"] > 92 or s15["rsi_v"] < 8:
            log.info(
                f"⛔ [{symbol}] BLOQUEIO RSI extremo: {s15['rsi_v']:.0f} "
                f"(fora de 8-92) — score era {combined}"
            )
            return None
        # Volume mínimo: 0.40x da média (equilibrio entre liquidez e frequência)
        if s15["vol_r"] < 0.40:
            log.info(
                f"⛔ [{symbol}] BLOQUEIO volume: {s15['vol_r']:.2f}x < 0.40x "
                f"— score era {combined}"
            )
            return None
        # 15M não alinhado — só bloqueia se regime não for TRENDING
        if not s15["aligned"] and regime not in ("TRENDING_UP","TRENDING_DOWN"):
            log.info(
                f"⛔ [{symbol}] BLOQUEIO alinhamento: 15M desalinhado e "
                f"regime={regime} — score era {combined}"
            )
            return None

        # ── PASSO 5: Tipo de entrada ────────────────────────────
        entry_ok, entry_type = detect_entry(
            c15, h15, l15, o15, v15, direction, atr_15
        )
        if not entry_ok:
            combined = max(0, combined - 5)
            if combined < min_score:
                log.info(
                    f"⛔ [{symbol}] BLOQUEIO setup: nenhum gatilho de entrada "
                    f"(pullback/breakout/reversão) — score {combined}"
                )
                return None

        # ── PASSO 6: SL/TP adaptativo por tipo de entrada ───────
        if entry_type == "BOS_BREAK":
            sl_mult, tp_mult = 1.2, 3.6   # R:R 1:3 — entrada mais cedo
        elif entry_type == "MOMENTUM":
            sl_mult, tp_mult = 1.5, 3.0   # R:R 1:2
        else:
            sl_mult, tp_mult = 2.0, 4.0   # R:R 1:2 — pullback clássico

        # ── ATR consistente: usa ATR do 15M para entrada do 15M ─
        # ATR_1H é 3-5x maior que ATR_15M → stops desnecessariamente largos
        # Usamos atr_15 para stops mais ajustados ao timeframe de entrada,
        # com um floor de 0.5× atr_1h para evitar stops muito apertados.
        sl_atr = max(atr_15, atr_1h * 0.5)
        if direction == "LONG":
            raw_sl = price - sl_atr * sl_mult
            raw_tp = price + sl_atr * tp_mult
        else:
            raw_sl = price + sl_atr * sl_mult
            raw_tp = price - sl_atr * tp_mult

        # Gate R:R against the intended, unrounded geometry. Exchange price
        # precision is applied only after the quality gate so a true 1:2
        # setup cannot become 1.9998... solely because SL/TP were rounded.
        rr = _rr_from_unrounded_levels(price, raw_sl, raw_tp)
        if rr < cfg.MIN_RR_RATIO:
            log.debug(f"[{symbol}] R:R {rr:.6f} < {cfg.MIN_RR_RATIO} → HOLD")
            return None

        sl = round(raw_sl, 6)
        tp = round(raw_tp, 6)

        # ── PASSO 7: Validação de taxas ─────────────────────────
        cost_pct   = TOTAL_COST * 100
        move_to_tp = abs(tp - price) / price * 100
        min_move   = cost_pct * fee_mult
        if move_to_tp < min_move:
            log.debug(f"[{symbol}] Move {move_to_tp:.3f}% < {min_move:.3f}% → HOLD")
            return None
        expected_net = move_to_tp - cost_pct

        # ── Monta sinal ─────────────────────────────────────────
        reasons = [
            f"4H:{s4h['total']}",
            f"1H:{s1h['total']}",
            f"15M:{s15['total']}",
            f"ADX{s15['adx_v']:.0f}",
            f"RR{rr:.1f}",
            f"ENTRY:{entry_type}",
        ]
        if s15["bos"]:      reasons.append("BOS✓")
        if s15["vwap_ok"]:  reasons.append("VWAP✓")
        if not s15["ci_chop"]: reasons.append("CI✓")
        if s15["choch"]:    reasons.append("CHoCH⚠")
        if s15["bb_squeeze"]: reasons.append("BB-SQZ")

        log.info(
            f"[{symbol}] ✅ SINAL {direction} score={combined}/100 "
            f"RR={rr:.1f} entry={entry_type} "
            f"| {' '.join(reasons)}"
        )

        signal = Signal(
            symbol=symbol, direction=direction,
            entry=price, sl=sl, tp=tp,
            confidence=min(0.97, combined/100),
            reason=" | ".join(reasons),
            score=int(combined),
            tf_4h=s4h["summary"], tf_1h=s1h["summary"], tf_15m=s15["summary"],
            expected_pnl=round(expected_net, 3),
            total_fees=round(cost_pct, 4),
            entry_type=entry_type,
            regime=regime,
        )

        # Observability-only metadata: these are the exact values already
        # computed by the strategy above. They are never consulted by trading
        # decisions and exist only so the pre-NEXUS wrapper can emit an
        # uncensored stop-geometry record before a pullback veto.
        signal._bgx_atr_15m = float(atr_15)
        signal._bgx_atr_1h = float(atr_1h)
        signal._bgx_adjusted_atr = float(sl_atr)
        formation_candle = (k15[:-1] if len(k15) > 20 else k15)[-1]
        formation_timestamp = float(formation_candle.get("ts", 0.0) or 0.0) / 1000.0
        if formation_timestamp > 0:
            signal._bgx_formation_timestamp = formation_timestamp
            signal._bgx_formation_bucket = int(formation_timestamp // 900)
        signal._bgx_4h_bias = "LONG" if bull_4h else ("SHORT" if bear_4h else "NEUTRAL")
        signal._bgx_1h_bias = "LONG" if bull_1h else ("SHORT" if bear_1h else "NEUTRAL")
        signal._bgx_15m_bias = (
            "LONG" if s15.get("bull") else ("SHORT" if s15.get("bear") else "NEUTRAL")
        )
        return signal

    def analyze(self, symbol, klines):
        return self.analyze_mtf(symbol, klines, klines, klines)

    def rank_signals(self, signals):
        return sorted([s for s in signals if s],
                      key=lambda s: s.score * s.rr, reverse=True)
