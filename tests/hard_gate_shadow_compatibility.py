"""Explicit temporary-merge probes; not imported or installed by production.

Run after merging exactly one peer PR into a disposable worktree:
    python -m tests.hard_gate_shadow_compatibility 492|493|494
Each case fails if that peer's modules are absent; no skipped compatibility.
"""
import asyncio
from copy import deepcopy
import json
import logging
import os
import sys
import time
from unittest.mock import AsyncMock, patch

os.environ["EXCHANGE"] = "binance"
os.environ["PAPER_TRADE"] = "true"
os.environ["NEXUS_TELEGRAM"] = "false"
from tests.run_offline import install_network_guard
from tests.test_hard_gate_shadow_scan import ENV, DB, CacheClient, engine, signal, decision
from bot import hard_gate_shadow_scan as shadow
from bot.hard_gate_shadow_context import AUTHORITY
from bot.config import cfg
from bot.strategy import Analyzer
from bot import nexus_ai
from bot.engine import TradingEngine
from bot.logger import log


async def probe(peer):
    install_network_guard()
    from bot.runtime_bootstrap import install
    install()
    obj, sql = engine(), DB()
    terminal = None
    bbo_views = {}
    if peer == "492":
        from bot import candidate_terminal_telemetry as telemetry
        with patch.dict(os.environ, {"CANDIDATE_TERMINAL_TELEMETRY": "true"}):
            terminal = telemetry.install(log)
        terminal.active = telemetry.CandidateTrace("live-existing", "SOLUSDT", "LONG", "80")
        before = deepcopy(vars(terminal.active)), deepcopy(terminal._candidates), deepcopy(terminal._candidate_ids)
    elif peer == "493":
        assert "bot.feasibility_frontier" not in sys.modules, "frontier auto-imported by runtime"
    elif peer == "494":
        from bot import bbo_cost_shadow_runtime as bbo, binance_bbo_feed as feed
        cache = feed.BBOCache(["SOLUSDT"])
        generation = cache.begin_generation()
        now = int(time.time() * 1000)
        cache.ingest({"e": "bookTicker", "s": "SOLUSDT", "b": "100", "a": "100.01",
                      "B": "50", "A": "50", "u": 123, "T": now, "E": now},
                     generation=generation, received_wall_ms=now, received_mono_ns=time.monotonic_ns())
        bbo_views["SOLUSDT"] = cache.snapshot("SOLUSDT")
        assert bbo_views["SOLUSDT"].valid
    else:
        raise ValueError(peer)
    captured = []
    private_calls = []
    def reject_capability(self, name):
        private_calls.append(name)
        raise AssertionError(name)
    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())
    capture = Capture()
    shadow.log.addHandler(capture)
    try:
        with patch.dict(os.environ, {**ENV, "NEXUS_BBO_COST_SHADOW": "true"}), patch.object(cfg, "MAX_DRAWDOWN", .17), \
             patch.object(Analyzer, "analyze_mtf", return_value=signal()), \
             patch.object(nexus_ai, "decide", return_value=decision()), \
             patch.object(TradingEngine, "_open", new_callable=AsyncMock) as opening, \
             patch.object(TradingEngine, "_nexus_validate", new_callable=AsyncMock) as live_validate, \
             patch.object(TradingEngine, "_filter_viable_symbols", new_callable=AsyncMock) as matrix, \
             patch.object(CacheClient, "__getattr__", new=reject_capability, create=True):
            out = await shadow.scan(obj, db=sql, bbo_views=bbo_views)
        row = out["candidates"][0]
        assert row["population"] == "HARD_GATE_SHADOW" and row["nexus_allowed"]
        assert opening.call_count == live_validate.call_count == matrix.call_count == 0
        assert private_calls == []
        if terminal:
            assert (vars(terminal.active), terminal._candidates, terminal._candidate_ids) == before
            assert not any("[CANDIDATE_TERMINAL]" in line or "[MIN_ORDER_FEASIBILITY_MATRIX]" in line for line in captured)
        if peer == "493":
            from bot.feasibility_frontier import Instrument, CostModel, evaluate
            info, costs = obj.instruments[row["symbol"]], row["cost_snapshot"]
            result = evaluate(Instrument(row["symbol"], info["qtyStep"], info["minQty"], info["minNotional"], "OBSERVED"),
                              price=row["entry"], stop_pct=abs(row["entry"]-row["stop"])/row["entry"],
                              gross_rr=abs(row["target"]-row["entry"])/abs(row["entry"]-row["stop"]),
                              equity=8.7583, available=8.7583, risk_pct=row["counterfactual_risk_pct"],
                              cost=CostModel(costs["taker_fee"], costs["entry_slippage"]), win_prob=.6,
                              leverage=cfg.LEVERAGE, max_margin_pct=cfg.MAX_MARGIN_PCT)
            segmented = {**AUTHORITY, "evaluation_fidelity": row["evaluation_fidelity"],
                         "counterfactual": True, "frontier": result.as_dict()}
            assert segmented["population"] == "HARD_GATE_SHADOW"
            assert segmented["execution_effect"] == "NONE"
            assert abs(result.risk_budget - float(row["risk_budget"])) < 1e-9
        if peer == "494":
            assert row["bbo_cost_observation"]["bbo_valid"] is True
            assert row["bbo_cost_observation"]["population"] == "HARD_GATE_SHADOW"
            assert row["bbo_cost_observation"]["costs"] is not None
            assert any("[COST_SHADOW_BBO]" in line and "population=HARD_GATE_SHADOW" in line for line in captured)
            assert not any("nexus_bbo_cost_shadow_v1" in call for call in sql.calls)
        assert all("hard_gate_shadow_" in call for call in sql.calls)
        print("COMPATIBILITY_PROOF=" + json.dumps({"peer_pr": int(peer), "status": "PASS",
              "population": row["population"], "place_order": private_calls.count("place_order"), "private_exchange_mutation": len(private_calls),
              "decision_effect": "NONE", "execution_effect": "NONE"}))
    finally:
        shadow.log.removeHandler(capture)
        sql.conn.close()


if __name__ == "__main__":
    asyncio.run(probe(sys.argv[1]))
