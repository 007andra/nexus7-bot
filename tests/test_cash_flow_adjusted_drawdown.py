"""Cash-flow-adjusted (TWR) drawdown: deposits/withdrawals are never bot PnL.

Every scenario runs the real LIVE refresh path (``pilot_live_runtime._refresh_account``
→ capital-flow reconciliation → durable HWM) against an in-memory SQLite key/value
store and a Binance-shaped fake client (``/fapi/v1/income`` + ``/fapi/v3/account``).
"""
import json
import logging
import math
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import aiosqlite

from bot import cash_flow_ledger as cfl
from bot import database as db
from bot import drawdown_persistence as ddp
from bot import pilot_live_runtime as plr
from bot.atomic_key_value import CompareAndSwapConflict, save_key_values_atomic_cas
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.risk import RiskManager

LOG = logging.getLogger("test_cash_flow_adjusted_drawdown")
T0 = 1_790_000_000_000


class FakeBinance:
    """Minimal Binance USD-M read surface used by the reconciliation."""

    def __init__(self, wallet: float):
        self.wallet = float(wallet)
        self.unrealized = 0.0
        self.position_margin = 0.0
        self.multi_assets = False
        self.now = T0
        self.rows: list[dict] = []
        self.hidden: list[dict] = []
        self.fail = False
        self._tran = 5000

    def _listen_key_request(self):  # Binance capability marker
        return None

    def _now_ms(self):
        return self.now

    async def _get(self, endpoint, params=None, auth=False):
        if self.fail:
            raise RuntimeError("HTTP 503 service unavailable")
        assert endpoint == "/fapi/v1/income", endpoint
        start, end = int(params["startTime"]), int(params["endTime"])
        return [dict(r) for r in self.rows if start <= int(r["time"]) <= end]

    async def get_account_state(self):
        if self.fail:
            raise RuntimeError("HTTP 503 service unavailable")
        equity = self.wallet + self.unrealized
        return {
            "equity": equity,
            "available": equity,
            "available_source": "availableBalance",
            "walletBalance": self.wallet,
            "unrealisedPNL": self.unrealized,
            "positionMargin": self.position_margin,
            "multiAssetsMargin": self.multi_assets,
        }

    def advance(self, ms: int = 120_000):
        self.now += ms

    def income(self, kind: str, amount: float, *, visible: bool = True, asset: str = "USDT") -> str:
        self.now += 1_000
        self._tran += 1
        row = {
            "symbol": "" if kind == "TRANSFER" else "BTCUSDT",
            "incomeType": kind,
            "income": f"{amount:.8f}",
            "asset": asset,
            "info": "",
            "time": self.now,
            "tranId": self._tran,
            "tradeId": "",
        }
        (self.rows if visible else self.hidden).append(row)
        if asset == "USDT":
            self.wallet += amount
        self.now += 1_000
        return str(self._tran)

    def reveal(self):
        self.rows.extend(self.hidden)
        self.rows.sort(key=lambda r: r["time"])
        self.hidden = []


def new_risk(equity: float):
    legacy = RiskManager()
    legacy.init(equity)
    return ProfessionalRiskAdapter(legacy)


class CashFlowAdjustedDrawdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig = (db._conn, db._is_pg)
        db._conn = await aiosqlite.connect(":memory:")
        db._is_pg = False
        await db._create_tables()
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("LIVE_RISK_OVERRIDE_APPROVED", None)

    async def asyncTearDown(self):
        self._env.stop()
        await db._conn.close()
        db._conn, db._is_pg = self._orig

    # ── helpers ──────────────────────────────────────────────────────────────
    def engine(self, client, equity=None):
        return SimpleNamespace(client=client, risk=new_risk(equity or client.wallet))

    async def refresh(self, engine):
        engine._pilot_last_capital_flow_check = 0.0  # force the reconciliation pass
        engine.risk._cash_flow_block_hold = None  # simulate the block hold elapsing
        engine.client.advance()
        return await plr._refresh_account(engine, LOG)

    async def boot(self, wallet=10.0):
        client = FakeBinance(wallet)
        engine = self.engine(client)
        await self.refresh(engine)
        self.assertAlmostEqual(await self.peak(), wallet)
        return client, engine

    async def peak(self):
        raw = await db.load_key_value(ddp.DURABLE_EQUITY_PEAK_KEY)
        return None if raw is None else float(raw)

    async def ledger(self):
        raw = await db.load_key_value(cfl.LEDGER_KEY)
        return None if raw is None else json.loads(raw)

    def drawdown(self, engine):
        legacy = getattr(engine.risk, "_legacy", engine.risk)
        return float(legacy.drawdown)

    def entry_allowed(self, engine):
        return plr._entry_drawdown_allows(engine, LOG)

    async def assert_real_loss_blocks(self, client, engine):
        """A further real loss beyond the limit must block new entries."""
        client.income("REALIZED_PNL", -0.02 * client.wallet)
        await self.refresh(engine)
        self.assertGreater(self.drawdown(engine), 0.10)
        self.assertFalse(self.entry_allowed(engine))

    async def assert_blocked(self, engine, code):
        with self.assertRaisesRegex(RuntimeError, code):
            await self.refresh(engine)

    # 1 ────────────────────────────────────────────────────────────────────────
    async def test_01_loss_without_flow_is_ten_percent(self):
        client, engine = await self.boot(10.0)
        client.income("REALIZED_PNL", -1.0)
        await self.refresh(engine)
        self.assertAlmostEqual(await self.peak(), 10.0)
        self.assertAlmostEqual(self.drawdown(engine), 0.10)
        await self.assert_real_loss_blocks(client, engine)

    # 2 ────────────────────────────────────────────────────────────────────────
    async def test_02_withdrawal_of_four_adds_zero_drawdown(self):
        client, engine = await self.boot(10.0)
        client.income("TRANSFER", -4.0)
        await self.refresh(engine)
        self.assertAlmostEqual(await self.peak(), 6.0)
        self.assertAlmostEqual(self.drawdown(engine), 0.0)
        self.assertTrue(self.entry_allowed(engine))
        record = (await self.ledger())["applied"][0]
        self.assertEqual(record["direction"], "WITHDRAWAL")
        self.assertEqual(record["method"], "LEDGER_RECONSTRUCTED")
        self.assertAlmostEqual(record["pre_flow_equity"], 10.0)
        self.assertAlmostEqual(record["post_flow_equity"], 6.0)
        self.assertAlmostEqual(record["previous_hwm"], 10.0)
        self.assertAlmostEqual(record["adjusted_hwm"], 6.0)

    # 3 ────────────────────────────────────────────────────────────────────────
    async def test_03_loss_one_then_withdrawal_one_is_ten_percent_trading_drawdown(self):
        client, engine = await self.boot(10.0)
        client.income("REALIZED_PNL", -1.0)
        await self.refresh(engine)
        client.income("TRANSFER", -1.0)
        await self.refresh(engine)
        self.assertAlmostEqual(client.wallet, 8.0)
        self.assertAlmostEqual(await self.peak(), 10.0 * 8.0 / 9.0)
        self.assertAlmostEqual(self.drawdown(engine), 0.10)
        await self.assert_real_loss_blocks(client, engine)

    async def test_03b_withdrawal_first_then_loss_measures_loss_on_remaining_capital(self):
        client, engine = await self.boot(10.0)
        client.income("TRANSFER", -1.0)
        await self.refresh(engine)
        client.income("REALIZED_PNL", -1.0)
        await self.refresh(engine)
        # 1 USDT lost out of the 9 USDT that stayed invested: 11.11%, never 20%.
        self.assertAlmostEqual(self.drawdown(engine), 1.0 / 9.0)

    # 4 ────────────────────────────────────────────────────────────────────────
    async def test_04_deposit_is_not_profit_and_does_not_reduce_drawdown(self):
        client, engine = await self.boot(10.0)
        client.income("REALIZED_PNL", -1.0)
        await self.refresh(engine)
        client.income("TRANSFER", 3.0)
        await self.refresh(engine)
        self.assertAlmostEqual(client.wallet, 12.0)
        self.assertAlmostEqual(await self.peak(), 10.0 * 12.0 / 9.0)
        self.assertAlmostEqual(self.drawdown(engine), 0.10)  # unchanged by the deposit
        await self.assert_real_loss_blocks(client, engine)
        self.assertEqual((await self.ledger())["applied"][0]["direction"], "DEPOSIT")

    async def test_04b_deposit_never_creates_a_new_high(self):
        client, engine = await self.boot(10.0)
        client.income("TRANSFER", 5.0)
        await self.refresh(engine)
        self.assertAlmostEqual(await self.peak(), 15.0)
        client.income("REALIZED_PNL", -1.5)
        await self.refresh(engine)
        self.assertAlmostEqual(self.drawdown(engine), 0.10)

    # 5 ────────────────────────────────────────────────────────────────────────
    async def test_05_withdrawal_is_not_a_loss(self):
        client, engine = await self.boot(10.0)
        client.income("REALIZED_PNL", -0.5)
        await self.refresh(engine)
        self.assertAlmostEqual(self.drawdown(engine), 0.05)
        client.income("TRANSFER", -4.5)
        await self.refresh(engine)
        self.assertAlmostEqual(client.wallet, 5.0)
        self.assertAlmostEqual(self.drawdown(engine), 0.05)  # gross would be 50%
        self.assertTrue(self.entry_allowed(engine))

    # 6 ────────────────────────────────────────────────────────────────────────
    async def test_06_realized_loss_after_withdrawal_still_increases_and_blocks(self):
        client, engine = await self.boot(10.0)
        client.income("TRANSFER", -4.0)
        await self.refresh(engine)
        client.income("REALIZED_PNL", -0.6)
        await self.refresh(engine)
        self.assertAlmostEqual(self.drawdown(engine), 0.10)
        await self.assert_real_loss_blocks(client, engine)

    # 7 ────────────────────────────────────────────────────────────────────────
    async def test_07_fees_are_trading_cost(self):
        client, engine = await self.boot(10.0)
        client.income("COMMISSION", -0.1)
        await self.refresh(engine)
        self.assertAlmostEqual(self.drawdown(engine), 0.01)
        self.assertEqual((await self.ledger())["applied"], [])
        self.assertEqual(cfl.classify_income_type("COMMISSION"), "PERFORMANCE")
        self.assertEqual(cfl.classify_income_type("REALIZED_PNL"), "PERFORMANCE")

    # 8 ────────────────────────────────────────────────────────────────────────
    async def test_08_funding_is_trading_result(self):
        client, engine = await self.boot(10.0)
        client.income("FUNDING_FEE", -0.2)
        await self.refresh(engine)
        self.assertAlmostEqual(self.drawdown(engine), 0.02)
        self.assertEqual(cfl.classify_income_type("FUNDING_FEE"), "PERFORMANCE")
        self.assertEqual((await self.ledger())["pending"], [])

    # 9 ────────────────────────────────────────────────────────────────────────
    async def test_09_duplicate_flow_is_never_applied_twice(self):
        client, engine = await self.boot(10.0)
        tran = client.income("TRANSFER", -4.0)
        await self.refresh(engine)
        for _ in range(3):
            await self.refresh(engine)
        doc = await self.ledger()
        self.assertEqual(len(doc["applied"]), 1)
        self.assertAlmostEqual(await self.peak(), 6.0)
        with self.assertRaisesRegex(ValueError, "already applied"):
            await cfl.attest_flows(
                rows=client.rows, reconciliation_id="op-dup", tran_ids=[tran],
                pre_flow_equity=10.0, current_equity=6.0, reason="dup", evidence_ref="test",
                apply=True, now_ms=client.now,
            )
        self.assertAlmostEqual(await self.peak(), 6.0)

    # 10 ───────────────────────────────────────────────────────────────────────
    async def test_10_restart_preserves_adjustment(self):
        client, engine = await self.boot(10.0)
        client.income("TRANSFER", -4.0)
        await self.refresh(engine)
        restarted = self.engine(client, equity=6.0)
        await self.refresh(restarted)
        self.assertAlmostEqual(await self.peak(), 6.0)
        self.assertAlmostEqual(self.drawdown(restarted), 0.0)
        self.assertEqual(len((await self.ledger())["applied"]), 1)

    # 11 ───────────────────────────────────────────────────────────────────────
    async def test_11_out_of_order_ledger_row_blocks_until_evidence_arrives(self):
        client, engine = await self.boot(10.0)
        client.income("TRANSFER", -4.0, visible=False)  # wallet moved, row not yet visible
        await self.assert_blocked(engine, "BINANCE_UNRECONCILED_EQUITY_CHANGE")
        self.assertAlmostEqual(await self.peak(), 10.0)  # never assumed to be a withdrawal
        client.reveal()
        await self.refresh(engine)
        self.assertAlmostEqual(await self.peak(), 6.0)
        self.assertAlmostEqual(self.drawdown(engine), 0.0)

    async def test_11b_unexplained_drop_is_never_treated_as_withdrawal(self):
        client, engine = await self.boot(10.0)
        client.wallet = 6.0  # no ledger row at all
        for _ in range(3):
            await self.assert_blocked(engine, "BINANCE_UNRECONCILED_EQUITY_CHANGE")
        self.assertAlmostEqual(await self.peak(), 10.0)
        self.assertEqual((await self.ledger())["applied"], [])

    # 12 ───────────────────────────────────────────────────────────────────────
    async def test_12_binance_api_unavailable_fails_closed(self):
        client, engine = await self.boot(10.0)
        client.income("TRANSFER", -4.0)
        client.fail = True
        with self.assertRaises(RuntimeError):
            await self.refresh(engine)
        self.assertAlmostEqual(await self.peak(), 10.0)
        client.fail = False
        # income unavailable but account readable → still blocked
        original = client._get

        async def income_down(*a, **k):
            raise RuntimeError("HTTP 503")

        client._get = income_down
        await self.assert_blocked(engine, "BINANCE_CASH_FLOW_EVIDENCE_UNAVAILABLE")
        client._get = original
        await self.refresh(engine)
        self.assertAlmostEqual(await self.peak(), 6.0)

    # 13 ───────────────────────────────────────────────────────────────────────
    async def test_13_ambiguous_flow_fails_closed_and_stays_pending(self):
        client, engine = await self.boot(10.0)
        client.multi_assets = True  # pre-flow equity no longer provable from the ledger
        client.income("TRANSFER", -4.0)
        for _ in range(2):
            await self.assert_blocked(engine, "BINANCE_CAPITAL_FLOW_REBASE_UNCONFIRMED")
        doc = await self.ledger()
        self.assertEqual(len(doc["pending"]), 1)
        self.assertEqual(doc["applied"], [])
        self.assertAlmostEqual(await self.peak(), 10.0)

    async def test_13b_pending_flow_keeps_blocking_after_leaving_the_exchange_window(self):
        client, engine = await self.boot(10.0)
        client.position_margin = 1.0
        client.income("TRANSFER", -4.0)
        await self.assert_blocked(engine, "BINANCE_CAPITAL_FLOW_REBASE_UNCONFIRMED")
        client.position_margin = 0.0
        client.advance(cfl.DETECTION_WINDOW_MS + 3_600_000)
        await self.assert_blocked(engine, "BINANCE_CAPITAL_FLOW_REBASE_UNCONFIRMED")
        self.assertEqual(len((await self.ledger())["pending"]), 1)

    # 14 ───────────────────────────────────────────────────────────────────────
    async def test_14_open_position_plus_withdrawal_requires_operator_attestation(self):
        client, engine = await self.boot(10.0)
        client.position_margin = 2.0
        client.unrealized = -0.2
        tran = client.income("TRANSFER", -4.0)
        await self.assert_blocked(engine, "BINANCE_CAPITAL_FLOW_REBASE_UNCONFIRMED")
        # the position closes with a realized loss after the withdrawal
        client.position_margin = 0.0
        client.unrealized = 0.0
        client.income("REALIZED_PNL", -0.3)
        client.income("COMMISSION", -0.01)
        await self.assert_blocked(engine, "BINANCE_CAPITAL_FLOW_REBASE_UNCONFIRMED")

        dry = await cfl.attest_flows(
            rows=client.rows, reconciliation_id="op-2026-09-26-w1", tran_ids=[tran],
            pre_flow_equity=9.8, current_equity=client.wallet, reason="operator withdrawal",
            evidence_ref="PILOT_LIVE_BALANCE equity=9.8 before transfer", apply=False,
            now_ms=client.now,
        )
        self.assertEqual(dry["status"], "DRY_RUN")
        self.assertAlmostEqual(await self.peak(), 10.0)  # dry run changes nothing
        out = await cfl.attest_flows(
            rows=client.rows, reconciliation_id="op-2026-09-26-w1", tran_ids=[tran],
            pre_flow_equity=9.8, current_equity=client.wallet, reason="operator withdrawal",
            evidence_ref="PILOT_LIVE_BALANCE equity=9.8 before transfer", apply=True,
            now_ms=client.now,
        )
        self.assertEqual(out["status"], "APPLIED")
        record = out["record"]
        for field in (
            "reconciliation_id", "net_amount", "direction", "flow_time_first_ms",
            "pre_flow_equity", "previous_hwm", "adjusted_hwm", "reason", "evidence_ref",
        ):
            self.assertIn(field, record)
        self.assertAlmostEqual(record["net_amount"], -4.0)  # from the exchange row
        self.assertAlmostEqual(record["adjusted_hwm"], 10.0 * 5.8 / 9.8)
        again = await cfl.attest_flows(
            rows=client.rows, reconciliation_id="op-2026-09-26-w1", tran_ids=[tran],
            pre_flow_equity=9.8, current_equity=client.wallet, reason="operator withdrawal",
            evidence_ref="x", apply=True, now_ms=client.now,
        )
        self.assertEqual(again["status"], "ALREADY_APPLIED")

        # the running process picks the operator record up without restart
        await self.refresh(engine)
        expected_peak = 10.0 * 5.8 / 9.8
        self.assertAlmostEqual(self.drawdown(engine), 1.0 - client.wallet / expected_peak)
        # trading losses (0.2 unrealized before + 0.11 net at close) stay in the drawdown
        self.assertAlmostEqual(client.wallet, 5.69)
        self.assertAlmostEqual(self.drawdown(engine), 1.0 - 5.69 / (10.0 * 5.8 / 9.8))
        self.assertGreater(self.drawdown(engine), 1.0 - 9.8 / 10.0)

    # 15 ───────────────────────────────────────────────────────────────────────
    async def test_15_multiple_deposits_and_withdrawals(self):
        client, engine = await self.boot(10.0)
        client.income("TRANSFER", 5.0)
        client.income("TRANSFER", -3.0)
        client.income("TRANSFER", -2.0)
        await self.refresh(engine)
        self.assertAlmostEqual(await self.peak(), 10.0)
        self.assertAlmostEqual(self.drawdown(engine), 0.0)
        client.income("REALIZED_PNL", -1.0)
        await self.refresh(engine)
        client.income("TRANSFER", 1.0)
        await self.refresh(engine)
        client.income("TRANSFER", -5.0)
        await self.refresh(engine)
        self.assertAlmostEqual(client.wallet, 5.0)
        self.assertAlmostEqual(self.drawdown(engine), 0.10)
        self.assertEqual(len((await self.ledger())["applied"]), 3)

    # 16 ───────────────────────────────────────────────────────────────────────
    async def test_16_new_high_only_from_real_profit(self):
        client, engine = await self.boot(10.0)
        client.income("TRANSFER", -4.0)
        await self.refresh(engine)
        client.income("REALIZED_PNL", 1.0)
        await self.refresh(engine)
        self.assertAlmostEqual(await self.peak(), 7.0)
        client.income("REALIZED_PNL", -0.7)
        await self.refresh(engine)
        self.assertAlmostEqual(self.drawdown(engine), 0.10)
        await self.assert_real_loss_blocks(client, engine)

    # 17 ───────────────────────────────────────────────────────────────────────
    async def test_17_daily_pnl_is_trade_based_and_ignores_cash_flows(self):
        from bot.durable_daily_pnl import realized

        now = datetime.now(timezone.utc)
        trade = SimpleNamespace(
            symbol="BTCUSDT", direction="LONG", opened_at=now, closed_at=now,
            qty=1.0, entry=100.0, exit_price=99.0, pnl=-1.0,
        )
        stats = SimpleNamespace(trades=[trade], _durable_daily_pnl={})
        before = realized(stats, now)
        client, engine = await self.boot(10.0)
        client.income("TRANSFER", -4.0)
        await self.refresh(engine)
        self.assertEqual(realized(stats, now), before)
        self.assertEqual(before, -1.0)

    # production replay ───────────────────────────────────────────────────────
    async def test_production_replay_2026_09_26_withdrawal(self):
        """HWM 10.8262, equity 9.8410 → operator withdrawal of 4 USDT → 5.8410.

        Pre-existing v1 cursor (checkpoint-only detector), no ledger yet; two
        new transfers netting to zero followed by the -4 USDT withdrawal.
        """
        client = FakeBinance(9.8410)
        client.income("TRANSFER", 19.68204603)  # historical, checkpointed long ago
        client.wallet = 9.8410
        seen = [cfl.flow_identity(client.rows[0])]
        await db.save_key_value(
            cfl.SEEN_CURSOR_KEY,
            json.dumps({"version": 1, "seen": seen, "observed_at_ms": client.now}),
        )
        await db.save_key_value(ddp.DURABLE_EQUITY_PEAK_KEY, "10.8262")
        engine = self.engine(client, equity=9.8410)
        client.advance()
        client.income("TRANSFER", 1.25)
        client.income("TRANSFER", -1.25)
        client.income("TRANSFER", -4.0)
        self.assertAlmostEqual(client.wallet, 5.8410)
        gross = 1.0 - 5.8410 / 10.8262
        self.assertAlmostEqual(gross * 100.0, 46.05, places=2)

        with self.assertLogs("kakazito-trade", level="INFO") as logs:
            await self.refresh(engine)
        adjusted = 10.8262 * 5.8410 / 9.8410
        self.assertAlmostEqual(await self.peak(), adjusted, places=9)
        self.assertAlmostEqual(adjusted, 6.4258, places=4)
        dd = self.drawdown(engine)
        self.assertAlmostEqual(dd, 1.0 - 9.8410 / 10.8262, places=12)  # = pre-withdrawal 9.10%
        self.assertAlmostEqual(dd * 100.0, 9.10, places=2)
        record = (await self.ledger())["applied"][0]
        self.assertAlmostEqual(record["net_amount"], -4.0)
        self.assertAlmostEqual(record["pre_flow_equity"], 9.8410)
        self.assertEqual(len(record["identities"]), 3)
        text = "\n".join(logs.output)
        self.assertIn("[CASH_FLOW]", text)
        self.assertIn("[DRAWDOWN_RECONCILIATION] result=RECONCILED", text)
        self.assertIn("[ADJUSTED_EQUITY] status=RECONCILED", text)
        self.assertIn("trading_drawdown=9.10%", text)
        self.assertNotIn("46.0", text)

    # safety properties ───────────────────────────────────────────────────────
    async def test_block_is_held_without_hammering_the_income_endpoint(self):
        client, engine = await self.boot(10.0)
        client.position_margin = 1.0
        client.income("TRANSFER", -4.0)
        await self.assert_blocked(engine, "BINANCE_CAPITAL_FLOW_REBASE_UNCONFIRMED")
        calls = []
        original = client._get

        async def counting(*a, **k):
            calls.append(a)
            return await original(*a, **k)

        client._get = counting
        for _ in range(5):
            engine._pilot_last_capital_flow_check = 0.0
            with self.assertRaisesRegex(RuntimeError, "BINANCE_CAPITAL_FLOW_REBASE_UNCONFIRMED"):
                await plr._refresh_account(engine, LOG)
        self.assertEqual(calls, [])  # held: no income re-query while blocked
        self.assertAlmostEqual(await self.peak(), 10.0)

    def test_unrecorded_high_before_flow_counts_as_performance(self):
        record = cfl.build_record(
            flows=[{"identity": "1:1", "tran_id": "1", "income_type": "TRANSFER",
                    "amount": -4.0, "time_ms": 1}],
            pre_flow_equity=11.0, post_flow_equity=7.0, previous_hwm=10.0,
            current_equity=7.0, method="TEST", reason="r", evidence_ref="e", now_ms=2,
        )
        self.assertAlmostEqual(record["adjusted_hwm"], 7.0)
        self.assertAlmostEqual(record["trading_drawdown_after"], 0.0)

    async def test_cas_conflict_refuses_concurrent_ledger_overwrite(self):
        await db.save_key_value(cfl.LEDGER_KEY, "A")
        with self.assertRaises(CompareAndSwapConflict):
            await save_key_values_atomic_cas(
                [(cfl.LEDGER_KEY, "B")], expected={cfl.LEDGER_KEY: "stale"}, strict=True,
            )
        self.assertEqual(await db.load_key_value(cfl.LEDGER_KEY), "A")

    async def test_implausible_flow_ratio_fails_closed(self):
        client, engine = await self.boot(10.0)
        client.income("REALIZED_PNL", -9.9)  # near-total loss
        await self.refresh(engine)
        client.income("TRANSFER", 150.0)
        await self.assert_blocked(engine, "BINANCE_CAPITAL_FLOW_REBASE_UNCONFIRMED")
        self.assertEqual((await self.ledger())["applied"], [])

    async def test_attestation_requires_exchange_rows_and_never_accepts_typed_amounts(self):
        client, engine = await self.boot(10.0)
        with self.assertRaisesRegex(ValueError, "not found"):
            await cfl.attest_flows(
                rows=client.rows, reconciliation_id="op-x", tran_ids=["999999"],
                pre_flow_equity=10.0, current_equity=10.0, reason="r", evidence_ref="e",
                apply=True, now_ms=client.now,
            )
        pnl = client.income("REALIZED_PNL", -1.0)
        with self.assertRaisesRegex(ValueError, "not found"):
            await cfl.attest_flows(
                rows=client.rows, reconciliation_id="op-y", tran_ids=[pnl],
                pre_flow_equity=10.0, current_equity=9.0, reason="r", evidence_ref="e",
                apply=True, now_ms=client.now,
            )
        self.assertAlmostEqual(await self.peak(), 10.0)

    def test_twr_math_identities(self):
        # a flow alone never changes drawdown: 1 - E+/P' == 1 - E-/P
        for peak, pre, amount in ((10.0, 9.0, -4.0), (10.8262, 9.8410, -4.0), (50.0, 40.0, 25.0)):
            post = pre + amount
            adjusted = cfl.twr_adjusted_peak(peak, pre, post)
            self.assertTrue(math.isclose(
                cfl.trading_drawdown(post, adjusted), cfl.trading_drawdown(pre, peak), abs_tol=1e-12,
            ))
        with self.assertRaises(ValueError):
            cfl.twr_adjusted_peak(10.0, 0.001, 50.0)


if __name__ == "__main__":
    unittest.main()


class CashFlowAdminCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_requires_exact_confirm_token_and_defaults_to_dry_run(self):
        from unittest.mock import AsyncMock

        from bot import cash_flow_admin

        client = SimpleNamespace(
            _now_ms=lambda: T0,
            get_account_state=AsyncMock(return_value={"equity": 6.0, "walletBalance": 6.0}),
            close=AsyncMock(),
        )
        base = [
            "attest", "--reconciliation-id", "op-1", "--tran-ids", "7",
            "--pre-flow-equity", "10", "--evidence-ref", "e", "--reason", "r",
        ]
        with patch("bot.database.init", AsyncMock()), patch("bot.database.close", AsyncMock()), \
             patch.object(cash_flow_admin, "_client", AsyncMock(return_value=client)), \
             patch("bot.binance_accounting_evidence.collect_income", AsyncMock(return_value=[])), \
             patch.object(cash_flow_admin.ledger, "attest_flows", AsyncMock(return_value={"status": "DRY_RUN"})) as attest:
            with self.assertRaises(SystemExit):
                await cash_flow_admin._run(cash_flow_admin._parser().parse_args(base + ["--apply", "--confirm", "yes"]))
            attest.assert_not_awaited()
            out = await cash_flow_admin._run(cash_flow_admin._parser().parse_args(base))
            self.assertEqual(out["status"], "DRY_RUN")
            self.assertFalse(attest.await_args.kwargs["apply"])
            await cash_flow_admin._run(cash_flow_admin._parser().parse_args(
                base + ["--apply", "--confirm", "ATTEST:op-1"]))
            self.assertTrue(attest.await_args.kwargs["apply"])
