"""NOVO-02 — closed/foreign history never owns a present position (offline).

OWNED = lifecycle OPEN AND exact opening lineage AND symbol/side match AND
exchange qty == opening - proven BGX reductions AND fill-ledger continuity
AND exchange order/protection proof. Anything else -> EXTERNAL.
"""
import json
import random
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import restart_ownership_recovery as recovery
from bot import trade_lifecycle as tl
from bot.engine import Position
from bot.strategy import Signal
from tests.test_restart_ownership_recovery import (
    POSITION, _STORE, _Client, _Engine, _filled_order,
)


def _record(order_id="oid-1"):
    return json.loads(_STORE[tl._key(order_id)])


def _put(record):
    _STORE[tl._key(record["opening_order_id"])] = json.dumps(record)


def _reduce(engine, order_id, contracts, reducer="partial-1"):
    """A BGX reduce order filled on the exchange and credited to the lineage."""
    record = _record(order_id)
    record["confirmed_reduced_qty"] = round(record["confirmed_reduced_qty"] + contracts * 0.01, 10)
    _put(record)
    engine.client.fills.append({
        "tradeId": f"t-{reducer}", "orderId": reducer, "symbol": "ETHUSDTM", "side": "sell",
        "size": str(contracts), "price": "1", "fee": "0", "feeCurrency": "USDT",
        "tradeTime": (int(time.time() * 1000) - 20_000) * 1_000_000, "tradeType": "trade"})


def _close(order_id="oid-1"):
    record = _record(order_id)
    record.update(status="CLOSED", closed_at_ms=int(time.time() * 1000), close_reason="exchange_flat_sync")
    _put(record)


def _manual(size, entry=2530.0, **extra):
    return dict(POSITION, size=size, sizeContracts=round(size * 100), entryPrice=entry, **extra)


def _position(oid="oid-1", qty=0.21):
    pos = Position(Signal("ETHUSDT", "LONG", 2530.0, 2480.0, 2630.0, 0.8, "t", 80), qty)
    pos._forensic_lineage = {"order_id": oid, "client_oid": "bgx7-owned"}
    return pos


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _STORE.clear()

        async def load(key, strict=False):
            return _STORE.get(key)

        async def save(key, value, strict=False):
            _STORE[key] = value
            return True
        for name, fn in (("load_key_value", load), ("save_key_value", save)):
            p = patch(f"bot.database.{name}", AsyncMock(side_effect=fn))
            p.start()
            self.addCleanup(p.stop)

    def engine(self, filled_size="21", stop_size="21"):
        client = _Client(status={"orderId": "oid-1", "clientOid": "bgx7-owned", "symbol": "ETHUSDTM",
                                 "side": "buy", "isActive": False, "cancelExist": False,
                                 "filledSize": filled_size})
        client.stops[0]["size"] = stop_size
        engine = _Engine(client)
        _filled_order(engine)                      # BGX opened 0.21 ETH (lineage OPEN)
        return engine

    async def prove(self, engine, position):
        return await recovery.prove_restart_ownership(engine, position)


class ClosedLineageTests(_Base):
    async def test_a_exact_size_manual_after_closed_trade_is_external(self):
        engine = self.engine()
        _close()
        proof = await self.prove(engine, _manual(0.21))
        self.assertEqual((proof.recovered, proof.reason), (False, "trade_lifecycle_closed"))

    async def test_b_smaller_manual_after_closed_partial_trade_is_external(self):
        engine = self.engine(stop_size="8")
        _reduce(engine, "oid-1", 11)
        _close()
        proof = await self.prove(engine, _manual(0.08))
        self.assertEqual((proof.recovered, proof.reason), (False, "trade_lifecycle_closed"))

    async def test_d_closed_wins_even_when_residual_matches(self):
        engine = self.engine(stop_size="10")
        _reduce(engine, "oid-1", 11)
        _close()
        proof = await self.prove(engine, _manual(0.10))
        self.assertFalse(proof.recovered)

    async def test_e_old_geometry_cannot_revive_closed_trade(self):
        from bot import exit_geometry_durability as g
        engine = self.engine()
        pos = _position()
        pos.update_pnl(2580.0)
        await g.persist(pos, "history")
        _close()
        fresh = _position(qty=0.21)
        fresh.initial_sl = None
        fresh._forensic_lineage = {"opening_order_id": "oid-1"}
        engine.positions = {"ETHUSDT": fresh}
        engine.client.get_positions = AsyncMock(return_value=[dict(POSITION)])
        self.assertFalse(await g.restore(engine, "ETHUSDT"))
        self.assertIsNone(fresh.initial_sl)

    async def test_f_old_partial_record_cannot_revive_closed_trade(self):
        from bot.confirmed_rr_exit import identity
        engine = self.engine(stop_size="8")
        key, idem = identity("ETHUSDT", SimpleNamespace(_forensic_lineage={"opening_order_id": "oid-1"}))
        _STORE[key.replace("rr_exit_v1:", "partial_exit_v1:")] = json.dumps(
            {"idem": idem.replace("rr-", "partial-", 1), "order_id": "p-1", "filled": True})
        _close()
        proof = await self.prove(engine, _manual(0.08))
        self.assertFalse(proof.recovered)

    async def test_g_manual_close_then_manual_reopen_is_external(self):
        engine = self.engine()
        closed = await tl.terminalize_positions({"ETHUSDT": _position()}, [], "exchange_flat_sync")
        self.assertEqual(closed, ["ETHUSDT"])
        proof = await self.prove(engine, _manual(0.21))
        self.assertEqual(proof.reason, "trade_lifecycle_closed")

    async def test_terminal_is_monotonic_and_never_resurrected(self):
        engine = self.engine()
        self.assertTrue(await tl.close("oid-1", "exchange_flat_sync"))
        self.assertFalse(await tl.close("oid-1", "again"))
        self.assertFalse(await tl.open_trade(_position(), 0.21))   # same lineage: no resurrection
        self.assertEqual(_record()["status"], "CLOSED")
        self.assertFalse(await tl.record_reduction(_position(), 0.10, 0.11, "late"))
        self.assertIsNotNone(engine)


class OpenLineageTests(_Base):
    async def test_c_i_legitimate_bgx_residual_is_owned(self):
        engine = self.engine(stop_size="10")
        pos = _position()
        await tl.record_reduction(pos, 0.10, 0.11, "partial_fill")
        engine.client.fills.append({"tradeId": "t-p", "orderId": "partial-1", "symbol": "ETHUSDTM",
                                    "side": "sell", "size": "11", "price": "1", "fee": "0",
                                    "feeCurrency": "USDT",
                                    "tradeTime": (int(time.time() * 1000) - 10_000) * 1_000_000,
                                    "tradeType": "trade"})
        proof = await self.prove(engine, _manual(0.10))
        self.assertTrue(proof.recovered)
        self.assertEqual((proof.order_id, proof.base_qty), ("oid-1", 0.10))

    async def test_h_q_manual_partial_reduction_breaks_ownership(self):
        engine = self.engine(stop_size="15")
        engine.client.fills.append({"tradeId": "t-m", "orderId": "manual-1", "symbol": "ETHUSDTM",
                                    "side": "sell", "size": "6", "price": "1", "fee": "0",
                                    "feeCurrency": "USDT",
                                    "tradeTime": (int(time.time() * 1000) - 10_000) * 1_000_000,
                                    "tradeType": "trade"})
        proof = await self.prove(engine, _manual(0.15))
        self.assertEqual((proof.recovered, proof.reason), (False, "trade_residual_mismatch"))

    async def test_reduction_credit_is_capped_by_bgx_order_size(self):
        self.engine()
        # BGX ordered 0.05, but the exchange shows 0.11 gone: only 0.05 is BGX.
        await tl.record_reduction(_position(), 0.10, 0.05, "partial_fill")
        self.assertAlmostEqual(_record()["confirmed_reduced_qty"], 0.05)

    async def test_p_increase_is_external(self):
        engine = self.engine()
        proof = await self.prove(engine, _manual(0.30))
        self.assertFalse(proof.recovered)

    async def test_r_side_mismatch_is_external(self):
        engine = self.engine()
        proof = await self.prove(engine, dict(_manual(0.21), side="Sell"))
        self.assertFalse(proof.recovered)

    async def test_s_t_entry_similarity_or_stop_alone_never_adopt(self):
        engine = _Engine(_Client())
        from tests.test_restart_ownership_recovery import _filled_order as legacy
        legacy(engine)
        _STORE.clear()                               # pre-patch history: no lifecycle record
        proof = await self.prove(engine, _manual(0.21, entry=2530.0))
        self.assertEqual((proof.recovered, proof.reason), (False, "trade_lifecycle_missing"))

    async def test_reopen_evidence_rejects(self):
        engine = self.engine()
        proof = await self.prove(engine, _manual(0.21, entry=2545.0))
        self.assertEqual(proof.reason, "trade_entry_mismatch")
        proof = await self.prove(engine, _manual(0.21, openingTimestamp=time.time() * 1000 + 3_600_000))
        self.assertEqual(proof.reason, "position_reopened_after_lineage")

    async def test_fill_ledger_disproves_continuity(self):
        engine = self.engine(stop_size="10")
        await tl.record_reduction(_position(), 0.10, 0.11, "partial_fill")
        now = int(time.time() * 1000)
        for oid, side, size, dt in (("partial-1", "sell", "11", 25_000), ("stop-1", "sell", "10", 20_000),
                                    ("manual-1", "buy", "10", 10_000)):
            engine.client.fills.append({"tradeId": f"t-{oid}", "orderId": oid, "symbol": "ETHUSDTM",
                                        "side": side, "size": size, "price": "1", "fee": "0",
                                        "feeCurrency": "USDT", "tradeTime": (now - dt) * 1_000_000,
                                        "tradeType": "trade"})
        proof = await self.prove(engine, _manual(0.10))
        self.assertEqual((proof.recovered, proof.reason), (False, "lineage_flat_in_fills"))
        engine.client.fills = [f for f in engine.client.fills if f["orderId"] != "stop-1"]
        engine.client.fills.append(dict(engine.client.fills[-1], tradeId="t-add", orderId="manual-2"))
        proof = await self.prove(engine, _manual(0.10))
        self.assertFalse(proof.recovered)

    async def test_unreadable_fill_ledger_fails_closed(self):
        engine = self.engine()
        engine.client._get = AsyncMock(side_effect=OSError("down"))
        proof = await self.prove(engine, _manual(0.21))
        self.assertEqual((proof.recovered, proof.reason), (False, "fills_ledger_unconfirmed"))

    async def test_o_new_bgx_trade_same_symbol_gets_new_lineage(self):
        engine = self.engine()
        _close()
        _filled_order(engine, client_oid="bgx7-new", order_id="oid-2")
        engine.client.status = {"orderId": "oid-2", "clientOid": "bgx7-new", "symbol": "ETHUSDTM",
                                "side": "buy", "isActive": False, "cancelExist": False, "filledSize": "21"}
        proof = await self.prove(engine, _manual(0.21))
        self.assertTrue(proof.recovered)
        self.assertEqual(proof.order_id, "oid-2")
        self.assertEqual(_record("oid-1")["status"], "CLOSED")


class TerminalPathTests(_Base):
    async def test_n_boot_reconcile_terminalizes_before_adoption(self):
        engine = self.engine()
        engine.paper_trade = False
        engine.client.get_positions = AsyncMock(return_value=[])          # crash after close
        await tl.reconcile_at_boot(engine)
        self.assertEqual(_record()["status"], "CLOSED")

    async def test_boot_reconcile_unconfirmed_snapshot_changes_nothing(self):
        engine = self.engine()
        engine.client.get_positions = AsyncMock(side_effect=RuntimeError("POSITIONS_UNCONFIRMED"))
        await tl.reconcile_at_boot(engine)
        self.assertEqual(_record()["status"], "OPEN")

    async def test_boot_reconcile_flipped_side_terminalizes(self):
        engine = self.engine()
        engine.client.get_positions = AsyncMock(return_value=[dict(POSITION, side="Sell")])
        await tl.reconcile_at_boot(engine)
        self.assertEqual(_record()["status"], "CLOSED")

    async def test_j_m_sync_removal_terminalizes_with_deferred_retry(self):
        from bot import post_trade_forensics
        from bot.config import cfg

        class P(Position):
            pass

        class Engine:
            paper_trade = False

            def __init__(self):
                self.positions = {"ETHUSDT": _position()}
                self.client = SimpleNamespace(get_positions=AsyncMock(
                    side_effect=[RuntimeError("POSITIONS_UNCONFIRMED"), []]))

            async def _open(self, sig):
                return None

            async def _sync_positions(self):
                self.positions.pop("ETHUSDT", None)     # core sync saw an authoritative flat
        self.engine()
        post_trade_forensics.install(Engine, P, cfg, 0.0006, Mock())
        engine = Engine()
        await engine._sync_positions()
        self.assertEqual(_record()["status"], "OPEN", "unconfirmed read never terminalizes")
        await engine._sync_positions()
        self.assertEqual(_record()["status"], "CLOSED", "deferred retry with authoritative read")

    async def test_k_emergency_flatten_terminalizes(self):
        from bot import emergency_flatten
        from tests.test_emergency_flatten import FakeExchange, _engine
        self.engine()
        exchange = FakeExchange({"ETHUSDT": 21})
        engine = _engine(exchange)
        engine.positions = {"ETHUSDT": _position()}
        with patch("bot.durable_execution.persist_orders", AsyncMock(return_value=True)):
            summary = await emergency_flatten.close_all_positions(engine, verify_delay_s=0)
        self.assertEqual(summary["status"], "FLAT")
        self.assertEqual(_record()["status"], "CLOSED")

    async def test_l_two_r_full_exit_terminalizes(self):
        from bot.confirmed_rr_exit import check
        self.engine()
        pos = _position()
        pos.update_pnl(2630.0)
        client = SimpleNamespace(build_client_oid=Mock(return_value="rr"),
                                 place_order=AsyncMock(return_value={"orderId": "x"}),
                                 get_order_by_client_oid=AsyncMock(return_value={}),
                                 wait_for_fill=AsyncMock(return_value={"filled": True}),
                                 get_positions=AsyncMock(return_value=[]))
        engine = SimpleNamespace(positions={"ETHUSDT": pos}, client=client, instruments={},
                                 _sync_positions=AsyncMock())
        await check(engine)
        self.assertEqual(_record()["status"], "CLOSED")


class PropertyTests(_Base):
    async def test_property_adoption_only_for_open_exact_residual(self):
        rng = random.Random(202)
        for _ in range(120):
            _STORE.clear()
            engine = self.engine(stop_size="21")
            reduced = rng.choice([0, 0, 5, 11, 16])
            if reduced:
                _reduce(engine, "oid-1", reduced)
            status = rng.choice(["OPEN", "CLOSED"])
            if status == "CLOSED":
                _close()
            qty_contracts = rng.choice([21 - reduced, 21, 8, 10, 30, max(1, 21 - reduced - 3)])
            engine.client.stops[0]["size"] = str(qty_contracts)
            proof = await self.prove(engine, _manual(qty_contracts / 100))
            expected = status == "OPEN" and qty_contracts == 21 - reduced
            self.assertEqual(proof.recovered, expected, (status, reduced, qty_contracts, proof.reason))


if __name__ == "__main__":
    unittest.main()
