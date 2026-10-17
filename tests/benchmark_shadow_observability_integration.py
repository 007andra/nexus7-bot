"""Measure composed observer cost with 25 cached symbols and real NEXUS.

Only strategy candidates are supplied synthetically; pullback math, MIN_ORDER,
NEXUS, BBO snapshot/model, terminal/matrix and SQLite persistence are real.
No deadlines or runtime performance policy changes are introduced.
"""
import asyncio
from contextlib import ExitStack
import json
import os
import statistics
import time
from unittest.mock import patch

os.environ['EXCHANGE'] = 'binance'
os.environ['PAPER_TRADE'] = 'true'
os.environ['NEXUS_TELEGRAM'] = 'false'

from tests.run_offline import install_network_guard
from tests import test_hard_gate_shadow_scan as f
from tests.test_shadow_observability_integration import FLAGS, seed
from bot import hard_gate_shadow_scan as shadow, candidate_terminal_telemetry as terminal
from bot.hard_gate_shadow_context import observation
from bot.runtime_bootstrap import install


async def main():
    install_network_guard()
    with patch.dict(os.environ, FLAGS):
        install()
    obj = f.engine([f'ASSET{i}USDT' for i in range(25)])
    sql = f.DB()
    totals = dict(bbo=0., terminal=0., db=0.)
    sample = [0]
    def analyze(symbol, k15, *args, **kwargs):
        sig = f.signal(symbol, kind='PULLBACK')
        sig._bgx_formation_bucket = sample[0]
        observation().pullback = 'PASS' if f.pullback._pullback_metrics(k15, 'LONG')['ok'] else 'BLOCKED'
        return sig
    def timed(name, original):
        def call(*args, **kwargs):
            start = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                totals[name] += (time.perf_counter()-start)*1000
        return call
    def db_timed(original):
        async def call(*args, **kwargs):
            start = time.perf_counter()
            try:
                return await original(*args, **kwargs)
            finally:
                totals['db'] += (time.perf_counter()-start)*1000
        return call
    emit = shadow._emit
    def measured_emit(tag, values):
        if tag == 'CANDIDATE_TERMINAL':
            return timed('terminal', emit)(tag, values)
        return emit(tag, values)
    runs = []
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, FLAGS))
        stack.enter_context(patch.object(f.cfg, 'MAX_DRAWDOWN', .17))
        stack.enter_context(patch.object(f.Analyzer, 'analyze_mtf', side_effect=analyze))
        stack.enter_context(patch.object(shadow, '_bbo_observation', side_effect=timed('bbo', shadow._bbo_observation)))
        stack.enter_context(patch.object(terminal, 'shadow_record', side_effect=timed('terminal', terminal.shadow_record)))
        stack.enter_context(patch.object(shadow, '_emit', side_effect=measured_emit))
        stack.enter_context(patch.object(sql, '_exec', side_effect=db_timed(sql._exec)))
        stack.enter_context(patch.object(sql, '_fetchall', side_effect=db_timed(sql._fetchall)))
        for run in range(21):
            sample[0] = run + 1
            seed(obj.client, obj.viable_symbols)
            for key in totals:
                totals[key] = 0.
            start = time.perf_counter()
            out = await shadow.scan(obj, db=sql)
            elapsed = (time.perf_counter()-start)*1000
            assert out['summary']['nexus_evaluated'] == 25
            assert all(r['bbo_cost_observation']['bbo_valid'] for r in out['candidates'])
            assert out['summary']['additional_rest_calls_per_scan'] == 0
            if run:
                runs.append(dict(scan=elapsed, **totals))
        sample[0] += 1
        seed(obj.client, obj.viable_symbols)
        scans = await asyncio.gather(*(shadow.scan(obj, db=sql) for _ in range(5)))
        completed = sum(x is not None for x in scans)
        assert completed == 1
    result = dict(symbols=25, samples=20,
                  mean_scan_ms=round(statistics.mean(x['scan'] for x in runs), 3),
                  p95_scan_ms=round(sorted(x['scan'] for x in runs)[18], 3),
                  bbo_observer_overhead_ms=round(statistics.mean(x['bbo'] for x in runs), 3),
                  terminal_telemetry_overhead_ms=round(statistics.mean(x['terminal'] for x in runs), 3),
                  db_shadow_overhead_ms=round(statistics.mean(x['db'] for x in runs), 3),
                  additional_rest_calls=0, overlapping_scans=completed-1,
                  skipped_previous_scan_running=5-completed, nexus_evaluations=500)
    print('COMPOSED_BENCHMARK='+json.dumps(result, sort_keys=True))
    sql.conn.close()


if __name__ == '__main__':
    asyncio.run(main())
