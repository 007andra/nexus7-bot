"""NOVO-02 U — composed LIVE-pilot ownership attack (offline, fake exchange).

1. BGX opens ETH LONG (real entry hooks), 2. partial (real durable_partial_exit),
3. full close (2R exit, or exchange stop while the bot is down),
4. durable history is kept, 5. operator opens a MANUAL ETH LONG,
6. restart through the FINAL composed ``_load_existing_positions``,
7. trailing / partial / 2R / protection cycles run.
The manual position must stay EXTERNAL with ZERO BGX stop/reduce mutations,
while a still-open BGX trade (control) is re-adopted and keeps being managed.
"""
import time
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_exit_geometry_durability_composed import _Exchange  # composed LIVE env

from bot import database as db  # noqa: E402
from bot import engine as core  # noqa: E402
from bot import exit_geometry_durability as g  # noqa: E402
from bot import trade_lifecycle as tl  # noqa: E402
from bot.strategy import Signal  # noqa: E402


class ComposedOwnershipAttackTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, close_how, manual_contracts, manual_entry, opening_ts=None):
        store = {}

        async def load(key, strict=False):
            return store.get(key)

        async def save(key, value, strict=False):
            store[key] = value
            return True
        ex = _Exchange("LONG")
        with patch.object(db, "load_key_value", AsyncMock(side_effect=load)), \
                patch.object(db, "save_key_value", AsyncMock(side_effect=save)):
            engine = ex.engine()
            pos = core.Position(Signal("ETHUSDT", "LONG", 100.0, 98.0, 104.0, .8, "t", 80), 1.0)
            pos._forensic_lineage = {"order_id": "open-1", "client_oid": "bgx7-open-1", "version": 2}
            engine.positions["ETHUSDT"] = pos
            await tl.open_trade(pos, pos.qty)               # post_trade_forensics entry hook
            await g.persist(pos, "entry_confirmed")

            async def cycle(price):
                ex.mark = price
                engine.positions["ETHUSDT"].update_pnl(price)
                with patch.object(engine, "_sync_positions", AsyncMock()):
                    await engine._manage_partial_tp()
                    await engine._apply_trailing_stops()
                    await engine._check_rr_double()

            await cycle(102.1)                               # BGX partial
            if close_how == "2R":
                await cycle(104.0)                           # 2R closes the remainder
            elif close_how == "stop_downtime":
                ex.add_fill("stop-1", "sell", ex.contracts)
                ex.contracts = 0
            if close_how != "open":
                ex.add_fill("manual-1", "buy", manual_contracts)
                ex.contracts = manual_contracts
            ex.stop, ex.mark = 99.0, manual_entry
            ex.set_sl_calls.clear()
            ex.reduce_orders.clear()

            engine = ex.engine()                             # crash + restart, same DB/exchange
            raw = engine.client._client
            original = raw.get_positions

            async def rows():
                out = await original()
                for row in out:
                    row["entryPrice"] = row["avgPrice"] = manual_entry
                    if opening_ts:
                        row["openingTimestamp"] = opening_ts
                return out
            raw.get_positions = rows
            await engine._load_existing_positions()
            adopted = "ETHUSDT" in engine.positions
            if adopted:
                for price in (101.0, 102.2, 103.0, 104.5):
                    await cycle(price)
            proof = engine._restart_ownership_proofs.get("ETHUSDT")
            lifecycle = await tl.load("open-1")
        return adopted, getattr(proof, "reason", None), list(ex.set_sl_calls), list(ex.reduce_orders), lifecycle

    async def test_u_manual_position_after_closed_trade_gets_zero_mutations(self):
        cases = [
            ("2R", 100, 100.5),                         # exact size, closed by 2R
            ("2R", 8, 100.5),                           # smaller size (the Q-01B widening)
            ("stop_downtime", 50, 100.0),               # stop filled while down, exact residual, same entry
            ("stop_downtime", 50, 100.0, time.time() * 1000 + 3_600_000),
        ]
        for case in cases:
            adopted, reason, set_sl, reduces, lifecycle = await self._run(*case)
            self.assertFalse(adopted, (case, reason))
            self.assertEqual((set_sl, reduces), ([], []), (case, "zero BGX mutations"))
            if case[0] == "2R":
                self.assertEqual(lifecycle["status"], "CLOSED")

    async def test_u_control_still_open_bgx_trade_is_readopted(self):
        adopted, reason, set_sl, reduces, lifecycle = await self._run("open", 50, 100.0)
        self.assertTrue(adopted, reason)
        # The post-restart 2R exit closed the remainder -> lineage terminal (L).
        self.assertEqual((lifecycle["status"], lifecycle["close_reason"]), ("CLOSED", "rr_exit_flat"))
        self.assertEqual(set_sl, [101.65, 102.25, 103.375], "management continues")
        self.assertEqual(len(reduces), 1, "2R exit of the BGX remainder")


if __name__ == "__main__":
    unittest.main()
