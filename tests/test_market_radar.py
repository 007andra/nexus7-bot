"""Market Radar: observability-only Telegram panel of the monitored universe."""
import asyncio
import logging
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot import market_radar as mr
from bot.config import cfg

ROOT = Path(__file__).resolve().parents[1]


class _Clock:
    def __init__(self, t=1_790_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class _Log:
    def __init__(self):
        self.lines = []

    def __getattr__(self, _name):
        def emit(msg, *args, **_kw):
            self.lines.append(msg % args if args else msg)
        return emit


# Exact shapes of the canonical runtime log lines (strategy/engine/hardenings).
def _hold(sym, score):
    return (f"[{sym}] Score={score}/100 < 60 → HOLD | 4H:50 1H:55 15M:58 "
            f"| ADX=20 CI=50 RSI=48 vol=1.10x regime=TRENDING_UP")


def _signal(sym, direction, score):
    return f"[{sym}] ✅ SINAL {direction} score={score}/100 RR=2.0 entry=PULLBACK | 4H:70"


def _ai(sym, direction, decision, source="nexus_ai"):
    return (f"[AI_DECISION] symbol={sym} side={direction} decision={decision} "
            f"approved={decision == 'APPROVE'} decision_source={source} score=70 "
            f"confidence=0.8 ts=1 reason=x")


class RadarStateTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.radar = mr.MarketRadar(clock=self.clock)

    def _row(self, symbol, rows):
        return next(r for r in rows if r.symbol == symbol)

    def test_all_states_and_directions(self):
        feed = [
            "⛔ [BTCUSDT] CANDLES INSUFICIENTES: 4h=3/10 1h=15/15 15m=20/20",
            "⛔ [ETHUSDT] REGIME RANGING no 4H — estratégia é trend-follow, só opera TRENDING_UP/DOWN",
            "⛔ [ATOMUSDT] 4H/1H NÃO ALINHADOS (bull4h=True bear4h=False bull1h=False bear1h=True) → HOLD",
            _hold("BNBUSDT", 58),
            "⛔ [ADAUSDT] BLOQUEIO volume: 0.30x < 0.40x — score era 64",
            _signal("PEOPLEUSDT", "LONG", 63),
            "[PULLBACK_CONFIRMATION] setup_id=s symbol=PEOPLEUSDT side=LONG result=BLOCKED reason=votes",
            _signal("DOTUSDT", "LONG", 67),
            _ai("DOTUSDT", "LONG", "APPROVE"),
            _signal("SOLUSDT", "SHORT", 66),
            _ai("SOLUSDT", "SHORT", "REJECT"),
            _signal("XRPUSDT", "LONG", 65),
            _ai("XRPUSDT", "LONG", "APPROVE"),
            "[BINANCE_CROSS_STRESS] symbol=XRPUSDT result=BLOCK reason=x mode=LIVE",
            _signal("LINKUSDT", "SHORT", 61),
            _ai("LINKUSDT", "SHORT", "REJECT", source="timeout"),
        ]
        for line in feed:
            self.radar.observe(line)
        rows = self.radar.snapshot(
            ["BTCUSDT", "ETHUSDT", "ATOMUSDT", "BNBUSDT", "ADAUSDT", "PEOPLEUSDT",
             "DOTUSDT", "SOLUSDT", "XRPUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT"],
            open_symbols=["LTCUSDT"], stale_s=900)
        expect = {
            "BTCUSDT": (mr.ERROR, None, None),
            "ETHUSDT": (mr.HOLD, None, None),
            "ATOMUSDT": (mr.MTF_VETO, None, None),
            "BNBUSDT": (mr.HOLD, 58, None),
            "ADAUSDT": (mr.HOLD, 64, None),
            "PEOPLEUSDT": (mr.WAIT, 63, "LONG"),
            "DOTUSDT": (mr.NEXUS_APPROVE, 67, "LONG"),
            "SOLUSDT": (mr.NEXUS_REJECT, 66, "SHORT"),
            "XRPUSDT": (mr.RISK_BLOCK, 65, "LONG"),
            "LINKUSDT": (mr.ERROR, 61, "SHORT"),
            "AVAXUSDT": (mr.SCANNING, None, None),
            "LTCUSDT": (mr.OPEN, None, None),
        }
        for sym, (state, score, direction) in expect.items():
            r = self._row(sym, rows)
            self.assertEqual((r.state, r.score, r.direction), (state, score, direction), sym)
        self.assertEqual(self.radar.parse_errors, 0)

    def test_nexus_wait_is_not_reported_as_reject(self):
        self.radar.observe(_signal("SUIUSDT", "LONG", 62))
        self.radar.observe("[NEXUS_SCORE_DECOMP] symbol=SUIUSDT decision=Decision.WAIT final=55.00 x")
        self.radar.observe(_ai("SUIUSDT", "LONG", "REJECT"))
        r = self.radar.snapshot(["SUIUSDT"], stale_s=900)[0]
        self.assertEqual((r.state, r.score, r.direction, r.reason), (mr.WAIT, 62, "LONG", "NEXUS WAIT"))

    def test_pullback_block_without_prior_signal_has_no_invented_score(self):
        self.radar.observe("[PULLBACK_CONFIRMATION] setup_id=s symbol=OPUSDT side=SHORT result=BLOCKED reason=x")
        r = self.radar.snapshot(["OPUSDT"], stale_s=900)[0]
        self.assertEqual((r.state, r.score, r.direction), (mr.WAIT, None, "SHORT"))

    def test_expiry_never_shows_old_score_as_current(self):
        self.radar.observe(_hold("BNBUSDT", 58))
        self.clock.t += 901
        r = self.radar.snapshot(["BNBUSDT"], stale_s=900)[0]
        self.assertEqual((r.state, r.score, r.direction), (mr.SCANNING, None, None))
        self.assertEqual(r.reason, "sem dado recente")
        self.radar.observe(_hold("BNBUSDT", 59))
        self.assertEqual(self.radar.snapshot(["BNBUSDT"], stale_s=900)[0].score, 59)

    def test_open_position_with_stale_entry_shows_no_score(self):
        self.radar.observe(_signal("DOTUSDT", "LONG", 67))
        self.clock.t += 5000
        r = self.radar.snapshot(["DOTUSDT"], open_symbols=["DOTUSDT"], stale_s=900)[0]
        self.assertEqual((r.state, r.score), (mr.OPEN, None))

    def test_garbage_never_raises_or_creates_rows(self):
        for junk in ("", None, 123, "[AI_DECISION] symbol=", "⛔ [x] SEM", object()):
            self.radar.observe(junk)
        self.assertEqual(self.radar.snapshot([], stale_s=900), [])

    def test_global_pause_note_and_its_expiry(self):
        self.radar.observe("⏸️ Scan pulado: posições=0/2 drawdown=10.4%/10% ready=True stop_diário=False")
        self.assertIn("Scan pulado", self.radar.global_note(stale_s=900))
        self.clock.t += 901
        self.assertIsNone(self.radar.global_note(stale_s=900))


class OrderingAndRenderTests(unittest.TestCase):
    def _e(self, sym, score, state=mr.HOLD, direction=None, t=0.0):
        return mr.RadarEntry(sym, state, score, direction, "r", t)

    def test_sort_desc_ties_and_na_last(self):
        rows = mr.sort_rows([
            self._e("AAAUSDT", None, mr.SCANNING),
            self._e("BBBUSDT", 60),
            self._e("CCCUSDT", 67, mr.NEXUS_APPROVE, "LONG"),
            self._e("DDDUSDT", 60, mr.WAIT, "SHORT"),
            self._e("EEEUSDT", 60),
            self._e("FFFUSDT", None, mr.MTF_VETO),
        ])
        self.assertEqual([r.symbol for r in rows],
                         ["CCCUSDT", "DDDUSDT", "BBBUSDT", "EEEUSDT", "FFFUSDT", "AAAUSDT"])

    def test_render_25_symbols_under_telegram_limit(self):
        self.assertEqual(len(cfg.SYMBOLS), 25)
        radar = mr.MarketRadar(clock=_Clock())
        for i, sym in enumerate(cfg.SYMBOLS[:20]):
            radar.observe(_hold(sym, 40 + i))
        rows = radar.snapshot(cfg.SYMBOLS, open_symbols=["ETCUSDT"], stale_s=900)
        text = mr.render(rows, now=radar._clock(), min_score=55, open_count=1)
        self.assertEqual(len(rows), 25)
        self.assertLess(len(text), mr.TELEGRAM_MAX_CHARS)
        for sym in cfg.SYMBOLS:
            self.assertIn(sym, text)
        self.assertIn("Monitorados: *25*", text)
        self.assertIn("Acima do score mínimo (55): *5*", text)  # 55..59 (>= min)
        self.assertIn("Posições abertas: *1*", text)
        self.assertIn("N/A", text)
        self.assertEqual(text.count("```"), 2)
        lines = text.split("```")[1].strip().splitlines()[1:]
        self.assertTrue(lines[0].strip().startswith("1. UNIUSDT"))  # SYMBOLS[19] has score 59
        open_row = next(l for l in lines if "ETCUSDT" in l)
        self.assertTrue(open_row.rstrip().endswith("—"))  # no fabricated age

    def test_render_truncates_to_limit(self):
        rows = [self._e(f"S{i:05d}USDT", i % 100) for i in range(600)]
        text = mr.render(rows, now=0.0, min_score=60, open_count=0)
        self.assertLessEqual(len(text), mr.TELEGRAM_MAX_CHARS)
        self.assertIn("… (truncado)", text)
        self.assertEqual(text.count("```"), 2)

    def test_na_and_dash_for_unscored_rows(self):
        text = mr.render([self._e("ATOMUSDT", None, mr.MTF_VETO)], now=10.0, min_score=60, open_count=0)
        row = text.split("```")[1].strip().splitlines()[1]
        self.assertIn("N/A", row)
        self.assertIn("—", row)
        self.assertIn("MTF_VETO", row)


class SendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_failure_is_swallowed(self):
        async def broken(_text):
            raise RuntimeError("telegram down token=SECRET")
        log = _Log()
        engine = SimpleNamespace(positions={})
        ok = await mr.send_once(engine, log, broken, mr.MarketRadar(clock=_Clock()))
        self.assertFalse(ok)
        self.assertTrue(any("send_failed error=RuntimeError" in l for l in log.lines))
        self.assertFalse(any("SECRET" in l for l in log.lines))

    async def test_send_uses_configured_symbols_and_positions(self):
        sent = []

        async def fake(text):
            sent.append(text)
        engine = SimpleNamespace(positions={"BTCUSDT": object()})
        self.assertTrue(await mr.send_once(engine, _Log(), fake, mr.MarketRadar(clock=_Clock())))
        self.assertIn("Monitorados: *25*", sent[0])
        self.assertIn("OPEN", sent[0])

    async def test_loop_survives_failures_and_cancels_cleanly(self):
        calls = []

        async def flaky(text):
            calls.append(text)
            raise RuntimeError("x")
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"MARKET_RADAR_FIRST_DELAY_S": "30",
                                     "MARKET_RADAR_INTERVAL_S": "300"}), \
                patch.object(mr.asyncio, "sleep", new=_fast_sleep):
            task = asyncio.ensure_future(mr._loop(SimpleNamespace(positions={}), _Log(), flaky,
                                                  mr.MarketRadar(clock=_Clock())))
            while len(calls) < 3:
                await _real_sleep(0)
            await mr.cancel(task)
        self.assertTrue(task.cancelled())
        self.assertGreaterEqual(len(calls), 3)

    async def test_disabled_does_not_start(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"MARKET_RADAR_ENABLED": "false"}):
            self.assertIsNone(mr.start(SimpleNamespace(), _Log()))
        await mr.cancel(None)


_real_sleep = asyncio.sleep


async def _fast_sleep(_delay, *a, **k):
    await _real_sleep(0)


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_observers_and_snapshots(self):
        radar = mr.MarketRadar()
        symbols = list(cfg.SYMBOLS)
        errors = []

        def writer(offset):
            for i in range(300):
                sym = symbols[(i + offset) % 25]
                radar.observe(_hold(sym, (i + offset) % 100))
                radar.observe(_signal(sym, "LONG", 70))

        def reader():
            for _ in range(300):
                try:
                    rows = radar.snapshot(symbols, stale_s=900)
                    assert len(rows) == 25
                    mr.render(rows, now=radar._clock(), min_score=60, open_count=0)
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(exc)

        threads = [threading.Thread(target=writer, args=(k,)) for k in range(6)]
        threads += [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(radar.parse_errors, 0)


class ObservabilityOnlyContractTests(unittest.TestCase):
    def test_handler_is_passive_and_never_raises(self):
        logger = logging.getLogger("market_radar_contract_test")
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        mr.install(logger)
        mr.install(logger)  # idempotent
        self.assertEqual(sum(isinstance(h, mr._RadarHandler) for h in logger.handlers), 1)
        engine = SimpleNamespace(positions={"BTCUSDT": 1}, risk=SimpleNamespace(balance=10.0))
        before = (dict(engine.positions), engine.risk.balance)
        logger.info(_signal("DOTUSDT", "LONG", 67))
        logger.info("%s", object())
        mr.build_message(engine)
        self.assertEqual((dict(engine.positions), engine.risk.balance), before)

    def test_module_has_no_execution_dependencies(self):
        src = (ROOT / "bot" / "market_radar.py").read_text(encoding="utf-8")
        for forbidden in ("place_order", "set_position_stops", "risk.size", "minimum_base_quantity",
                          "execution_allowed", "from bot import engine", "import bot.engine",
                          "nexus_ai", "risk_manager", "cancel_all_orders", "_post(", "LEVERAGE"):
            self.assertNotIn(forbidden, src, forbidden)

    def test_started_only_by_reviewed_run_owner(self):
        src = (ROOT / "bot" / "operator_runtime_policy.py").read_text(encoding="utf-8")
        self.assertIn("radar = market_radar.start(self, log)", src)
        self.assertIn("await market_radar.cancel(radar)", src)


if __name__ == "__main__":
    unittest.main()


class SourceLogShapeAnchorTests(unittest.TestCase):
    """The radar parses existing log lines; fail loudly if their format drifts."""

    # ── cash-flow-adjusted account block ─────────────────────────────────
    _RECONCILED = (
        "[ADJUSTED_EQUITY] status=RECONCILED equity=5.8410 performance_hwm=6.4258 "
        "trading_drawdown=9.10% unadjusted_drawdown=N/A drawdown_limit=10.00% "
        "external_flows_applied=1 external_flows_net=-4.0000 pending_flows=0 "
        "pending_net=0.0000 execution_effect=NONE"
    )
    _PENDING = (
        "[ADJUSTED_EQUITY] status=UNRECONCILED equity=5.8410 performance_hwm=10.8262 "
        "trading_drawdown=N/A unadjusted_drawdown=46.05% drawdown_limit=10.00% "
        "external_flows_applied=0 external_flows_net=0.0000 pending_flows=3 "
        "pending_net=-4.0000 execution_effect=BLOCK_NEW_ENTRY"
    )

    def _account_text(self, line, advance=0.0):
        clock = _Clock()
        radar = mr.MarketRadar(clock=clock)
        radar.observe(line)
        clock.t += advance
        return mr.render(
            [], now=clock.t, min_score=60, open_count=0,
            account=radar.account(stale_s=900.0), show_account=True,
        )

    def test_account_block_shows_trading_drawdown_after_reconciled_withdrawal(self):
        text = self._account_text(self._RECONCILED)
        self.assertIn("Equity: *5.8410 USDT*", text)
        self.assertIn("Performance HWM: *6.4258*", text)
        self.assertIn("Fluxos externos: *1 reconciliado(s), líquido -4.0000 USDT*", text)
        self.assertIn("Drawdown de trading: *9.10%* (limite 10.00%)", text)
        self.assertNotIn("46", text)
        self.assertNotIn("10.8262", text)

    def test_account_block_pending_flow_never_labels_gross_as_trading(self):
        text = self._account_text(self._PENDING)
        self.assertIn("Drawdown de trading: *N/A*", text)
        self.assertIn("Fluxos pendentes: *3, líquido -4.0000 USDT*", text)
        self.assertIn("entradas bloqueadas", text)
        self.assertNotIn("46.05", text)

    def test_account_block_expires(self):
        text = self._account_text(self._RECONCILED, advance=901.0)
        self.assertIn("sem dado recente de drawdown ajustado", text)
        self.assertNotIn("9.10%", text)

    def test_adjusted_equity_log_format_matches_ledger(self):
        source = (ROOT / "bot" / "cash_flow_ledger.py").read_text(encoding="utf-8")
        self.assertIn('"[ADJUSTED_EQUITY] status=%s equity=%.4f performance_hwm=%s trading_drawdown=%s "', source)
        for key in ("drawdown_limit=%s", "external_flows_applied=%d", "external_flows_net=%.4f",
                    "pending_flows=%d", "pending_net=%.4f"):
            self.assertIn(key, source)

    def test_canonical_log_formats_still_present(self):
        read = lambda p: (ROOT / "bot" / p).read_text(encoding="utf-8")  # noqa: E731
        strategy, engine = read("strategy.py"), read("engine.py")
        for fragment in (
            'f"⛔ [{symbol}] CANDLES INSUFICIENTES: "',
            'f"⛔ [{symbol}] REGIME {regime} no 4H',
            'f"⛔ [{symbol}] 4H/1H NÃO ALINHADOS "',
            'f"[{symbol}] Score={combined}/100 < {min_score} → HOLD "',
            'f"⛔ [{symbol}] BLOQUEIO volume: {s15[\'vol_r\']:.2f}x < 0.40x "',
            'f"[{symbol}] ✅ SINAL {direction} score={combined}/100 "',
        ):
            self.assertIn(fragment, strategy, fragment)
        self.assertIn('f"[AI_DECISION] symbol={sig.symbol} side={sig.direction} "', engine)
        self.assertIn("decision_source={decision_source}", engine)
        self.assertIn("symbol=%s side=%s result=BLOCKED", read("pullback_confirmation_hardening.py"))
        self.assertIn("[NEXUS_SCORE_DECOMP] symbol=%s decision=%s", read("nexus_decision_consistency.py"))
        self.assertIn("[BINANCE_CROSS_STRESS] symbol=%s result=BLOCK", read("binance_cross_portfolio_stress.py"))
