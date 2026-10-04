"""#492 + #494 + #496 together: real observers, one NEXUS, no execution."""
import asyncio
from copy import deepcopy
import json
import logging
import os
import time
from unittest.mock import patch

from tests import test_hard_gate_shadow_scan as f
from bot import bbo_cost_shadow_runtime as bbo, binance_bbo_feed as feed
from bot import candidate_terminal_telemetry as terminal, min_order_feasibility_matrix as matrix
from bot import hard_gate_shadow_scan as shadow
from bot.hard_gate_shadow_context import AUTHORITY, observation, scope

REAL_DECIDE = f.nexus_ai.decide

FLAGS = {**f.ENV, "NEXUS_BBO_COST_SHADOW": "true", "NEXUS_BBO_COST_SHADOW_PERSIST": "true",
         "CANDIDATE_TERMINAL_TELEMETRY": "true", "MIN_ORDER_FEASIBILITY_MATRIX": "true"}


def seed(client, symbols):
    cache = feed.BBOCache(symbols)
    gen = cache.begin_generation()
    now = int(time.time() * 1000)
    for symbol in symbols:
        cache.ingest({"e": "bookTicker", "s": symbol, "b": "100", "a": "100.01",
                      "B": "50", "A": "50", "u": 123, "T": now, "E": now},
                     generation=gen, received_wall_ms=now, received_mono_ns=time.monotonic_ns())
    bbo._register(client, cache, None)
    return cache


class ComposedProof(f.Proof):
    def setUp(self):
        # Full production bootstrap, with all observers opted in before install.
        with patch.dict(os.environ, FLAGS):
            from bot.runtime_bootstrap import install
            install()
        super().setUp()
        self.stack.enter_context(patch.dict(os.environ, FLAGS))
        self.book = seed(self.e.client, self.e.viable_symbols)
        self.lines = []
        lines = self.lines
        class Capture(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())
        self.capture = Capture()
        shadow.log.addHandler(self.capture)
        self.addCleanup(shadow.log.removeHandler, self.capture)

    def count(self, tag):
        return sum(line.startswith('[' + tag + ']') for line in self.lines)

    async def test_composed_candidate_single_decision_all_observers_and_outcome(self):
        # Simulate strategy candidate; production pullback mathematics remains real.
        def analyze(symbol, k15, *args, **kwargs):
            sig = f.signal(symbol, kind='PULLBACK')
            metrics = f.pullback._pullback_metrics(k15, sig.direction)
            self.assertTrue(metrics['ok'])
            observation().pullback = 'PASS'
            return sig
        self.analyzer.side_effect = analyze
        live = terminal.CandidateTerminalCollector()
        live.active = terminal.CandidateTrace('live-existing', 'SOLUSDT', 'LONG', '80')
        before = deepcopy(vars(live.active))
        out = await self.run_scan()
        row = out['candidates'][0]
        self.assertTrue(row['pullback_pass'])
        self.assertTrue(row['counterfactual'])
        self.assertTrue(row['nexus_allowed'])
        self.assertTrue(row['bbo_cost_observation']['shadow_allowed'])
        self.assertEqual(self.nexus.call_count, 1)
        for tag in ('SHADOW_CANDIDATE_WHILE_LIVE_BLOCKED', 'COST_SHADOW_BBO',
                    'CANDIDATE_TERMINAL', 'MIN_ORDER_FEASIBILITY_MATRIX'):
            lines = [x for x in self.lines if x.startswith('['+tag+']')]
            self.assertEqual(len(lines), 1, tag)
            self.assertIn('candidate_id='+row['candidate_id'], lines[0])
            for key in ('population=HARD_GATE_SHADOW', 'shadow_only=true', 'live_eligible=false',
                        'live_block_reason=DRAWDOWN_HARD_GATE', 'decision_effect=NONE', 'execution_effect=NONE'):
                self.assertIn(key, lines[0])
            self.assertEqual(live.observe(lines[0]), [])
        self.assertEqual(vars(live.active), before)
        self.assertEqual(row['champion_challenger']['population'], 'HARD_GATE_SHADOW')
        start = int((row['captured_epoch'] + 899) // 900) * 900
        self.e.client.cache['15'] = [{'ts': (start+i*900)*1000, 'c': 101, 'h': 102, 'l': 99} for i in range(16)]
        with patch.object(shadow.time, 'time', return_value=start+14401):
            await shadow.observe_outcomes(self.e, self.db)
        outcomes = self.db.conn.execute('SELECT payload FROM hard_gate_shadow_outcomes_v1').fetchall()
        self.assertEqual(len(outcomes), 2)
        for item in outcomes:
            outcome = json.loads(item[0])
            self.assertEqual(outcome['outcome'], 'OBSERVED')
            self.assertEqual(outcome['population'], 'HARD_GATE_SHADOW')
        self.assertFalse(any('nexus_bbo_cost_shadow_v1' in sql for sql in self.db.calls))
        self.assert_isolated()
        print('COMPOSED_EXECUTION_COUNTERS='+json.dumps({k: v.call_count for k,v in self.counters.items()}, sort_keys=True))

    async def test_bbo_exception_preserves_candidate_terminal_and_gate(self):
        with patch.object(bbo, 'build_record', side_effect=RuntimeError('BBO fault')):
            row = (await self.run_scan())['candidates'][0]
        self.assertEqual(row['bbo_cost_observation']['status'], 'SHADOW_DATA_UNAVAILABLE')
        self.assertEqual(self.count('CANDIDATE_TERMINAL'), 1)
        self.assert_isolated()

    async def test_snapshot_exception_never_changes_gate(self):
        with patch.object(bbo, 'snapshot_for_research', side_effect=RuntimeError('cache fault')):
            self.assertEqual(len((await self.run_scan())['candidates']), 1)
        self.assert_isolated()

    async def test_telemetry_exception_preserves_persistence(self):
        with patch.object(shadow.log, 'info', side_effect=RuntimeError('sink fault')), \
             patch.object(terminal, 'shadow_record', side_effect=RuntimeError('terminal fault')):
            out = await self.run_scan()
        self.assertEqual(len(out['candidates']), 1)
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM hard_gate_shadow_candidates_v1').fetchone()[0], 1)
        self.assert_isolated()

    async def test_db_failure_preserves_observers_and_gate(self):
        with patch.object(self.db, '_exec', side_effect=RuntimeError('database fault')):
            self.assertEqual(len((await self.run_scan())['candidates']), 1)
        self.assertEqual(self.count('CANDIDATE_TERMINAL'), 1)
        self.assert_isolated()

    async def test_bbo_reconnect_during_nexus_invalidates_old_generation(self):
        def reconnect(**kwargs):
            self.book.begin_generation()
            return f.decision()
        self.nexus.side_effect = reconnect
        row = (await self.run_scan())['candidates'][0]
        self.assertFalse(row['bbo_cost_observation']['bbo_valid'])
        self.assertIsNone(row['bbo_cost_observation']['shadow_allowed'])
        self.assertEqual(self.nexus.call_count, 1)
        self.assert_isolated()

    async def test_gate_clears_during_bbo_no_terminal_or_persisted_candidate(self):
        real = bbo.build_record
        def cleared(*args, **kwargs):
            self.e.risk.drawdown = .01
            return real(*args, **kwargs)
        with patch.object(bbo, 'build_record', side_effect=cleared):
            out = await self.run_scan()
        self.assertEqual(out['summary']['shadow_scan_aborted_reason'], 'LIVE_HARD_GATE_CLEARED')
        self.assertFalse(out['candidates'])
        self.assertEqual(self.count('CANDIDATE_TERMINAL'), 0)
        self.assertFalse(any('INSERT' in sql for sql in self.db.calls))
        self.assertEqual(sum(x.call_count for x in self.counters.values()), 0)

    async def test_duplicate_and_restart_reuse_decision_no_observer_reemit(self):
        sig = f.signal()
        sig._bgx_formation_bucket = 123
        self.analyzer.side_effect = lambda *a, **k: sig
        first = await self.run_scan()
        second = await self.run_scan()
        restarted = f.engine()
        third = await shadow.scan(restarted, db=self.db)
        self.assertEqual(self.nexus.call_count, 1)
        for result in (second, third):
            self.assertEqual(result['summary']['nexus_evaluated'], 0)
            self.assertEqual(result['candidates'][0], json.loads(json.dumps(first['candidates'][0], default=str)))
        for tag in ('CANDIDATE_TERMINAL', 'COST_SHADOW_BBO', 'SHADOW_CANDIDATE_WHILE_LIVE_BLOCKED'):
            self.assertEqual(self.count(tag), 1)
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM hard_gate_shadow_candidates_v1').fetchone()[0], 1)
        self.assertFalse(restarted.positions)
        self.assert_isolated()

    async def test_defaults_false_no_observers_cache_or_db_work(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(shadow.enabled())
            self.assertFalse(bbo.enabled())
            self.assertFalse(bbo.persist_enabled())
            self.assertFalse(terminal.enabled())
            self.assertFalse(matrix.enabled())
            with patch.object(bbo, 'snapshot_for_research') as snapshot:
                self.assertIsNone(await shadow.scan_if_enabled(self.e))
            snapshot.assert_not_called()
            logger = logging.getLogger('integration-disabled-proof')
            handlers = list(logger.handlers)
            self.assertIsNone(terminal.install(logger))
            self.assertEqual(logger.handlers, handlers)
            with patch.object(matrix, 'build_matrix') as build:
                matrix.log_once(self.e, {}, logger)
            build.assert_not_called()
        self.assertFalse(self.db.calls)
        self.assert_isolated()

    async def test_matrix_live_path_suppressed_inside_research_scope(self):
        with scope(), patch.object(matrix, 'build_matrix') as build:
            matrix.log_once(self.e, {}, shadow.log)
        build.assert_not_called()
        self.assert_isolated()

    async def test_missing_and_stale_bbo_do_not_change_decision(self):
        for view in (None, feed.BBOView('SOLUSDT', False, 'STALE', 'CONN_SILENT', None, 9000)):
            sql = f.DB()
            try:
                with patch.object(bbo, 'snapshot_for_research', return_value=view):
                    row = (await shadow.scan(self.e, db=sql))['candidates'][0]
                self.assertTrue(row['nexus_allowed'])
                self.assertFalse(row['bbo_cost_observation']['bbo_valid'])
            finally:
                sql.conn.close()
        self.assert_isolated()

    async def test_real_nexus_called_once_with_passive_observers(self):
        self.nexus.side_effect = REAL_DECIDE
        row = (await self.run_scan())['candidates'][0]
        self.assertEqual(self.nexus.call_count, 1)
        self.assertTrue(row['nexus_called'])
        self.assertTrue(row['bbo_cost_observation']['bbo_valid'])
        self.assertEqual(self.count('CANDIDATE_TERMINAL'), 1)
        self.assert_isolated()


    async def test_persist_false_keeps_bbo_in_memory_but_not_candidate_json(self):
        flags = {**FLAGS, "NEXUS_BBO_COST_SHADOW_PERSIST": "false"}
        with patch.dict(os.environ, flags):
            row = (await self.run_scan())['candidates'][0]
        self.assertIn('bbo_cost_observation', row)
        self.assertTrue(row['bbo_cost_observation']['bbo_valid'])
        raw = self.db.conn.execute(
            'SELECT payload FROM hard_gate_shadow_candidates_v1 WHERE candidate_id=?',
            (row['candidate_id'],)
        ).fetchone()[0]
        stored = json.loads(raw)
        self.assertNotIn('bbo_cost_observation', stored)
        self.assertFalse(any('nexus_bbo_cost_shadow_v1' in sql for sql in self.db.calls))
        self.assert_isolated()


    async def test_persist_false_db_readback_telemetry_proves_bbo_absent(self):
        flags = {**FLAGS, "NEXUS_BBO_COST_SHADOW_PERSIST": "false"}
        shadow._PERSISTENCE_LOGGED.clear()
        with patch.dict(os.environ, flags), patch.object(shadow.time, "time", return_value=1791138001):
            first = await self.run_scan()
            second = await self.run_scan()
        self.assertEqual(first['candidates'][0]['candidate_id'], second['candidates'][0]['candidate_id'])
        proof = [line for line in self.lines if line.startswith('[HARD_GATE_SHADOW_PERSISTENCE]')]
        self.assertEqual(len(proof), 1)
        self.assertIn('persist_enabled=false', proof[0])
        self.assertIn('stored_bbo_present=false', proof[0])
        self.assertIn('proof_source=DB_READBACK_DEDUPE', proof[0])
        self.assert_isolated()

    async def test_persist_false_redacts_legacy_bbo_on_reuse_without_rewriting_history(self):
        flags = {**FLAGS, "NEXUS_BBO_COST_SHADOW_PERSIST": "false"}
        with patch.dict(os.environ, FLAGS):
            first = (await self.run_scan())['candidates'][0]
        raw_before = self.db.conn.execute(
            'SELECT payload FROM hard_gate_shadow_candidates_v1 WHERE candidate_id=?',
            (first['candidate_id'],)
        ).fetchone()[0]
        self.assertIn('bbo_cost_observation', json.loads(raw_before))
        restarted = f.engine()
        with patch.dict(os.environ, flags):
            reused = await shadow.scan(restarted, db=self.db)
        self.assertNotIn('bbo_cost_observation', reused['candidates'][0])
        raw_after = self.db.conn.execute(
            'SELECT payload FROM hard_gate_shadow_candidates_v1 WHERE candidate_id=?',
            (first['candidate_id'],)
        ).fetchone()[0]
        self.assertEqual(raw_after, raw_before)
        self.assertFalse(restarted.positions)
