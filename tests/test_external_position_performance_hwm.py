"""Regression tests for external/manual-position performance-HWM quarantine."""
import asyncio
import json
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

from bot import database as db
from bot import drawdown_persistence as ddp
from bot import external_position_performance as epp
from bot import hwm_namespace
from bot import pilot_live_runtime as plr
from bot.professional_risk import CapitalState
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.risk import RiskManager


LOG = logging.getLogger("test_external_position_performance_hwm")


def new_risk(equity: float):
    legacy = RiskManager()
    legacy.init(float(equity))
    return ProfessionalRiskAdapter(legacy)


class FakeBinance:
    def __init__(self, *, wallet: float, unrealized: float = 0.0):
        self.wallet = float(wallet)
        self.unrealized = float(unrealized)
        self.position_margin = 0.0
        self.positions = []
        self.rows = []
        self.trades = []
        self.orders = []
        self.now_ms = epp._INCIDENT_END_MS
        self.fail_positions = False

    def _listen_key_request(self):
        return None

    def _now_ms(self):
        return self.now_ms

    async def get_account_state(self):
        equity = self.wallet + self.unrealized
        return {
            "equity": equity,
            "available": max(0.0, equity - self.position_margin),
            "available_source": "availableBalance",
            "walletBalance": self.wallet,
            "unrealisedPNL": self.unrealized,
            "positionMargin": self.position_margin,
            "orderMargin": 0.0,
            "multiAssetsMargin": False,
            "canTrade": True,
        }

    async def get_positions(self):
        if self.fail_positions:
            raise RuntimeError("position endpoint unavailable")
        return [dict(row) for row in self.positions]

    async def _get(self, endpoint, params=None, auth=False):
        start = int(params["startTime"])
        end = int(params["endTime"])
        if endpoint == "/fapi/v1/income":
            source = self.rows
        elif endpoint == "/fapi/v1/userTrades":
            source = self.trades
        elif endpoint == "/fapi/v1/allOrders":
            source = self.orders
        else:
            raise AssertionError(endpoint)
        symbol = str((params or {}).get("symbol") or "").upper()
        return [
            dict(row)
            for row in source
            if start <= int(row["time"]) <= end
            and (
                not symbol
                or str(row.get("symbol") or "").upper() == symbol
            )
        ]


def incident_rows(net_adjustment: float = 0.0):
    base = [
        ("COMMISSION", -0.01346650, epp._INCIDENT_START_MS + 10_000, 8101),
        ("COMMISSION", -0.01346650, epp._INCIDENT_START_MS + 20_000, 8102),
        ("COMMISSION", -0.01346650, epp._INCIDENT_START_MS + 30_000, 8103),
        ("COMMISSION", -0.01346650, epp._INCIDENT_START_MS + 40_000, 8104),
        ("COMMISSION", -0.01346648, epp._INCIDENT_START_MS + 50_000, 8105),
        ("REALIZED_PNL", 0.45130999, epp._INCIDENT_END_MS - 30_000, 8106),
        ("COMMISSION", -0.14653506, epp._INCIDENT_END_MS - 20_000, 8107),
        ("FUNDING_FEE", -0.02571883 + net_adjustment, epp._INCIDENT_END_MS - 10_000, 8108),
    ]
    return [
        {
            "symbol": "ATOMUSDT",
            "incomeType": kind,
            "income": f"{amount:.8f}",
            "asset": "USDT",
            "info": "external manual position",
            "time": ts,
            "tranId": tran,
            "tradeId": str(tran),
        }
        for kind, amount, ts, tran in base
    ]


def incident_trade_evidence(*, bgx_identity: bool = False):
    opening_commissions = [
        0.01346650,
        0.01346650,
        0.01346650,
        0.01346650,
        0.01346648,
    ]
    trades = []
    orders = []
    for idx, commission in enumerate(opening_commissions, start=1):
        order_id = 9100 + idx
        ts = epp._INCIDENT_START_MS + idx * 10_000
        trades.append({
            "symbol": "ATOMUSDT",
            "id": 9200 + idx,
            "orderId": order_id,
            "side": "BUY",
            "price": "1.80",
            "qty": "1",
            "quoteQty": "1.80",
            "realizedPnl": "0.00000000",
            "commission": f"{commission:.8f}",
            "commissionAsset": "USDT",
            "time": ts,
            "buyer": True,
            "maker": False,
            "positionSide": "BOTH",
        })
        orders.append({
            "symbol": "ATOMUSDT",
            "orderId": order_id,
            "clientOrderId": (
                "bgx7-forbidden" if bgx_identity and idx == 1
                else f"manual-open-{idx}"
            ),
            "status": "FILLED",
            "side": "BUY",
            "positionSide": "BOTH",
            "type": "MARKET",
            "origType": "MARKET",
            "reduceOnly": False,
            "closePosition": False,
            "executedQty": "1",
            "avgPrice": "1.80",
            "time": ts,
            "updateTime": ts,
        })

    close_order_id = 9199
    close_ts = epp._INCIDENT_END_MS - 20_000
    trades.append({
        "symbol": "ATOMUSDT",
        "id": 9299,
        "orderId": close_order_id,
        "side": "SELL",
        "price": "1.85",
        "qty": "5",
        "quoteQty": "9.25",
        "realizedPnl": "0.45130999",
        "commission": "0.14653506",
        "commissionAsset": "USDT",
        "time": close_ts,
        "buyer": False,
        "maker": False,
        "positionSide": "BOTH",
    })
    orders.append({
        "symbol": "ATOMUSDT",
        "orderId": close_order_id,
        "clientOrderId": "manual-close",
        "status": "FILLED",
        "side": "SELL",
        "positionSide": "BOTH",
        "type": "MARKET",
        "origType": "MARKET",
        "reduceOnly": True,
        "closePosition": False,
        "executedQty": "5",
        "avgPrice": "1.85",
        "time": close_ts,
        "updateTime": close_ts,
    })
    return trades, orders


def pinned_flat_evidence():
    start = epp._FLAT_INCIDENT_STARTED_MS
    trades = []
    orders = []
    rows = []
    specs = [
        ("OPUSDT", 3572.6, start - 300_000, start + 1_800_000, -1.25),
        ("SEIUSDT", 100.0, start + 2_000_000, start + 4_000_000, -0.35),
    ]
    for index, (symbol, qty, opened, closed, realized) in enumerate(specs, start=1):
        open_order = 30000 + index * 10
        close_order = open_order + 1
        trades.extend([
            {
                "symbol": symbol,
                "id": 40000 + index * 10,
                "orderId": open_order,
                "side": "BUY",
                "price": "1",
                "qty": str(qty),
                "quoteQty": str(qty),
                "realizedPnl": "0",
                "commission": "0.01000000",
                "commissionAsset": "USDT",
                "time": opened,
                "buyer": True,
                "maker": False,
                "positionSide": "BOTH",
            },
            {
                "symbol": symbol,
                "id": 40000 + index * 10 + 1,
                "orderId": close_order,
                "side": "SELL",
                "price": "1",
                "qty": str(qty),
                "quoteQty": str(qty),
                "realizedPnl": str(realized),
                "commission": "0.01000000",
                "commissionAsset": "USDT",
                "time": closed,
                "buyer": False,
                "maker": False,
                "positionSide": "BOTH",
            },
        ])
        orders.extend([
            {
                "symbol": symbol,
                "orderId": open_order,
                "clientOrderId": f"manual-{symbol}-open",
                "status": "FILLED",
                "side": "BUY",
                "positionSide": "BOTH",
                "type": "MARKET",
                "origType": "MARKET",
                "reduceOnly": False,
                "closePosition": False,
                "executedQty": str(qty),
                "avgPrice": "1",
                "time": opened,
                "updateTime": opened,
            },
            {
                "symbol": symbol,
                "orderId": close_order,
                "clientOrderId": f"manual-{symbol}-close",
                "status": "FILLED",
                "side": "SELL",
                "positionSide": "BOTH",
                "type": "MARKET",
                "origType": "MARKET",
                "reduceOnly": True,
                "closePosition": False,
                "executedQty": str(qty),
                "avgPrice": "1",
                "time": closed,
                "updateTime": closed,
            },
        ])
        rows.extend([
            {
                "symbol": symbol,
                "incomeType": "COMMISSION",
                "income": "-0.02000000",
                "asset": "USDT",
                "info": "",
                "time": closed,
                "tranId": 50000 + index * 10,
                "tradeId": str(40000 + index * 10 + 1),
            },
            {
                "symbol": symbol,
                "incomeType": "REALIZED_PNL",
                "income": str(realized),
                "asset": "USDT",
                "info": "",
                "time": closed,
                "tranId": 50000 + index * 10 + 1,
                "tradeId": str(40000 + index * 10 + 1),
            },
        ])
    return trades, orders, rows


def pinned_quarantine_state():
    return {
        "version": 1,
        "status": "UNRESOLVED",
        "reason": "external_symbol_set_changed",
        "symbols": ["OPUSDT", "SEIUSDT"],
        "started_at_ms": epp._FLAT_INCIDENT_STARTED_MS,
        "pre_event_equity": None,
        "pre_event_peak": None,
        "execution_effect": "BLOCK_NEW_ENTRIES",
    }


class ExternalPerformanceHwmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig = (db._conn, db._is_pg)
        db._conn = await aiosqlite.connect(":memory:")
        db._is_pg = False
        await db._create_tables()

    async def asyncTearDown(self):
        await db._conn.close()
        db._conn, db._is_pg = self._orig

    async def set_peak(self, value: float):
        await db.save_key_value(ddp.DURABLE_EQUITY_PEAK_KEY, str(value), strict=True)

    async def peak(self):
        raw = await db.load_key_value(ddp.DURABLE_EQUITY_PEAK_KEY, strict=True)
        return None if raw is None else float(raw)

    async def quarantine(self):
        raw = await db.load_key_value(epp.state_key(), strict=True)
        return None if raw is None else json.loads(raw)

    def engine(self, client, *, prior_equity=None, prior_ms=None):
        engine = SimpleNamespace(
            client=client,
            risk=new_risk(prior_equity or client.wallet),
            positions={},
        )
        if prior_equity is not None:
            engine._pilot_prev_account_equity = float(prior_equity)
        if prior_ms is not None:
            engine._pilot_prev_account_observed_ms = int(prior_ms)
        return engine

    def test_owned_symbol_requires_compatible_side_and_quantity(self):
        client = FakeBinance(wallet=6.0)
        engine = SimpleNamespace(
            client=client,
            positions={
                "ATOMUSDT": SimpleNamespace(qty=10.0, direction="LONG")
            },
        )

        compatible = [{
            "symbol": "ATOMUSDT",
            "size": 8.0,
            "sizeUnit": "BASE_ASSET",
            "side": "Buy",
        }]
        self.assertEqual(epp._unowned_symbols(engine, compatible), set())

        increased = [{
            "symbol": "ATOMUSDT",
            "size": 12.0,
            "sizeUnit": "BASE_ASSET",
            "side": "Buy",
        }]
        reversed_side = [{
            "symbol": "ATOMUSDT",
            "size": 8.0,
            "sizeUnit": "BASE_ASSET",
            "side": "Sell",
        }]
        self.assertEqual(epp._unowned_symbols(engine, increased), {"ATOMUSDT"})
        self.assertEqual(
            epp._unowned_symbols(engine, reversed_side), {"ATOMUSDT"}
        )

    def incident_engine(self):
        client = FakeBinance(wallet=epp._INCIDENT_POST_EQUITY)
        client.rows = incident_rows()
        client.trades, client.orders = incident_trade_evidence()
        return self.engine(client)

    async def repair(self, engine):
        return await epp.maybe_repair_known_atom_incident(
            engine, await engine.client.get_account_state(), [], log=LOG
        )

    async def test_incident_cannot_repair_twice_after_restart_and_hwm_revisit(self):
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        self.assertTrue(await self.repair(self.incident_engine()))
        # Simulate ordinary later highs replacing the latest provenance and a
        # return to the historical numeric predicate. The incident marker must
        # survive independently, including a closed/reopened database.
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        await db.save_key_value(hwm_namespace.provenance_key(), "later-high", strict=True)
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/restart.sqlite"
            target = await aiosqlite.connect(path)
            await db._conn.backup(target)
            await target.close()
            await db._conn.close()
            db._conn = await aiosqlite.connect(path)
            fresh_engine = self.incident_engine()
            with patch.object(epp, "collect_income", new=AsyncMock(wraps=epp.collect_income)) as income:
                self.assertFalse(await self.repair(fresh_engine))
                income.assert_not_awaited()
            self.assertEqual(await self.peak(), epp._INCIDENT_BAD_HWM)
            self.assertFalse(hasattr(fresh_engine.risk, ddp._CACHE_ATTR))
            self.assertEqual(
                await db.load_key_value(hwm_namespace.provenance_key(), strict=True),
                "later-high",
            )
            await db._conn.close()
            db._conn = await aiosqlite.connect(":memory:")

    async def test_incident_marker_commits_with_hwm_and_provenance(self):
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        self.assertTrue(await self.repair(self.incident_engine()))
        self.assertEqual(
            await db.load_key_value(epp.incident_repair_key(), strict=True),
            epp._incident_consumed_marker(),
        )
        provenance = json.loads(await db.load_key_value(hwm_namespace.provenance_key(), strict=True))
        self.assertEqual(provenance["new_peak"], await self.peak())
        self.assertEqual(provenance["old_peak"], epp._INCIDENT_BAD_HWM)

    async def test_legacy_namespace_consumed_marker_survives_novo03_fallback(self):
        current_ns = "v3:stable-test"
        legacy_ns = "v2:legacy-test"
        legacy_key = (
            "risk:external_performance_repair:ATOMUSDT_20260927:v1:"
            + legacy_ns
        )
        legacy_marker = epp._incident_consumed_marker_for_namespace(legacy_ns)
        await db.save_key_value(legacy_key, legacy_marker, strict=True)

        with (
            patch.object(hwm_namespace, "hwm_namespace", return_value=current_ns),
            patch.object(
                hwm_namespace,
                "legacy_hwm_namespace",
                return_value=legacy_ns,
            ),
        ):
            self.assertTrue(await epp._incident_already_consumed())

    async def test_legacy_namespace_marker_mutation_remains_fail_closed(self):
        current_ns = "v3:stable-test"
        legacy_ns = "v2:legacy-test"
        legacy_key = (
            "risk:external_performance_repair:ATOMUSDT_20260927:v1:"
            + legacy_ns
        )
        marker = json.loads(
            epp._incident_consumed_marker_for_namespace(legacy_ns)
        )
        marker["status"] = "PENDING"
        await db.save_key_value(
            legacy_key,
            json.dumps(marker, sort_keys=True, separators=(",", ":")),
            strict=True,
        )

        with (
            patch.object(hwm_namespace, "hwm_namespace", return_value=current_ns),
            patch.object(
                hwm_namespace,
                "legacy_hwm_namespace",
                return_value=legacy_ns,
            ),
        ):
            with self.assertRaisesRegex(
                db.PersistenceError,
                "marker ambiguous",
            ):
                await epp._incident_already_consumed()

    async def test_incident_marker_failure_rolls_back_all_three_keys(self):
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        await db.save_key_value(hwm_namespace.provenance_key(), "before", strict=True)
        # The marker is the last write; failing here proves HWM and provenance
        # are rolled back, rather than merely testing a pre-write exception.
        await db._conn.execute(
            "CREATE TRIGGER reject_incident_marker BEFORE INSERT ON key_value "
            "WHEN NEW.key LIKE 'risk:external_performance_repair:%' "
            "BEGIN SELECT RAISE(ABORT, 'injected marker failure'); END"
        )
        await db._conn.commit()
        engine = self.incident_engine()
        old_memory_peak = engine.risk._legacy.peak_balance
        with self.assertRaises(db.PersistenceError):
            await self.repair(engine)
        self.assertEqual(await self.peak(), epp._INCIDENT_BAD_HWM)
        self.assertEqual(await db.load_key_value(hwm_namespace.provenance_key(), strict=True), "before")
        self.assertIsNone(await db.load_key_value(epp.incident_repair_key(), strict=True))
        self.assertEqual(engine.risk._legacy.peak_balance, old_memory_peak)
        self.assertFalse(hasattr(engine.risk, ddp._CACHE_ATTR))
        await db._conn.execute("DROP TRIGGER reject_incident_marker")
        await db._conn.commit()
        self.assertTrue(await self.repair(engine))

    async def test_ambiguous_incident_marker_blocks_without_writes(self):
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        for raw in ("", "null", "{}", "garbage", '{"status":"PENDING"}',
                    epp._incident_consumed_marker().replace('"version":1', '"version":2')):
            with self.subTest(raw=raw):
                await db.save_key_value(epp.incident_repair_key(), raw, strict=True)
                with self.assertRaisesRegex(db.PersistenceError, "marker ambiguous"):
                    await plr._refresh_account(self.incident_engine(), LOG)
                self.assertEqual(await self.peak(), epp._INCIDENT_BAD_HWM)
                self.assertEqual(await db.load_key_value(epp.incident_repair_key(), strict=True), raw)
                self.assertIsNone(await db.load_key_value(hwm_namespace.provenance_key(), strict=True))

    async def test_incident_marker_read_failure_is_not_absence(self):
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        original = db.load_key_value

        async def read(key, **kwargs):
            if key == epp.incident_repair_key():
                self.assertIs(kwargs.get("strict"), True)
                raise db.PersistenceError("injected marker read failure")
            return await original(key, **kwargs)

        with patch.object(db, "load_key_value", side_effect=read):
            with self.assertRaisesRegex(db.PersistenceError, "marker read failure"):
                await self.repair(self.incident_engine())
        self.assertEqual(await self.peak(), epp._INCIDENT_BAD_HWM)

    async def test_lost_commit_ack_does_not_permit_retry_rebase(self):
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        original = ddp.save_key_values_atomic_cas
        engine = self.incident_engine()

        async def lost_ack(*args, **kwargs):
            await original(*args, **kwargs)
            raise db.PersistenceError("commit acknowledgement lost")

        with patch.object(ddp, "save_key_values_atomic_cas", side_effect=lost_ack):
            with self.assertRaisesRegex(db.PersistenceError, "acknowledgement lost"):
                await self.repair(engine)
        self.assertFalse(hasattr(engine.risk, ddp._CACHE_ATTR))
        committed_peak = await self.peak()
        self.assertNotEqual(committed_peak, epp._INCIDENT_BAD_HWM)
        self.assertFalse(await self.repair(self.incident_engine()))
        self.assertEqual(await self.peak(), committed_peak)

    async def test_concurrent_incident_repairs_have_one_winner(self):
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        original = ddp.save_key_values_atomic_cas
        ready = asyncio.Event()
        arrivals = 0

        async def commit(*args, **kwargs):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=5)
            return await original(*args, **kwargs)

        with patch.object(ddp, "save_key_values_atomic_cas", side_effect=commit):
            results = await asyncio.gather(
                self.repair(self.incident_engine()), self.repair(self.incident_engine()),
                return_exceptions=True,
            )
        self.assertEqual(sum(result is True for result in results), 1)
        self.assertEqual(sum(isinstance(result, db.PersistenceError) for result in results), 1)
        self.assertEqual(await db.load_key_value(epp.incident_repair_key(), strict=True), epp._incident_consumed_marker())

    async def test_marker_created_after_read_refuses_even_identical_hwm(self):
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        original = ddp.save_key_values_atomic_cas

        async def raced_commit(*args, **kwargs):
            await db.save_key_value(epp.incident_repair_key(), epp._incident_consumed_marker(), strict=True)
            return await original(*args, **kwargs)

        with patch.object(ddp, "save_key_values_atomic_cas", side_effect=raced_commit):
            with self.assertRaises(db.PersistenceError):
                await self.repair(self.incident_engine())
        self.assertEqual(await self.peak(), epp._INCIDENT_BAD_HWM)
        self.assertIsNone(await db.load_key_value(hwm_namespace.provenance_key(), strict=True))

    async def test_hwm_change_during_evidence_collection_refuses_repair(self):
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        original = epp.collect_income

        async def changed_peak(*args, **kwargs):
            await self.set_peak(8.0)
            return await original(*args, **kwargs)

        with patch.object(epp, "collect_income", side_effect=changed_peak):
            with self.assertRaises(db.PersistenceError):
                await self.repair(self.incident_engine())
        self.assertEqual(await self.peak(), 8.0)
        self.assertIsNone(await db.load_key_value(epp.incident_repair_key(), strict=True))

    async def test_active_external_position_cannot_create_new_performance_high(self):
        await self.set_peak(6.4680)
        client = FakeBinance(wallet=6.4680, unrealized=0.8882)
        client.position_margin = 1.0
        client.positions = [{
            "symbol": "ATOMUSDT",
            "size": 10.0,
            "sizeUnit": "BASE_ASSET",
            "side": "Buy",
        }]
        engine = self.engine(
            client,
            prior_equity=5.8827,
            prior_ms=epp._INCIDENT_START_MS - 1_000,
        )

        await plr._refresh_account(engine, LOG)

        self.assertAlmostEqual(await self.peak(), 6.4680, places=6)
        legacy = engine.risk._legacy
        self.assertAlmostEqual(legacy.peak_balance, 6.4680, places=6)
        self.assertTrue(engine._external_performance_quarantine)
        state = await self.quarantine()
        self.assertEqual(state["status"], "ACTIVE")
        self.assertEqual(state["symbols"], ["ATOMUSDT"])
        self.assertAlmostEqual(state["pre_event_equity"], 5.8827, places=6)
        self.assertAlmostEqual(state["pre_event_peak"], 6.4680, places=6)
        self.assertFalse(plr._entry_drawdown_allows(engine, LOG))

    async def test_flat_after_unresolved_external_episode_remains_blocked(self):
        await self.set_peak(6.4680)
        client = FakeBinance(wallet=6.0944)
        engine = self.engine(client, prior_equity=5.8827, prior_ms=epp._INCIDENT_START_MS)
        await db.save_key_value(
            epp.state_key(),
            json.dumps({
                "version": 1,
                "status": "ACTIVE",
                "reason": "external_position_detected",
                "symbols": ["ATOMUSDT"],
                "started_at_ms": epp._INCIDENT_START_MS,
                "pre_event_equity": 5.8827,
                "pre_event_peak": 6.4680,
                "execution_effect": "BLOCK_NEW_ENTRIES",
            }, sort_keys=True, separators=(",", ":")),
            strict=True,
        )

        await plr._refresh_account(engine, LOG)

        self.assertAlmostEqual(await self.peak(), 6.4680, places=6)
        self.assertTrue(engine._external_performance_quarantine)
        self.assertFalse(plr._entry_drawdown_allows(engine, LOG))

    async def test_pinned_op_sei_flat_resolution_preserves_hwm_and_writes_receipt(self):
        await self.set_peak(22.7987)
        client = FakeBinance(wallet=5.3956)
        client.now_ms = epp._FLAT_INCIDENT_STARTED_MS + 16 * 3600 * 1000
        client.trades, client.orders, client.rows = pinned_flat_evidence()
        engine = self.engine(client, prior_equity=5.3956)
        await db.save_key_value(
            epp.state_key(),
            json.dumps(pinned_quarantine_state(), sort_keys=True, separators=(",", ":")),
            strict=True,
        )

        with patch.dict(
            os.environ,
            {epp._FLAT_RECONCILE_ENV: epp._FLAT_RECONCILE_TOKEN},
            clear=False,
        ):
            result = await epp.evaluate(
                engine, await client.get_account_state(), log=LOG
            )

        self.assertEqual(result, "NORMAL")
        self.assertFalse(engine._external_performance_quarantine)
        self.assertAlmostEqual(await self.peak(), 22.7987, places=6)
        state = await self.quarantine()
        self.assertEqual(state["status"], "RESOLVED")
        self.assertFalse(state["hwm_rebased"])
        receipt = json.loads(
            await db.load_key_value(epp.flat_resolution_key(), strict=True)
        )
        self.assertEqual(receipt["ownership"], "MANUAL_EXTERNAL_ONLY")
        self.assertTrue(receipt["account_flat"])
        self.assertFalse(receipt["hwm_rebased"])
        self.assertEqual(receipt["symbols"], ["OPUSDT", "SEIUSDT"])

    async def test_pinned_flat_resolution_requires_explicit_operator_approval(self):
        await self.set_peak(22.7987)
        client = FakeBinance(wallet=5.3956)
        client.now_ms = epp._FLAT_INCIDENT_STARTED_MS + 16 * 3600 * 1000
        client.trades, client.orders, client.rows = pinned_flat_evidence()
        engine = self.engine(client, prior_equity=5.3956)
        await db.save_key_value(
            epp.state_key(),
            json.dumps(pinned_quarantine_state(), sort_keys=True, separators=(",", ":")),
            strict=True,
        )

        with patch.dict(os.environ, {epp._FLAT_RECONCILE_ENV: ""}, clear=False):
            result = await epp.evaluate(
                engine, await client.get_account_state(), log=LOG
            )

        self.assertEqual(result, "QUARANTINE")
        self.assertTrue(engine._external_performance_quarantine)
        self.assertAlmostEqual(await self.peak(), 22.7987, places=6)
        self.assertIsNone(
            await db.load_key_value(epp.flat_resolution_key(), strict=True)
        )

    async def test_pinned_flat_resolution_refuses_bgx_or_unknown_trade_identity(self):
        await self.set_peak(22.7987)
        client = FakeBinance(wallet=5.3956)
        client.now_ms = epp._FLAT_INCIDENT_STARTED_MS + 16 * 3600 * 1000
        client.trades, client.orders, client.rows = pinned_flat_evidence()
        client.orders[0]["clientOrderId"] = "bgx7-conflict"
        engine = self.engine(client, prior_equity=5.3956)
        await db.save_key_value(
            epp.state_key(),
            json.dumps(pinned_quarantine_state(), sort_keys=True, separators=(",", ":")),
            strict=True,
        )

        with patch.dict(
            os.environ,
            {epp._FLAT_RECONCILE_ENV: epp._FLAT_RECONCILE_TOKEN},
            clear=False,
        ):
            with self.assertRaises(db.PersistenceError):
                await epp.evaluate(
                    engine, await client.get_account_state(), log=LOG
                )

        self.assertAlmostEqual(await self.peak(), 22.7987, places=6)
        state = await self.quarantine()
        self.assertNotEqual(state["status"], "RESOLVED")

    async def test_resolved_state_does_not_mask_future_external_position(self):
        await self.set_peak(22.7987)
        client = FakeBinance(wallet=5.3956)
        client.now_ms = epp._FLAT_INCIDENT_STARTED_MS + 16 * 3600 * 1000
        client.trades, client.orders, client.rows = pinned_flat_evidence()
        engine = self.engine(client, prior_equity=5.3956)
        await db.save_key_value(
            epp.state_key(),
            json.dumps(pinned_quarantine_state(), sort_keys=True, separators=(",", ":")),
            strict=True,
        )
        with patch.dict(
            os.environ,
            {epp._FLAT_RECONCILE_ENV: epp._FLAT_RECONCILE_TOKEN},
            clear=False,
        ):
            self.assertEqual(
                await epp.evaluate(
                    engine, await client.get_account_state(), log=LOG
                ),
                "NORMAL",
            )

        client.now_ms += 60_000
        client.positions = [{
            "symbol": "OPUSDT",
            "size": 1.0,
            "sizeUnit": "BASE_ASSET",
            "side": "Buy",
        }]
        result = await epp.evaluate(
            engine, await client.get_account_state(), log=LOG
        )
        self.assertEqual(result, "FREEZE")
        state = await self.quarantine()
        self.assertIn(state["status"], {"ACTIVE", "UNRESOLVED"})
        self.assertNotEqual(state["status"], "RESOLVED")

    async def test_known_atom_incident_rebases_to_pre_episode_drawdown(self):
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        client = FakeBinance(wallet=epp._INCIDENT_POST_EQUITY)
        client.rows = incident_rows()
        client.trades, client.orders = incident_trade_evidence()
        engine = self.engine(client, prior_equity=epp._INCIDENT_POST_EQUITY)
        engine.risk.update_capital(
            CapitalState(
                equity=epp._INCIDENT_POST_EQUITY,
                available_collateral=epp._INCIDENT_POST_EQUITY,
            )
        )

        await plr._refresh_account(engine, LOG)

        pre_equity = epp._INCIDENT_POST_EQUITY - epp._INCIDENT_NET_EXTERNAL
        expected_peak = (
            epp._INCIDENT_PRE_HWM
            * epp._INCIDENT_POST_EQUITY
            / pre_equity
        )
        expected_dd = 1.0 - pre_equity / epp._INCIDENT_PRE_HWM

        self.assertAlmostEqual(await self.peak(), expected_peak, places=9)
        self.assertAlmostEqual(engine.risk._legacy.drawdown, expected_dd, places=9)
        self.assertAlmostEqual(
            engine.risk.professional_snapshot.drawdown, expected_dd, places=9
        )
        self.assertAlmostEqual(engine.risk._legacy.drawdown * 100.0, 9.04953, places=4)
        self.assertFalse(engine._external_performance_quarantine)
        self.assertTrue(plr._entry_drawdown_allows(engine, LOG))

        raw = await db.load_key_value(hwm_namespace.provenance_key(), strict=True)
        provenance = json.loads(raw)
        self.assertEqual(
            provenance["reason"], "external_position_performance_rebase"
        )

    async def test_incident_repair_refuses_non_manual_order_identity(self):
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        client = FakeBinance(wallet=epp._INCIDENT_POST_EQUITY)
        client.rows = incident_rows()
        client.trades, client.orders = incident_trade_evidence(
            bgx_identity=True
        )
        engine = self.engine(client, prior_equity=epp._INCIDENT_POST_EQUITY)

        with self.assertRaisesRegex(
            db.PersistenceError,
            "external incident ownership is not provably manual",
        ):
            await plr._refresh_account(engine, LOG)

        self.assertAlmostEqual(
            await self.peak(), epp._INCIDENT_BAD_HWM, places=6
        )

    async def test_incident_repair_refuses_mismatched_exchange_ledger(self):
        await self.set_peak(epp._INCIDENT_BAD_HWM)
        client = FakeBinance(wallet=epp._INCIDENT_POST_EQUITY)
        client.rows = incident_rows(net_adjustment=-0.01)
        client.trades, client.orders = incident_trade_evidence()
        engine = self.engine(client, prior_equity=epp._INCIDENT_POST_EQUITY)

        with self.assertRaisesRegex(
            db.PersistenceError, "external incident income signature mismatch"
        ):
            await plr._refresh_account(engine, LOG)

        self.assertAlmostEqual(await self.peak(), epp._INCIDENT_BAD_HWM, places=6)

    async def test_position_read_failure_never_creates_new_high_and_blocks(self):
        await self.set_peak(6.4680)
        client = FakeBinance(wallet=7.3562)
        client.fail_positions = True
        engine = self.engine(client, prior_equity=5.8827, prior_ms=epp._INCIDENT_START_MS)

        await plr._refresh_account(engine, LOG)

        self.assertAlmostEqual(await self.peak(), 6.4680, places=6)
        self.assertTrue(engine._external_performance_quarantine)
        self.assertFalse(plr._entry_drawdown_allows(engine, LOG))


if __name__ == "__main__":
    unittest.main()
