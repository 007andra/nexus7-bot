"""Real PostgreSQL proof for the operational risk epoch (30% period limit).

CI MUST provide TEST_POSTGRES_DSN (postgres:16) and run this module explicitly.
Every test uses an isolated namespace, writes only disposable keys and never
touches an exchange. The scenario mirrors production on 2026-10-08:
equity 5.3177 USDT, durable historical HWM 22.7986938551 USDT (drawdown
76.68%), two applied external cash flows, MAX_DRAWDOWN 30%.
"""
import asyncio
import json
import logging

NAMESPACE = None
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import database as db
from bot import drawdown_persistence, hwm_namespace, risk_epoch
from bot import cash_flow_ledger
from bot.professional_risk import CapitalState
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.risk import RiskManager

HISTORICAL_HWM = 22.7986938551
EQUITY = 5.31768526
EPOCH_ID = "BGX_EPOCH_30PCT_TEST_V1"
LEDGER_DOC = {
    "version": 1,
    "applied": [
        {"reconciliation_id": "auto-a", "identities": ["1:TRANSFER:111"], "net_amount": 5.0},
        {"reconciliation_id": "auto-b", "identities": ["2:TRANSFER:222"], "net_amount": 2.7808},
    ],
    "pending": [],
}


class _Orders:
    def __init__(self, pending=()):
        self.pending = list(pending)

    def pending_orders(self):
        return list(self.pending)


def _env(**extra):
    env = {
        risk_epoch.ENABLED_ENV: "true",
        risk_epoch.EPOCH_ID_ENV: EPOCH_ID,
        risk_epoch.MAX_DRAWDOWN_ENV: "0.30",
        "RAILWAY_GIT_COMMIT_SHA": "0258a2b154a7b32bc01a853e848c1e3fa7e81b1c",
    }
    env.update(extra)
    return env


@unittest.skipUnless(os.environ.get("TEST_POSTGRES_DSN"), "isolated PostgreSQL required")
class RiskEpochPostgresProof(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg

        self.dsn = os.environ["TEST_POSTGRES_DSN"]
        self.conn = await asyncpg.connect(self.dsn)
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS key_value "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)"
        )
        self.saved = (db._conn, db._is_pg, db._io_lock, db.DATABASE_URL)
        db._conn, db._is_pg, db._io_lock = self.conn, True, asyncio.Lock()
        db.DATABASE_URL = self.dsn
        # Production key derivation (no patching): the epoch shares the HWM
        # namespace but never its keys.
        global NAMESPACE
        NAMESPACE = hwm_namespace.hwm_namespace()
        self.hwm_key = drawdown_persistence.DURABLE_EQUITY_PEAK_KEY
        self.assertEqual(self.hwm_key, hwm_namespace.equity_peak_key())
        self.prov_key = hwm_namespace.provenance_key()
        self.cfg_patch = patch("bot.config.cfg.MAX_DRAWDOWN", 0.30)
        self.cfg_patch.start()
        await self._clean()
        # Production-shaped durable financial state that the epoch must never write.
        await self._put(self.hwm_key, format(HISTORICAL_HWM, ".17g"))
        await self._put(self.prov_key, json.dumps({"reason": "external_capital_flow_rebase"}))
        await self._put(cash_flow_ledger.LEDGER_KEY, json.dumps(LEDGER_DOC))
        self.financial_before = await self._financial_rows()

    async def asyncTearDown(self):
        await self._clean()
        self.cfg_patch.stop()
        db._conn, db._is_pg, db._io_lock, db.DATABASE_URL = self.saved
        await self.conn.close()

    async def _clean(self):
        await self.conn.execute(
            "DELETE FROM key_value WHERE key LIKE $1 OR key LIKE $2 OR key = $3 OR key = $4",
            f"%{NAMESPACE}%", "risk_epoch:v1:%", cash_flow_ledger.LEDGER_KEY,
            cash_flow_ledger.WALLET_BASELINE_KEY,
        )

    async def _put(self, key, value):
        await self.conn.execute(
            "INSERT INTO key_value (key,value,updated_at) VALUES ($1,$2,'t') "
            "ON CONFLICT (key) DO UPDATE SET value=$2", key, value,
        )

    async def _get(self, key):
        return await self.conn.fetchval("SELECT value FROM key_value WHERE key=$1", key)

    async def _financial_rows(self):
        return {
            key: await self._get(key)
            for key in (self.hwm_key, self.prov_key, cash_flow_ledger.LEDGER_KEY)
        }

    async def _engine(self, equity=EQUITY, *, positions=None, pending=()):
        """Fresh runtime-shaped engine whose historical HWM is restored from PostgreSQL."""
        risk = ProfessionalRiskAdapter(RiskManager())
        risk.init(equity)
        risk.update_capital(CapitalState(equity, equity))
        await drawdown_persistence.restore_update_real_account_peak(risk, equity, strict=True)
        return SimpleNamespace(
            risk=risk,
            paper_trade=False,
            positions=dict(positions or {}),
            orders=_Orders(pending),
            _pilot_account_equity=equity,
            _pilot_available_balance=equity,
        )

    async def _set_equity(self, engine, equity):
        engine.risk.update_capital(CapitalState(equity, equity))
        await drawdown_persistence.restore_update_real_account_peak(engine.risk, equity, strict=True)

    async def _record(self):
        raw = await self._get(risk_epoch.epoch_key(EPOCH_ID, NAMESPACE))
        return None if raw is None else json.loads(raw)

    # ── baseline ────────────────────────────────────────────────────────────

    async def test_baseline_is_verifiable_and_preserves_all_historical_accounting(self):
        engine = await self._engine()
        self.assertAlmostEqual(engine.risk.drawdown, 0.7667548284, places=9)
        with patch.dict(os.environ, _env(), clear=False):
            state = await risk_epoch.observe(engine)

        self.assertEqual(state["status"], risk_epoch.ACTIVE, state)
        self.assertEqual(state["reason"], "baseline_created")
        record = await self._record()
        self.assertEqual(record["start_equity"], EQUITY)
        self.assertEqual(record["epoch_drawdown_limit"], 0.30)
        self.assertEqual(record["historical_peak_equity_at_start"], HISTORICAL_HWM)
        self.assertAlmostEqual(record["historical_drawdown_at_start"], 0.7667548284, places=9)
        self.assertEqual(record["historical_drawdown_limit_at_start"], 0.30)
        self.assertEqual(record["cash_flow_applied_records"], 2)
        self.assertAlmostEqual(record["cash_flow_applied_net"], 7.7808)
        self.assertEqual(record["code_sha"], "0258a2b154a7b32bc01a853e848c1e3fa7e81b1c")
        self.assertEqual(record["baseline_digest"], risk_epoch.baseline_digest(record))

        # Historical drawdown is reported unchanged and separately from the epoch.
        self.assertAlmostEqual(state["historical_drawdown"], 0.7667548284, places=9)
        self.assertEqual(state["historical_peak_equity"], HISTORICAL_HWM)
        self.assertEqual(state["epoch_drawdown"], 0.0)
        self.assertEqual(state["epoch_drawdown_limit"], 0.30)
        self.assertAlmostEqual(state["epoch_floor_equity"], EQUITY * 0.70)
        # Byte-identical HWM, HWM provenance and cash-flow ledger.
        self.assertEqual(await self._financial_rows(), self.financial_before)

    async def test_no_baseline_without_flat_confirmed_account_and_settled_flows(self):
        cases = [
            ("account_not_flat", dict(positions={"ETHUSDT": object()})),
            ("pending_orders", dict(pending=[object()])),
        ]
        for reason, kwargs in cases:
            engine = await self._engine(**kwargs)
            with patch.dict(os.environ, _env(), clear=False):
                state = await risk_epoch.observe(engine)
            self.assertEqual(state["status"], risk_epoch.PENDING_BASELINE)
            self.assertIn(reason, state["reason"])
            self.assertIsNone(await self._record())

        engine = await self._engine()
        engine.risk.invalidate_capital()
        engine.risk.balance_confirmed = False
        with patch.dict(os.environ, _env(), clear=False):
            state = await risk_epoch.observe(engine)
        self.assertEqual(state["status"], risk_epoch.PENDING_BASELINE)
        self.assertIn("capital_unconfirmed", state["reason"])

        pending = dict(LEDGER_DOC, pending=[{"identity": "3:TRANSFER:333", "amount": 1.0}])
        await self._put(cash_flow_ledger.LEDGER_KEY, json.dumps(pending))
        engine = await self._engine()
        with patch.dict(os.environ, _env(), clear=False):
            state = await risk_epoch.observe(engine)
        self.assertEqual(state["status"], risk_epoch.PENDING_BASELINE)
        self.assertIn("pending_external_flows", state["reason"])
        self.assertIsNone(await self._record())

    # ── restart / integrity ────────────────────────────────────────────────

    async def test_restart_reuses_same_baseline_and_historical_hwm(self):
        first = await self._engine()
        with patch.dict(os.environ, _env(), clear=False):
            created = await risk_epoch.observe(first)
            await self._set_equity(first, 5.60)
            await risk_epoch.observe(first)
        before = await self._record()

        # Simulated process restart: brand new engine/risk objects, same DB.
        restarted = await self._engine(equity=5.50)
        self.assertEqual(restarted.risk.peak_balance, HISTORICAL_HWM)
        with patch.dict(os.environ, _env(RAILWAY_GIT_COMMIT_SHA="ffffffffffffffffffffffffffffffffffffffff"),
                        clear=False):
            state = await risk_epoch.observe(restarted)
        after = await self._record()

        self.assertEqual(state["status"], risk_epoch.ACTIVE)
        self.assertEqual(after["started_at"], before["started_at"])
        self.assertEqual(after["baseline_digest"], created["baseline_digest"])
        self.assertEqual(after["code_sha"], "0258a2b154a7b32bc01a853e848c1e3fa7e81b1c")
        self.assertEqual(after["epoch_peak_equity"], 5.60)
        self.assertAlmostEqual(state["epoch_drawdown"], (5.60 - 5.50) / 5.60)
        self.assertAlmostEqual(state["historical_drawdown"], 1 - 5.50 / HISTORICAL_HWM)
        self.assertEqual(await self._get(self.hwm_key), self.financial_before[self.hwm_key])

    async def test_tampered_baseline_fails_closed(self):
        engine = await self._engine()
        with patch.dict(os.environ, _env(), clear=False):
            await risk_epoch.observe(engine)
            record = await self._record()
            record["start_equity"] = 50.0
            await self._put(risk_epoch.epoch_key(EPOCH_ID, NAMESPACE), json.dumps(record))
            state = await risk_epoch.observe(engine)
            blocked, _ = risk_epoch.blocks_new_entries(state)
        self.assertEqual(state["status"], risk_epoch.INVALID)
        self.assertEqual(state["reason"], "baseline_digest_mismatch")
        self.assertTrue(blocked)

    async def test_limit_is_immutable_and_cannot_be_loosened_by_env(self):
        engine = await self._engine()
        with patch.dict(os.environ, _env(), clear=False):
            await risk_epoch.observe(engine)
        with patch.dict(os.environ, _env(**{risk_epoch.MAX_DRAWDOWN_ENV: "0.25"}), clear=False):
            state = await risk_epoch.observe(engine)
        self.assertEqual(state["status"], risk_epoch.INVALID)
        self.assertEqual(state["reason"], "epoch_limit_changed")
        with patch.dict(os.environ, _env(**{risk_epoch.MAX_DRAWDOWN_ENV: "0.40"}), clear=False):
            state = await risk_epoch.observe(engine)
            blocked, _ = risk_epoch.blocks_new_entries(state)
        self.assertEqual(state["status"], risk_epoch.CONFIG_INVALID)
        self.assertEqual(state["reason"], "invalid_epoch_limit")
        self.assertTrue(blocked)

    # ── epoch drawdown / breach ────────────────────────────────────────────

    async def test_epoch_drawdown_is_separate_and_breach_is_sticky_across_restart(self):
        engine = await self._engine()
        with patch.dict(os.environ, _env(), clear=False):
            await risk_epoch.observe(engine)
            await self._set_equity(engine, 6.00)          # new epoch peak
            state = await risk_epoch.observe(engine)
            self.assertEqual(state["epoch_peak_equity"], 6.00)
            await self._set_equity(engine, 4.30)          # 28.33% below 6.00
            state = await risk_epoch.observe(engine)
            self.assertEqual(state["status"], risk_epoch.ACTIVE)
            self.assertAlmostEqual(state["epoch_drawdown"], (6.0 - 4.3) / 6.0)
            await self._set_equity(engine, 4.20)          # 30.0% -> breach
            state = await risk_epoch.observe(engine)
            self.assertEqual(state["status"], risk_epoch.BREACHED)
            self.assertTrue(risk_epoch.blocks_new_entries(state)[0])

            # Equity recovers and the process restarts: the breach persists.
            restarted = await self._engine(equity=5.90)
            state = await risk_epoch.observe(restarted)
        self.assertEqual(state["status"], risk_epoch.BREACHED)
        record = await self._record()
        self.assertTrue(record["breached"])
        self.assertAlmostEqual(record["breach_drawdown"], 0.30)
        # The historical HWM was never lowered by any of this.
        self.assertEqual(await self._get(self.hwm_key), self.financial_before[self.hwm_key])

    async def test_external_cash_flow_after_baseline_blocks_epoch(self):
        engine = await self._engine()
        with patch.dict(os.environ, _env(), clear=False):
            await risk_epoch.observe(engine)
            changed = dict(LEDGER_DOC)
            changed["applied"] = LEDGER_DOC["applied"] + [
                {"reconciliation_id": "auto-c", "identities": ["9:TRANSFER:999"], "net_amount": 10.0}
            ]
            await self._put(cash_flow_ledger.LEDGER_KEY, json.dumps(changed))
            state = await risk_epoch.observe(engine)
            blocked, _ = risk_epoch.blocks_new_entries(state)
        self.assertEqual(state["status"], risk_epoch.FLOW_CHANGED)
        self.assertTrue(blocked)

    # ── concurrency and epoch chain ────────────────────────────────────────

    async def test_concurrent_creation_writes_exactly_one_baseline(self):
        engines = [await self._engine() for _ in range(4)]
        # Every CAS opens its own PostgreSQL session, so the racing creators
        # contend on real advisory locks and row checks, as replicas would.
        with patch.dict(os.environ, _env(), clear=False):
            results = await asyncio.gather(*(risk_epoch.observe(e) for e in engines))
        statuses = sorted(r["status"] for r in results)
        self.assertIn(risk_epoch.ACTIVE, statuses)
        rows = await self.conn.fetch(
            "SELECT key FROM key_value WHERE key LIKE $1", f"risk_epoch:v1:{NAMESPACE}:epoch:%"
        )
        self.assertEqual(len(rows), 1)
        index = json.loads(await self._get(risk_epoch.index_key(NAMESPACE)))
        self.assertEqual(index["epochs"], [EPOCH_ID])

    async def test_new_epoch_after_breach_requires_explicit_ack_and_keeps_history(self):
        engine = await self._engine()
        with patch.dict(os.environ, _env(), clear=False):
            await risk_epoch.observe(engine)
            await self._set_equity(engine, 3.70)
            breached = await risk_epoch.observe(engine)
        self.assertEqual(breached["status"], risk_epoch.BREACHED)
        old_record = await self._record()

        second_id = "BGX_EPOCH_30PCT_TEST_V2"
        with patch.dict(os.environ, _env(**{risk_epoch.EPOCH_ID_ENV: second_id}), clear=False):
            refused = await risk_epoch.observe(engine)
        self.assertEqual(refused["status"], risk_epoch.PENDING_BASELINE)
        self.assertEqual(refused["reason"], "previous_epoch_supersede_ack_missing")

        with patch.dict(os.environ, _env(**{
            risk_epoch.EPOCH_ID_ENV: second_id,
            risk_epoch.SUPERSEDE_ACK_ENV: EPOCH_ID,
        }), clear=False):
            created = await risk_epoch.observe(engine)
        self.assertEqual(created["status"], risk_epoch.ACTIVE)
        second = json.loads(await self._get(risk_epoch.epoch_key(second_id, NAMESPACE)))
        self.assertEqual(second["previous_epoch_id"], EPOCH_ID)
        self.assertEqual(second["previous_epoch_status"], risk_epoch.BREACHED)
        self.assertAlmostEqual(second["previous_epoch_drawdown"], old_record["breach_drawdown"])
        # The breached epoch keeps its baseline and breach evidence verbatim
        # and only gains the formal closure fields.
        closed = await self._record()
        closure = {"closed", "closed_at", "closed_by_epoch_id", "closing_status"}
        self.assertEqual({k: v for k, v in closed.items() if k not in closure},
                         {k: v for k, v in old_record.items() if k not in closure})
        self.assertTrue(closed["closed"])
        self.assertEqual(closed["closed_by_epoch_id"], second_id)
        self.assertEqual(closed["closing_status"], risk_epoch.BREACHED)
        index = json.loads(await self._get(risk_epoch.index_key(NAMESPACE)))
        self.assertEqual(index["epochs"], [EPOCH_ID, second_id])

        # Re-using an id from the chain is refused.
        await self.conn.execute("DELETE FROM key_value WHERE key=$1",
                                risk_epoch.epoch_key(EPOCH_ID, NAMESPACE))
        with patch.dict(os.environ, _env(), clear=False):
            reused = await risk_epoch.observe(engine)
        self.assertEqual(reused["status"], risk_epoch.INVALID)
        self.assertEqual(reused["reason"], "epoch_id_reused")

    async def test_sub_limit_loss_is_durable_across_restart_and_into_successor(self):
        engine = await self._engine()
        with patch.dict(os.environ, _env(), clear=False):
            await risk_epoch.observe(engine)
            await self._set_equity(engine, 4.00)   # -24.78%, peak unchanged
            state = await risk_epoch.observe(engine)
        self.assertEqual(state["status"], risk_epoch.ACTIVE)
        expected_dd = (EQUITY - 4.00) / EQUITY
        record = await self._record()
        self.assertEqual(record["last_equity"], 4.00)
        self.assertAlmostEqual(record["max_epoch_drawdown"], expected_dd)
        self.assertEqual(record["epoch_peak_equity"], EQUITY)

        # Restart at a partial recovery: the worst sub-limit loss stays on record.
        restarted = await self._engine(equity=4.50)
        with patch.dict(os.environ, _env(), clear=False):
            state = await risk_epoch.observe(restarted)
        record = await self._record()
        self.assertAlmostEqual(state["epoch_drawdown"], (EQUITY - 4.50) / EQUITY)
        self.assertAlmostEqual(record["max_epoch_drawdown"], expected_dd)
        self.assertEqual(record["min_equity"], 4.00)

        second_id = "BGX_EPOCH_30PCT_TEST_V2"
        with patch.dict(os.environ, _env(**{
            risk_epoch.EPOCH_ID_ENV: second_id, risk_epoch.SUPERSEDE_ACK_ENV: EPOCH_ID,
        }), clear=False):
            created = await risk_epoch.observe(restarted)
        self.assertEqual(created["status"], risk_epoch.ACTIVE)
        second = json.loads(await self._get(risk_epoch.epoch_key(second_id, NAMESPACE)))
        self.assertEqual(second["previous_epoch_status"], "CLOSED_BY_SUCCESSOR")
        self.assertAlmostEqual(second["previous_epoch_drawdown"], expected_dd)
        closed = await self._record()
        self.assertTrue(closed["closed"])
        self.assertEqual(closed["closed_by_epoch_id"], second_id)
        self.assertEqual(closed["baseline_digest"], risk_epoch.baseline_digest(closed))

        # The closed epoch can never be revived by switching the id back.
        with patch.dict(os.environ, _env(), clear=False):
            revived = await risk_epoch.observe(restarted)
            blocked, _ = risk_epoch.blocks_new_entries(revived)
        self.assertEqual(revived["status"], risk_epoch.INVALID)
        self.assertEqual(revived["reason"], "epoch_closed_by_successor")
        self.assertTrue(blocked)

    async def test_amount_change_on_same_flow_identity_is_detected(self):
        engine = await self._engine()
        with patch.dict(os.environ, _env(), clear=False):
            await risk_epoch.observe(engine)
            altered = json.loads(json.dumps(LEDGER_DOC))
            altered["applied"][1]["net_amount"] = 12.7808   # same id, new amount
            await self._put(cash_flow_ledger.LEDGER_KEY, json.dumps(altered))
            state = await risk_epoch.observe(engine)
            blocked, _ = risk_epoch.blocks_new_entries(state)
        self.assertEqual(state["status"], risk_epoch.FLOW_CHANGED)
        self.assertTrue(blocked)

    async def test_active_epoch_succession_requires_exact_explicit_ack(self):
        engine = await self._engine()
        with patch.dict(os.environ, _env(), clear=False):
            await risk_epoch.observe(engine)
        before = await self._record()
        second_id = "BGX_EPOCH_30PCT_TEST_V2"
        for ack in (None, "WRONG_EPOCH_ID_V0"):
            extra = {risk_epoch.EPOCH_ID_ENV: second_id}
            if ack:
                extra[risk_epoch.SUPERSEDE_ACK_ENV] = ack
            with patch.dict(os.environ, _env(**extra), clear=False):
                if ack is None:
                    os.environ.pop(risk_epoch.SUPERSEDE_ACK_ENV, None)
                state = await risk_epoch.observe(engine)
                blocked, _ = risk_epoch.blocks_new_entries(state)
            self.assertEqual(state["status"], risk_epoch.PENDING_BASELINE, ack)
            self.assertEqual(state["reason"], "previous_epoch_supersede_ack_missing")
            self.assertTrue(blocked)
        self.assertIsNone(await self._get(risk_epoch.epoch_key(second_id, NAMESPACE)))
        self.assertEqual(await self._record(), before)
        index = json.loads(await self._get(risk_epoch.index_key(NAMESPACE)))
        self.assertEqual(index["epochs"], [EPOCH_ID])

    # ── gates ──────────────────────────────────────────────────────────────

    async def test_historical_gate_still_blocks_and_epoch_never_unblocks(self):
        from bot import pilot_live_runtime, reentry_readiness

        engine = await self._engine()
        engine._pilot_live_prelive_ready = True
        log = logging.getLogger("test.risk_epoch")
        with patch.dict(os.environ, _env(), clear=False), \
             patch("bot.operator_runtime_policy._risk_override_enabled", return_value=False), \
             patch("bot.drawdown_recovery.threshold_decision",
                   return_value=(False, "disabled", SimpleNamespace(episode_id=""))), \
             patch("bot.controlled_live_reentry_v1.drawdown_bridge_allowed",
                   return_value=(False, "manual_arm_missing")):
            state = await risk_epoch.observe(engine)
            self.assertEqual(state["status"], risk_epoch.ACTIVE)
            self.assertFalse(risk_epoch.blocks_new_entries(state)[0])
            allowed = await pilot_live_runtime._entry_drawdown_allows_durable(engine, log)
            readiness = reentry_readiness.snapshot(engine)
        self.assertFalse(allowed)  # 76.68% >= 30%: the historical gate decides.
        self.assertIn("DRAWDOWN_ABOVE_LIMIT", readiness["blockers"])
        self.assertAlmostEqual(readiness["historical_drawdown"], 0.7667548284, places=9)
        self.assertEqual(readiness["epoch_drawdown"], 0.0)
        self.assertEqual(readiness["epoch_drawdown_limit"], 0.30)
        self.assertEqual(readiness["epoch_status"], risk_epoch.ACTIVE)

    async def test_breached_epoch_blocks_even_when_historical_gate_passes(self):
        from bot import pilot_live_runtime

        engine = await self._engine()
        log = logging.getLogger("test.risk_epoch")
        with patch.dict(os.environ, _env(), clear=False):
            await risk_epoch.observe(engine)
            await self._set_equity(engine, 3.60)
            with patch("bot.config.cfg.MAX_DRAWDOWN", 0.99):  # historical would pass
                allowed = await pilot_live_runtime._entry_drawdown_allows_durable(engine, log)
        self.assertFalse(allowed)
        self.assertEqual(engine._risk_epoch_state["status"], risk_epoch.BREACHED)

    async def test_disabled_epoch_performs_no_io_and_changes_nothing(self):
        from bot import pilot_live_runtime

        engine = await self._engine()
        log = logging.getLogger("test.risk_epoch")
        with patch.dict(os.environ, {risk_epoch.ENABLED_ENV: "false"}, clear=False), \
             patch("bot.config.cfg.MAX_DRAWDOWN", 0.99), \
             patch("bot.risk_epoch.observe") as observe:
            allowed = await pilot_live_runtime._entry_drawdown_allows_durable(engine, log)
        self.assertTrue(allowed)
        observe.assert_not_called()
        self.assertIsNone(await self._record())


if __name__ == "__main__":
    unittest.main()
