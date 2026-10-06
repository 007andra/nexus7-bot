"""Historical segmented OOS for SHORT + TRENDING_DOWN + BOS_BREAK.

Research-only. Uses the same closed-candle Analyzer + NEXUS historical clock
path as the corrected OOS replay, but reports only the frozen segment that is
being validated prospectively. Historical data at or after 2026-10-06 00:00 UTC
is excluded so the three R3 UNI observations cannot leak into this report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path

from bot.backtest import _closed_window_by_ts, _timestamp_index
from bot.config import cfg
from bot.nexus_oos_real_replay import PublicKuCoinFuturesClient, _funding_at
from bot.kucoin_execution_model import fetch_public_funding_history
from bot.nexus_oos_real_replay_corrected import (
    _freeze_full_clock,
    fetch_history_contiguous,
)

HISTORICAL_CUTOFF_MS = 1791244800000  # 2026-10-06T00:00:00Z
SEGMENT = {
    "side": "SHORT",
    "regime": "TRENDING_DOWN",
    "setup": "BOS_BREAK",
}


def _finite(v):
    try:
        x=float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _outcome(k15, *, decision_idx, entry, side, horizon_minutes):
    bars_needed = horizon_minutes // 15
    start = decision_idx
    end = start + bars_needed
    if start < 0 or end > len(k15) or bars_needed <= 0:
        return None
    bars = k15[start:end]
    sign = 1.0 if side == "LONG" else -1.0
    vals=[]
    for bar in bars:
        vals.append(sign * (float(bar["h"]) / entry - 1.0))
        vals.append(sign * (float(bar["l"]) / entry - 1.0))
    future = sign * (float(bars[-1]["c"]) / entry - 1.0)
    return {
        "future_return": future,
        "MFE": max(0.0, max(vals)),
        "MAE": min(0.0, min(vals)),
    }


def _mean(rows, key):
    vals=[float(r[key]) for r in rows if _finite(r.get(key)) is not None]
    return sum(vals)/len(vals) if vals else None


def _positive_rate(rows):
    return (
        sum(1 for r in rows if float(r["future_return"]) > 0.0) / len(rows)
        if rows else None
    )


def summarize(rows):
    approved=[r for r in rows if r["nexus_allowed"]]
    rejected=[r for r in rows if not r["nexus_allowed"]]
    by_symbol={}
    for row in approved:
        by_symbol.setdefault(row["symbol"], []).append(row)

    symbol_summary={}
    for symbol, items in sorted(by_symbol.items()):
        symbol_summary[symbol]={
            "n":len(items),
            "avg_return_60m":_mean([x["outcome_60m"] for x in items],"future_return"),
            "avg_return_240m":_mean([x["outcome_240m"] for x in items],"future_return"),
            "positive_rate_60m":_positive_rate([x["outcome_60m"] for x in items]),
            "positive_rate_240m":_positive_rate([x["outcome_240m"] for x in items]),
        }

    a60=[r["outcome_60m"] for r in approved]
    a240=[r["outcome_240m"] for r in approved]
    r60=[r["outcome_60m"] for r in rejected]
    r240=[r["outcome_240m"] for r in rejected]

    blockers=[]
    if len(approved) < 10:
        blockers.append("APPROVED_SAMPLE_LT_10")
    if _mean(a60,"future_return") is None or _mean(a60,"future_return") <= 0:
        blockers.append("APPROVED_AVG_60M_NOT_POSITIVE")
    if _mean(a240,"future_return") is None or _mean(a240,"future_return") <= 0:
        blockers.append("APPROVED_AVG_240M_NOT_POSITIVE")

    return {
        "status":"SUPPORTIVE_HISTORICAL_CONTEXT" if not blockers else "HISTORICAL_SUPPORT_NOT_ESTABLISHED",
        "blockers":blockers,
        "segment":SEGMENT,
        "historical_cutoff_ms":HISTORICAL_CUTOFF_MS,
        "r3_seed_excluded":True,
        "segment_candidates":len(rows),
        "approved_candidates":len(approved),
        "rejected_candidates":len(rejected),
        "approved_60m":{
            "avg_return":_mean(a60,"future_return"),
            "positive_rate":_positive_rate(a60),
            "avg_mfe":_mean(a60,"MFE"),
            "avg_mae":_mean(a60,"MAE"),
        },
        "approved_240m":{
            "avg_return":_mean(a240,"future_return"),
            "positive_rate":_positive_rate(a240),
            "avg_mfe":_mean(a240,"MFE"),
            "avg_mae":_mean(a240,"MAE"),
        },
        "rejected_60m":{
            "avg_return":_mean(r60,"future_return"),
            "positive_rate":_positive_rate(r60),
        },
        "rejected_240m":{
            "avg_return":_mean(r240,"future_return"),
            "positive_rate":_positive_rate(r240),
        },
        "symbol_summary":symbol_summary,
        "research_only":True,
        "historical_context_only":True,
        "prospective_sample_credit":False,
        "automatic_promotion":False,
        "promotion_allowed":False,
        "live_allowed":False,
        "decision_effect":"NONE",
        "execution_effect":"NONE",
    }


async def replay_symbol(client, symbol, *, limit_15m):
    from bot.strategy import Analyzer
    from bot import nexus_ai

    k15=await fetch_history_contiguous(client,symbol,"15",limit_15m)
    k1h=await fetch_history_contiguous(client,symbol,"60",max(900,limit_15m//4+120))
    k4h=await fetch_history_contiguous(client,symbol,"240",max(300,limit_15m//16+80))
    if len(k15)<200 or len(k1h)<60 or len(k4h)<30:
        return {"symbol":symbol,"error":"insufficient_history","rows":[]}

    start_ms=int(_timestamp_index(k15)[0])
    end_ms=min(
        HISTORICAL_CUTOFF_MS,
        int(_timestamp_index(k15)[-1]) + 15 * 60 * 1000,
    )
    funding_events=await fetch_public_funding_history(
        client, symbol, start_ms, end_ms
    )

    ts15=_timestamp_index(k15)
    ts1h=_timestamp_index(k1h)
    ts4h=_timestamp_index(k4h)
    analyzer=Analyzer()
    rows=[]

    for i in range(80,len(k15)-16):
        decision_ts=ts15[i]
        # Require the entire 240m observation path to be pre-cutoff. This
        # prevents any outcome candle from the R3 day leaking into the
        # historical-context report.
        if decision_ts + 240 * 60 * 1000 > HISTORICAL_CUTOFF_MS:
            continue

        w15=_closed_window_by_ts(k15,ts15,decision_ts,15,80)
        w1h=_closed_window_by_ts(k1h,ts1h,decision_ts,60,50)
        w4h=_closed_window_by_ts(k4h,ts4h,decision_ts,240,30)
        if len(w15)<60 or len(w1h)<40 or len(w4h)<20:
            continue

        try:
            sig=analyzer.analyze_mtf(
                symbol,w15,w1h,w4h,
                min_score=int(getattr(cfg,"MIN_ENTRY_SCORE",65)),
                fee_mult=getattr(cfg,"FEE_MULTIPLIER",2.0),
                vol_mult=getattr(cfg,"MIN_VOLUME_MULT",1.2),
            )
        except Exception:
            continue

        if not sig or float(sig.rr) < float(getattr(cfg,"MIN_RR_RATIO",2.0)):
            continue
        side=str(getattr(sig,"direction","")).upper()
        regime=str(getattr(sig,"regime","")).upper()
        setup=str(getattr(sig,"entry_type","")).upper()
        if side != SEGMENT["side"] or regime != SEGMENT["regime"] or setup != SEGMENT["setup"]:
            continue

        entry=float(sig.entry)
        o60=_outcome(k15,decision_idx=i,entry=entry,side=side,horizon_minutes=60)
        o240=_outcome(k15,decision_idx=i,entry=entry,side=side,horizon_minutes=240)
        if o60 is None or o240 is None:
            continue

        ticker={"lastPrice":str(float(k15[i]["o"]))}
        funding=_funding_at(funding_events, decision_ts)
        with _freeze_full_clock(nexus_ai,decision_ts):
            nx=nexus_ai.decide(
                symbol,w15,w1h,w4h,
                entry=entry,
                sl=float(sig.sl),
                tp=float(sig.tp),
                ticker=ticker,
                funding=funding,
                oi=None,
                orderbook=None,
                min_score=float(getattr(cfg,"NEXUS_MIN_SCORE",55)),
            )

        nx_regime=str(getattr(nx,"market_regime","") or "").upper()
        allowed=getattr(nx,"execution_allowed",False) is True
        rows.append({
            "symbol":symbol,
            "decision_ts":int(decision_ts),
            "side":side,
            "signal_regime":regime,
            "setup":setup,
            "strategy_score":float(getattr(sig,"score",0) or 0),
            "entry":entry,
            "stop":float(sig.sl),
            "target":float(sig.tp),
            "nexus_allowed":allowed,
            "nexus_regime":nx_regime,
            "nexus_score":float(getattr(nx,"setup_quality",0) or 0),
            "nexus_rr":float(getattr(nx,"risk_reward",0) or 0),
            "nexus_ev":float(getattr(nx,"expected_value",0) or 0),
            "outcome_60m":o60,
            "outcome_240m":o240,
        })

    return {"symbol":symbol,"rows":rows}


async def run(symbols, *, limit_15m):
    from bot.runtime_bootstrap import install as install_runtime
    install_runtime()

    reports=[]
    rows=[]
    async with PublicKuCoinFuturesClient() as client:
        for symbol in symbols:
            rep=await replay_symbol(client,symbol,limit_15m=limit_15m)
            reports.append({
                "symbol":symbol,
                "candidate_count":len(rep.get("rows",[])),
                "error":rep.get("error"),
            })
            rows.extend(rep.get("rows",[]))

    return {
        **summarize(rows),
        "symbols":reports,
        "rows":rows,
        "methodology":{
            "closed_candles_only":True,
            "same_strategy_analyzer":True,
            "same_nexus_decision":True,
            "historical_clock_frozen":True,
            "funding_history_included_when_available":True,
            "entire_240m_path_pre_cutoff":True,
            "outcomes_match_hard_gate_shadow_semantics":True,
            "cutoff":"2026-10-06T00:00:00Z",
            "exchange_mutations":False,
            "runtime_policy_mutations":False,
        },
    }


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--symbols",nargs="+",default=[
        "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT",
        "AVAXUSDT","ADAUSDT","ATOMUSDT","AAVEUSDT","UNIUSDT",
    ])
    p.add_argument("--limit-15m",type=int,default=2500)
    p.add_argument("--output",default="artifacts/oos_short_down_bos_history.json")
    args=p.parse_args()
    report=asyncio.run(run(args.symbols,limit_15m=args.limit_15m))
    out=Path(args.output)
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,indent=2,sort_keys=True),encoding="utf-8")
    print(json.dumps({
        "status":report["status"],
        "blockers":report["blockers"],
        "segment_candidates":report["segment_candidates"],
        "approved_candidates":report["approved_candidates"],
        "rejected_candidates":report["rejected_candidates"],
        "approved_60m":report["approved_60m"],
        "approved_240m":report["approved_240m"],
        "symbol_summary":report["symbol_summary"],
        "r3_seed_excluded":report["r3_seed_excluded"],
        "prospective_sample_credit":False,
        "live_allowed":False,
        "output":str(out),
    },sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
