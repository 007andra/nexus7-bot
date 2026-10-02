from datetime import datetime,timedelta
import pytest
from bot.nexus_market_language import AutoregressiveMarketLanguage, temporal_context, tokenize_candles, walk_forward

def data(n=180):
    candles=[]; ts=[]; p=100.0
    for i in range(n):
        move=.002 if i%6<4 else -.001
        o=p; c=p*(1+move); h=max(o,c)*1.001; l=min(o,c)*.999
        candles.append({"open":o,"high":h,"low":l,"close":c,"volume":1000+i})
        ts.append(datetime(2026,1,1)+timedelta(minutes=15*i)); p=c
    return candles,ts

def test_temporal_context():
    assert temporal_context(datetime(2026,10,2,14,37))==(37,14,4,2,10)

def test_hierarchical_tokens_and_native_forecast_are_deterministic():
    candles,ts=data(); tokens=tokenize_candles(candles,ts); closes=[c["close"] for c in candles]
    assert tokens[5].coarse != tokens[5].fine
    m=AutoregressiveMarketLanguage(order=2).fit(tokens,closes)
    a=m.forecast(tokens,horizon=3,samples=24,seed=7)
    b=m.forecast(tokens,horizon=3,samples=24,seed=7)
    assert a==b and len(a.paths)==24
    assert 0<=a.probability_up<=1 and 0<=a.probability_down<=1
    assert a.q10_return<=a.median_return<=a.q90_return

def test_walk_forward_is_strictly_causal_and_produces_evidence():
    candles,ts=data(); tokens=tokenize_candles(candles,ts); closes=[c["close"] for c in candles]
    evidence=walk_forward(tokens,closes,order=2,min_train=120)
    assert evidence
    assert all(x["cut"]>=120 for x in evidence)
    assert all("actual_return" in x for x in evidence)

def test_invalid_geometry_fails_closed():
    with pytest.raises(ValueError):
        tokenize_candles([{"open":100,"high":98,"low":99,"close":100}], [datetime.now()])
