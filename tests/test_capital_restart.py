"""Operator capital restart after a drawdown beyond the limit.

Runs the real LIVE refresh path against an in-memory key/value store and the
Binance-shaped fake client of the cash-flow tests. Production shape: HWM
22.7987, loss to 5.31768526, full outflow, then fresh capital.
"""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

from bot import cash_flow_ledger as cfl
from bot import database as db
from bot import drawdown_persistence as ddp
from bot import hwm_namespace
from bot import pilot_live_runtime as plr
from tests.test_cash_flow_adjusted_drawdown import LOG, T0, FakeBinance, new_risk

PEAK = 22.7986938551
LOW = 5.31768526


class CapitalRestartTests(unittest.IsolatedAsyncioTestCase):
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

    async def refresh(self, engine):
        engine._pilot_last_capital_flow_check = 0.0
        engine.risk._cash_flow_block_hold = None
        engine.client.advance()
        return await plr._refresh_account(engine, LOG)

    async def peak(self):
        raw = await db.load_key_value(ddp.DURABLE_EQUITY_PEAK_KEY)
        return None if raw is None else float(raw)

    async def ledger(self):
        return json.loads(await db.load_key_value(cfl.LEDGER_KEY))

    def drawdown(self, engine):
        return float(getattr(engine.risk, "_legacy", engine.risk).drawdown)

    async def emptied_then_redeposited(self, deposit=60.0):
        client = FakeBinance(PEAK)
        engine = SimpleNamespace(client=client, risk=new_risk(PEAK))
        await self.refresh(engine)
        client.income("REALIZED_PNL", LOW - PEAK)
        await self.refresh(engine)
        out_tran = client.income("TRANSFER", -LOW)
        client.advance(49 * 3600 * 1000)
        in_tran = client.income("TRANSFER", deposit)
        return client, engine, out_tran, in_tran

    async def restart(self, client, tran_ids, **over):
        kwargs = dict(
            rows=client.rows, restart_id="restart-1", tran_ids=tran_ids,
            reason="recapitalised after total loss", evidence_ref="binance income rows",
            apply=True, now_ms=client.now, account=await client.get_account_state(),
            rows_cover_until_ms=client.now,
        )
        kwargs.update(over)
        return await cfl.capital_restart(**kwargs)

    async def test_deposit_alone_keeps_entries_blocked(self):
        client, engine, _, _ = await self.emptied_then_redeposited()
        with self.assertRaises(RuntimeError):
            await self.refresh(engine)
        self.assertAlmostEqual(await self.peak(), PEAK)

    async def test_restart_opens_a_new_baseline_and_preserves_history(self):
        client, engine, out_tran, in_tran = await self.emptied_then_redeposited(60.0)
        applied_before = len((await self.ledger())["applied"])

        out = await self.restart(client, [out_tran, in_tran])

        self.assertEqual(out["status"], "APPLIED")
        record = out["record"]
        self.assertEqual(record["method"], "CAPITAL_RESTART")
        self.assertAlmostEqual(record["previous_hwm"], PEAK)
        self.assertAlmostEqual(record["pre_flow_equity"], LOW)
        self.assertAlmostEqual(record["historical_drawdown_at_restart"], 1 - LOW / PEAK)
        self.assertAlmostEqual(record["adjusted_hwm"], 60.0)
        self.assertTrue(record["history_preserved"])
        self.assertAlmostEqual(await self.peak(), 60.0)
        doc = await self.ledger()
        self.assertEqual(len(doc["applied"]), applied_before + 1)
        self.assertEqual(doc["pending"], [])
        history = json.loads(await db.load_key_value(cfl.CAPITAL_RESTART_HISTORY_KEY))
        self.assertEqual(len(history["restarts"]), 1)
        self.assertAlmostEqual(history["restarts"][0]["previous_hwm"], PEAK)
        provenance = json.loads(await db.load_key_value(hwm_namespace.provenance_key()))
        self.assertEqual(provenance["reason"], "capital_restart")
        self.assertAlmostEqual(provenance["old_peak"], PEAK)
        snapshot = await cfl.ledger_snapshot()
        self.assertEqual(len(snapshot["capital_restarts"]), 1)

        # The running process picks it up: entries allowed again on the new capital.
        await self.refresh(engine)
        self.assertAlmostEqual(self.drawdown(engine), 0.0)
        self.assertTrue(plr._entry_drawdown_allows(engine, LOG))

    async def test_the_drawdown_limit_still_applies_to_the_new_capital(self):
        client, engine, out_tran, in_tran = await self.emptied_then_redeposited(60.0)
        await self.restart(client, [out_tran, in_tran])
        await self.refresh(engine)
        client.income("REALIZED_PNL", -0.35 * 60.0)
        await self.refresh(engine)
        self.assertAlmostEqual(self.drawdown(engine), 0.35)
        self.assertFalse(plr._entry_drawdown_allows(engine, LOG))
        self.assertAlmostEqual(await self.peak(), 60.0)

    async def test_restart_without_the_earlier_outflow_still_proves_an_empty_account(self):
        client, engine, _, in_tran = await self.emptied_then_redeposited(60.0)
        out = await self.restart(client, [in_tran])
        self.assertAlmostEqual(out["record"]["pre_flow_equity"], 0.0)
        self.assertAlmostEqual(out["record"]["historical_drawdown_at_restart"], 1.0)
        self.assertAlmostEqual(await self.peak(), 60.0)

    async def test_dry_run_and_idempotency(self):
        client, _, out_tran, in_tran = await self.emptied_then_redeposited(60.0)
        dry = await self.restart(client, [out_tran, in_tran], apply=False)
        self.assertEqual(dry["status"], "DRY_RUN")
        self.assertAlmostEqual(await self.peak(), PEAK)
        self.assertIsNone(await db.load_key_value(cfl.CAPITAL_RESTART_HISTORY_KEY))
        await self.restart(client, [out_tran, in_tran])
        again = await self.restart(client, [out_tran, in_tran])
        self.assertEqual(again["status"], "ALREADY_APPLIED")
        with self.assertRaisesRegex(ValueError, "already"):
            await self.restart(client, [in_tran], restart_id="restart-2")

    async def test_refused_when_history_does_not_block(self):
        client = FakeBinance(10.0)
        engine = SimpleNamespace(client=client, risk=new_risk(10.0))
        await self.refresh(engine)
        client.income("REALIZED_PNL", -0.5)  # 5% < limit
        await self.refresh(engine)
        client.advance(49 * 3600 * 1000)
        tran = client.income("TRANSFER", 50.0)
        with self.assertRaisesRegex(ValueError, "below the limit"):
            await self.restart(client, [tran])
        self.assertAlmostEqual(await self.peak(), 10.0)

    async def test_refused_without_flat_account_or_complete_evidence(self):
        client, _, out_tran, in_tran = await self.emptied_then_redeposited(60.0)
        with self.assertRaisesRegex(ValueError, "unverified"):
            await self.restart(client, [out_tran, in_tran], account=None)
        with self.assertRaisesRegex(ValueError, "unverified"):
            await self.restart(client, [out_tran, in_tran], rows_cover_until_ms=client.now - 1)
        client.position_margin = 1.0
        with self.assertRaisesRegex(ValueError, "unverified"):
            await self.restart(client, [out_tran, in_tran])
        client.position_margin = 0.0
        client.income("REALIZED_PNL", 0.5)  # performance after the flows
        with self.assertRaisesRegex(ValueError, "unverified"):
            await self.restart(client, [out_tran, in_tran])
        self.assertAlmostEqual(await self.peak(), PEAK)

    async def test_refused_when_a_pending_flow_is_left_out_or_net_is_not_new_capital(self):
        client, engine, out_tran, in_tran = await self.emptied_then_redeposited(60.0)
        with self.assertRaises(RuntimeError):
            await self.refresh(engine)  # the runtime records the deposit as pending
        self.assertEqual(len((await self.ledger())["pending"]), 1)
        with self.assertRaisesRegex(ValueError, "every pending flow"):
            await self.restart(client, [out_tran])
        with self.assertRaisesRegex(ValueError, "not found"):
            await self.restart(client, ["999999"])
        self.assertAlmostEqual(await self.peak(), PEAK)

    async def test_typed_amounts_are_never_accepted(self):
        client, _, out_tran, in_tran = await self.emptied_then_redeposited(60.0)
        account = dict(await client.get_account_state())
        account["equity"] = account["walletBalance"] = 600.0
        with self.assertRaises(ValueError):
            await self.restart(client, [out_tran, in_tran], account=account)
        self.assertAlmostEqual(await self.peak(), PEAK)

    async def test_readonly_audit_accepts_a_restart_record(self):
        from bot import hwm_cashflow_readonly_audit_v1 as audit

        client, _, out_tran, in_tran = await self.emptied_then_redeposited(60.0)
        await self.restart(client, [out_tran, in_tran])
        keys = [audit.LEDGER_KEY, audit.CURSOR_KEY, audit.WALLET_KEY,
                ddp.DURABLE_EQUITY_PEAK_KEY, hwm_namespace.provenance_key()]
        values = {k: await db.load_key_value(k) for k in keys}
        out = audit.review_values(values, peak_key=ddp.DURABLE_EQUITY_PEAK_KEY,
                                  provenance_key=hwm_namespace.provenance_key())
        for code in ("TWR_EVIDENCE_INCOMPLETE", "TWR_REBASE_MISMATCH",
                     "FLOW_DRAWDOWN_RECORD_MISMATCH", "HWM_PROVENANCE_REASON_UNRECOGNIZED",
                     "HWM_PROVENANCE_PEAK_MISMATCH"):
            self.assertNotIn(code, out.get("errors", []))


class CapitalRestartCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_requires_exact_confirm_token(self):
        from bot import cash_flow_admin

        client = SimpleNamespace(
            _now_ms=lambda: T0,
            get_account_state=AsyncMock(return_value={"equity": 60.0, "walletBalance": 60.0}),
            close=AsyncMock(),
        )
        base = ["restart-capital", "--restart-id", "r1", "--tran-ids", "7,8",
                "--evidence-ref", "e", "--reason", "r"]
        with patch("bot.database.init", AsyncMock()), patch("bot.database.close", AsyncMock()), \
             patch.object(cash_flow_admin, "_client", AsyncMock(return_value=client)), \
             patch("bot.binance_accounting_evidence.collect_income", AsyncMock(return_value=[])), \
             patch.object(cash_flow_admin.ledger, "capital_restart",
                          AsyncMock(return_value={"status": "DRY_RUN"})) as restart:
            with self.assertRaises(SystemExit):
                await cash_flow_admin._run(cash_flow_admin._parser().parse_args(
                    base + ["--apply", "--confirm", "ATTEST:r1"]))
            restart.assert_not_awaited()
            out = await cash_flow_admin._run(cash_flow_admin._parser().parse_args(base))
            self.assertEqual(out["status"], "DRY_RUN")
            self.assertFalse(restart.await_args.kwargs["apply"])
            self.assertEqual(restart.await_args.kwargs["tran_ids"], ["7", "8"])
            await cash_flow_admin._run(cash_flow_admin._parser().parse_args(
                base + ["--apply", "--confirm", "RESTART-CAPITAL:r1"]))
            self.assertTrue(restart.await_args.kwargs["apply"])


if __name__ == "__main__":
    unittest.main()
