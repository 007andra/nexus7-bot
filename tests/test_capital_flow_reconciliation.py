import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import database as db
from bot.drawdown_persistence import DURABLE_EQUITY_PEAK_KEY
from bot.capital_flow_reconciliation import (
    BINANCE_LAST_FLOW_CURSOR_KEY,
    LAST_FLOW_OFFSET_KEY,
    reconcile_external_capital_flows,
)


class DummyClient:
    def __init__(self, payload):
        self._get = AsyncMock(return_value=payload)


class CapitalFlowReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_binance_bootstrap_checkpoints_without_rebasing(self):
        client = DummyClient({})
        client._listen_key_request = AsyncMock(return_value={"listenKey": "lk"})
        client.get_account_state = AsyncMock(return_value={
            "equity": 20.20, "walletBalance": 20.20, "unrealisedPNL": 0.0,
            "positionMargin": 0.0, "multiAssetsMargin": False,
        })
        rows = [{
            "symbol": "",
            "incomeType": "TRANSFER",
            "income": "19.68204603",
            "asset": "USDT",
            "info": "",
            "time": 1000,
            "tranId": 77,
            "tradeId": "",
        }]

        with patch(
            "bot.binance_accounting_evidence.collect_income",
            AsyncMock(return_value=rows),
        ), patch(
            "bot.cash_flow_ledger.db.load_key_value",
            AsyncMock(return_value=None),
        ), patch(
            "bot.cash_flow_ledger.save_key_values_atomic_cas",
            AsyncMock(return_value=True),
        ) as save, patch(
            "bot.capital_flow_reconciliation.rebase_real_account_peak_for_external_flow",
            AsyncMock(),
        ) as rebase:
            result = await reconcile_external_capital_flows(
                client, SimpleNamespace(), 20.20, strict=True
            )

        self.assertEqual(result["applied"], 0)
        self.assertTrue(result["bootstrap"])
        self.assertEqual(result["authority"], "checkpoint_only")
        rebase.assert_not_awaited()
        items = dict(save.await_args.args[0])
        self.assertNotIn(DURABLE_EQUITY_PEAK_KEY, items)
        checkpoint = json.loads(items[BINANCE_LAST_FLOW_CURSOR_KEY])
        self.assertEqual(checkpoint["version"], 1)
        self.assertEqual(checkpoint["seen"], ["1000:77"])

    async def test_binance_new_transfer_after_checkpoint_fails_closed(self):
        client = DummyClient({})
        client._listen_key_request = AsyncMock(return_value={"listenKey": "lk"})
        client.get_account_state = AsyncMock(return_value={
            "equity": 18.20, "walletBalance": 18.20, "unrealisedPNL": 0.0,
            "positionMargin": 0.0, "multiAssetsMargin": False,
        })
        rows = [
            {
                "incomeType": "TRANSFER",
                "income": "19.68204603",
                "asset": "USDT",
                "time": 1000,
                "tranId": 77,
            },
            {
                "incomeType": "TRANSFER",
                "income": "-2.0",
                "asset": "USDT",
                "time": 2000,
                "tranId": 78,
            },
        ]
        cursor = json.dumps({
            "version": 1,
            "seen": ["1000:77"],
            "observed_at_ms": 1500,
        })

        async def load(key, strict=False):
            return cursor if key == BINANCE_LAST_FLOW_CURSOR_KEY else None

        with patch(
            "bot.binance_accounting_evidence.collect_income",
            AsyncMock(return_value=rows),
        ), patch(
            "bot.cash_flow_ledger.db.load_key_value",
            AsyncMock(side_effect=load),
        ), patch(
            "bot.cash_flow_ledger.save_key_values_atomic_cas",
            AsyncMock(return_value=True),
        ) as save, patch(
            "bot.capital_flow_reconciliation.rebase_real_account_peak_for_external_flow",
            AsyncMock(),
        ) as rebase:
            # the transfer predates the evidence window: E- is unprovable
            with self.assertRaisesRegex(
                RuntimeError, "BINANCE_CAPITAL_FLOW_REBASE_UNCONFIRMED"
            ):
                await reconcile_external_capital_flows(
                    client, SimpleNamespace(), 18.20, strict=True
                )

        rebase.assert_not_awaited()
        written = [dict(call.args[0]) for call in save.await_args_list]
        self.assertTrue(written)
        for items in written:
            self.assertNotIn(DURABLE_EQUITY_PEAK_KEY, items)
        pending = json.loads(written[0]["risk:external_cash_flows:binance:ledger:v1"])["pending"]
        self.assertEqual([p["identity"] for p in pending], ["2000:78"])

    async def test_bootstrap_matching_latest_transferout_rebases_once_and_checkpoints(self):
        client = DummyClient({
            "dataList": [
                {
                    "time": 1000,
                    "type": "RealisedPNL",
                    "amount": -1.5,
                    "fee": 0,
                    "accountEquity": 34.8664,
                    "status": "Completed",
                    "offset": 10,
                    "currency": "USDT",
                },
                {
                    "time": 2000,
                    "type": "TransferOut",
                    "amount": -14.0,
                    "fee": 0,
                    "accountEquity": 20.8664,
                    "status": "Completed",
                    "offset": 11,
                    "currency": "USDT",
                },
            ]
        })
        risk = object()

        with patch("bot.capital_flow_reconciliation.db.load_key_value", AsyncMock(return_value=None)), \
             patch("bot.capital_flow_reconciliation.db.save_key_value", AsyncMock(return_value=True)) as save, \
             patch("bot.capital_flow_reconciliation.rebase_real_account_peak_for_external_flow", AsyncMock(return_value=22.9)) as rebase:
            result = await reconcile_external_capital_flows(client, risk, 20.8664, strict=True)

        self.assertEqual(result["applied"], 1)
        self.assertTrue(result["bootstrap"])
        rebase.assert_awaited_once_with(
            risk,
            20.8664,
            pre_flow_equity=34.8664,
            post_flow_equity=20.8664,
            flow_type="TransferOut",
            flow_amount=14.0,
            flow_offset="11",
            strict=True,
        )
        save.assert_awaited_once_with(LAST_FLOW_OFFSET_KEY, "11", strict=True)

    async def test_bootstrap_can_match_recent_transfer_even_if_not_newest_transfer(self):
        client = DummyClient({
            "dataList": [
                {
                    "time": 2000,
                    "type": "TransferOut",
                    "amount": -14.0,
                    "accountEquity": 20.8664,
                    "status": "Completed",
                    "offset": 11,
                },
                {
                    "time": 3000,
                    "type": "TransferIn",
                    "amount": 5.0,
                    "accountEquity": 84.9133,
                    "status": "Completed",
                    "offset": 12,
                },
            ]
        })
        risk = object()

        with patch("bot.capital_flow_reconciliation.db.load_key_value", AsyncMock(return_value=None)), \
             patch("bot.capital_flow_reconciliation.db.save_key_value", AsyncMock(return_value=True)) as save, \
             patch("bot.capital_flow_reconciliation.rebase_real_account_peak_for_external_flow", AsyncMock(return_value=22.9)) as rebase:
            result = await reconcile_external_capital_flows(client, risk, 20.8664, strict=True)

        self.assertEqual(result["applied"], 1)
        kwargs = rebase.await_args.kwargs
        self.assertEqual(kwargs["flow_offset"], "11")
        self.assertEqual(kwargs["flow_amount"], 14.0)
        save.assert_awaited_once_with(LAST_FLOW_OFFSET_KEY, "12", strict=True)

    async def test_bootstrap_equity_mismatch_does_not_rebase_but_checkpoints(self):
        client = DummyClient({
            "dataList": [{
                "time": 2000,
                "type": "TransferOut",
                "amount": -14.0,
                "fee": 0,
                "accountEquity": 20.8664,
                "status": "Completed",
                "offset": 11,
                "currency": "USDT",
            }]
        })
        risk = object()

        with patch("bot.capital_flow_reconciliation.db.load_key_value", AsyncMock(return_value=None)), \
             patch("bot.capital_flow_reconciliation.db.save_key_value", AsyncMock(return_value=True)) as save, \
             patch("bot.capital_flow_reconciliation.rebase_real_account_peak_for_external_flow", AsyncMock()) as rebase:
            result = await reconcile_external_capital_flows(client, risk, 18.0, strict=True)

        self.assertEqual(result["applied"], 0)
        rebase.assert_not_awaited()
        save.assert_awaited_once_with(LAST_FLOW_OFFSET_KEY, "11", strict=True)

    async def test_existing_cursor_processes_only_new_completed_transfers(self):
        client = DummyClient({
            "dataList": [
                {
                    "time": 1000,
                    "type": "TransferOut",
                    "amount": -2.0,
                    "accountEquity": 98.0,
                    "status": "Completed",
                    "offset": 10,
                },
                {
                    "time": 2000,
                    "type": "TransferIn",
                    "amount": 10.0,
                    "accountEquity": 108.0,
                    "status": "Completed",
                    "offset": 11,
                },
                {
                    "time": 3000,
                    "type": "TransferOut",
                    "amount": -3.0,
                    "accountEquity": 105.0,
                    "status": "Pending",
                    "offset": 12,
                },
                {
                    "time": 4000,
                    "type": "RealisedPNL",
                    "amount": 4.0,
                    "accountEquity": 112.0,
                    "status": "Completed",
                    "offset": 13,
                },
            ]
        })
        risk = object()

        with patch("bot.capital_flow_reconciliation.db.load_key_value", AsyncMock(return_value="10")), \
             patch("bot.capital_flow_reconciliation.db.save_key_value", AsyncMock(return_value=True)) as save, \
             patch("bot.capital_flow_reconciliation.rebase_real_account_peak_for_external_flow", AsyncMock(return_value=120.0)) as rebase:
            result = await reconcile_external_capital_flows(client, risk, 108.0, strict=True)

        self.assertEqual(result["applied"], 1)
        rebase.assert_awaited_once()
        kwargs = rebase.await_args.kwargs
        self.assertEqual(kwargs["flow_type"], "TransferIn")
        self.assertEqual(kwargs["flow_amount"], 10.0)
        self.assertEqual(kwargs["pre_flow_equity"], 98.0)
        self.assertEqual(kwargs["post_flow_equity"], 108.0)
        save.assert_awaited_once_with(LAST_FLOW_OFFSET_KEY, "11", strict=True)

    async def test_no_completed_transfers_does_nothing(self):
        client = DummyClient({
            "dataList": [
                {
                    "time": 1000,
                    "type": "RealisedPNL",
                    "amount": 2.0,
                    "accountEquity": 102.0,
                    "status": "Completed",
                    "offset": 1,
                }
            ]
        })
        with patch("bot.capital_flow_reconciliation.db.load_key_value", AsyncMock()) as load:
            result = await reconcile_external_capital_flows(client, object(), 102.0, strict=True)
        self.assertEqual(result, {"applied": 0, "bootstrap": False})
        load.assert_not_awaited()

    async def test_malformed_cursor_fails_closed(self):
        client = DummyClient({
            "dataList": [{
                "time": 2000,
                "type": "TransferOut",
                "amount": -5.0,
                "accountEquity": 95.0,
                "status": "Completed",
                "offset": 11,
            }]
        })
        with patch("bot.capital_flow_reconciliation.db.load_key_value", AsyncMock(return_value="bad-offset")):
            with self.assertRaises(db.PersistenceError):
                await reconcile_external_capital_flows(client, object(), 95.0, strict=True)

    async def test_ledger_failure_fails_closed(self):
        client = DummyClient(None)
        with self.assertRaises(RuntimeError):
            await reconcile_external_capital_flows(client, object(), 95.0, strict=True)


if __name__ == "__main__":
    unittest.main()
