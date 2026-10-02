"""Native NEXUS market-language primitives.

Inspired by general ideas in Kronos (MIT): hierarchical market states, temporal
context and autoregressive probabilistic forecasting. No Kronos code, weights,
runtime dependency or external inference service is required.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import math, random
from collections import Counter, defaultdict
from typing import Iterable, Mapping, Sequence

@dataclass(frozen=True)
class CandleToken:
    coarse: tuple[int, ...]
    fine: tuple[int, ...]
    time: tuple[int, int, int, int, int]

@dataclass(frozen=True)
class ForecastDistribution:
    paths: tuple[tuple[CandleToken, ...], ...]
    probability_up: float
    probability_down: float
    median_return: float
    q10_return: float
    q90_return: float
    dispersion: float
    entropy: float

def _quantize(x: float, step: float, limit: int) -> int:
    return max(-limit, min(limit, int(round(x / step))))

def _returns(c: Mapping[str, float], anchor: float) -> tuple[float, ...]:
    if anchor <= 0: raise ValueError("anchor must be > 0")
    vals=[float(c[k]) for k in ("open","high","low","close")]
    if any(not math.isfinite(v) or v <= 0 for v in vals): raise ValueError("invalid OHLC")
    if vals[1] < vals[2]: raise ValueError("high must be >= low")
    volume=max(0.0,float(c.get("volume",0.0)))
    return tuple(math.log(v/anchor) for v in vals)+(math.log1p(volume),)

def temporal_context(ts: datetime) -> tuple[int,int,int,int,int]:
    return ts.minute, ts.hour, ts.weekday(), ts.day, ts.month

def tokenize_candles(candles: Sequence[Mapping[str,float]], timestamps: Sequence[datetime],
                     coarse_step: float=.005, fine_step: float=.001) -> list[CandleToken]:
    if len(candles)!=len(timestamps) or not candles: raise ValueError("aligned non-empty inputs required")
    out=[]; previous=float(candles[0]["open"])
    for candle,ts in zip(candles,timestamps):
        features=_returns(candle,previous)
        # volume is transformed but differenced causally so absolute scale cannot dominate.
        vol=features[-1]
        coarse=tuple(_quantize(v,coarse_step,32) for v in features[:4])+(_quantize(vol/20,coarse_step,32),)
        fine=tuple(_quantize(v, fine_step,64) for v in features[:4])+(_quantize(vol/20,fine_step,64),)
        out.append(CandleToken(coarse,fine,temporal_context(ts)))
        previous=float(candle["close"])
    return out

class AutoregressiveMarketLanguage:
    """Dependency-aware two-level Markov language model for native NEXUS research."""
    def __init__(self, order:int=3):
        if order<1: raise ValueError("order must be >=1")
        self.order=order; self._coarse=defaultdict(Counter); self._fine=defaultdict(Counter); self._returns={}

    def fit(self, tokens:Sequence[CandleToken], closes:Sequence[float]) -> "AutoregressiveMarketLanguage":
        if len(tokens)!=len(closes) or len(tokens)<=self.order: raise ValueError("insufficient aligned history")
        for i in range(self.order,len(tokens)):
            ctx=tuple(t.coarse for t in tokens[i-self.order:i])
            target=tokens[i]
            self._coarse[ctx][target.coarse]+=1
            self._fine[(ctx,target.coarse)][target.fine]+=1
            prev=float(closes[i-1]); cur=float(closes[i])
            self._returns[(target.coarse,target.fine)]=cur/prev-1.0
        return self

    @staticmethod
    def _sample(counter:Counter, rng:random.Random, top_p:float) -> tuple[int,...]:
        if not counter: raise LookupError("unseen market context")
        ranked=counter.most_common(); total=sum(counter.values()); kept=[]; cum=0
        for state,count in ranked:
            kept.append((state,count)); cum+=count/total
            if cum>=top_p: break
        pick=rng.uniform(0,sum(c for _,c in kept)); acc=0
        for state,count in kept:
            acc+=count
            if pick<=acc:return state
        return kept[-1][0]

    def forecast(self, context:Sequence[CandleToken], horizon:int=8, samples:int=64,
                 top_p:float=.9, seed:int=0) -> ForecastDistribution:
        if len(context)<self.order or horizon<1 or samples<1: raise ValueError("invalid forecast request")
        if not 0 < top_p <= 1: raise ValueError("top_p must be in (0,1]")
        rng=random.Random(seed); paths=[]; returns=[]
        for _ in range(samples):
            rolling=list(context); compounded=1.0; path=[]
            for _step in range(horizon):
                ctx=tuple(t.coarse for t in rolling[-self.order:])
                coarse=self._sample(self._coarse[ctx],rng,top_p)
                fine=self._sample(self._fine[(ctx,coarse)],rng,top_p)
                token=CandleToken(coarse,fine,rolling[-1].time)
                path.append(token); rolling.append(token)
                compounded*=1.0+self._returns.get((coarse,fine),0.0)
            paths.append(tuple(path)); returns.append(compounded-1.0)
        ordered=sorted(returns); n=len(ordered)
        p_up=sum(r>0 for r in returns)/n; p_down=sum(r<0 for r in returns)/n
        mean=sum(returns)/n; dispersion=math.sqrt(sum((r-mean)**2 for r in returns)/n)
        probs=[p for p in (p_up,p_down,1-p_up-p_down) if p>0]
        entropy=-sum(p*math.log(p,2) for p in probs)
        return ForecastDistribution(tuple(paths),p_up,p_down,ordered[n//2],
            ordered[max(0,int(.1*(n-1)))],ordered[min(n-1,int(.9*(n-1)))],dispersion,entropy)

def walk_forward(tokens:Sequence[CandleToken], closes:Sequence[float], *, order:int=3,
                 min_train:int=100, horizon:int=1) -> list[dict]:
    """Strictly causal expanding-window evaluation; never trains on future observations."""
    results=[]
    for cut in range(max(min_train,order+1),len(tokens)-horizon):
        model=AutoregressiveMarketLanguage(order).fit(tokens[:cut],closes[:cut])
        try: dist=model.forecast(tokens[:cut],horizon=horizon,samples=32,seed=cut)
        except LookupError: continue
        actual=float(closes[cut+horizon-1])/float(closes[cut-1])-1.0
        results.append({"cut":cut,"p_up":dist.probability_up,"median_return":dist.median_return,
                        "actual_return":actual,"direction_correct":(dist.median_return>=0)==(actual>=0)})
    return results
