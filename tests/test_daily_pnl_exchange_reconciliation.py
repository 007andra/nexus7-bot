import unittest
from datetime import datetime, timezone

from bot import durable_daily_pnl as pnl


class DailyPnlExchangeReconciliationTests(unittest.TestCase):
    def test_confirmed_kucoin_pnl_adjusts_estimate_exactly_once(self):
        rows = {
            "a" * 64: {
                "pnl": -2.33165996,
                "source": "ESTIMATED_LOCAL_MARK_AND_FEE_RATE",
                "closed_at": "2026-09-14T00:33:47+00:00",
            }
        }
        row = {
            "closeId": "200000000043135568",
            "symbol": "LINKUSDTM",
            "closeTime": int(datetime(2026, 9, 14, 0, 33, 41, 700000, tzinfo=timezone.utc).timestamp() * 1000),
            "pnl": "-3.0559949",
        }

        result = pnl._confirmed_adjustment(rows, row)
        self.assertIsNotNone(result)
        token, value = result
        self.assertEqual(len(token), 64)
        self.assertAlmostEqual(value["estimated_pnl"], -2.33165996)
        self.assertAlmostEqual(value["confirmed_pnl"], -3.0559949)
        self.assertAlmostEqual(value["pnl"], -0.72433494)
        self.assertEqual(value["source"], "KUCOIN_RECONCILIATION_ADJUSTMENT")
        self.assertEqual(value["symbol"], "LINKUSDT")

        total = sum(v["pnl"] for v in rows.values()) + value["pnl"]
        self.assertAlmostEqual(total, -3.0559949)
        again = pnl._confirmed_adjustment(rows, row)
        self.assertEqual(result, again)

    def test_binance_confirmed_pnl_uses_exchange_specific_adjustment_source(self):
        rows = {
            "a" * 64: {
                "pnl": -3.417347,
                "source": "ESTIMATED_LOCAL_MARK_AND_FEE_RATE",
                "closed_at": "2026-10-01T07:25:53+00:00",
                "symbol": "AAVEUSDT",
                "opening_order_id": "27098648014",
            }
        }
        row = {
            "closeId": "BINANCE:27098648014:27098929423,27100972276",
            "symbol": "AAVEUSDT",
            "closeTime": int(
                datetime(2026, 10, 1, 7, 25, 55, tzinfo=timezone.utc).timestamp()
                * 1000
            ),
            "pnl": "0.809514",
        }
        receipt = {
            "opening_order_ids": ["27098648014"],
            "exchange": "BINANCE",
        }
        result = pnl._confirmed_adjustment(rows, row, receipt)
        self.assertIsNotNone(result)
        _, value = result
        self.assertEqual(value["source"], "BINANCE_RECONCILIATION_ADJUSTMENT")
        self.assertEqual(value["opening_order_id"], "27098648014")
        self.assertAlmostEqual(value["pnl"], 4.226861)
        self.assertAlmostEqual(
            rows["a" * 64]["pnl"] + value["pnl"],
            0.809514,
        )

    def test_opening_order_lineage_disambiguates_same_symbol_estimates(self):
        rows = {
            "a" * 64: {
                "pnl": -1.0,
                "source": "ESTIMATED_LOCAL_MARK_AND_FEE_RATE",
                "closed_at": "2026-09-14T15:00:40+00:00",
                "symbol": "ATOMUSDT",
                "opening_order_id": "111",
            },
            "b" * 64: {
                "pnl": -2.0,
                "source": "ESTIMATED_LOCAL_MARK_AND_FEE_RATE",
                "closed_at": "2026-09-14T15:00:42+00:00",
                "symbol": "ATOMUSDT",
                "opening_order_id": "222",
            },
        }
        row = {
            "closeId": "close-atom",
            "symbol": "ATOMUSDTM",
            "closeTime": int(datetime(2026, 9, 14, 15, 0, 41, tzinfo=timezone.utc).timestamp() * 1000),
            "pnl": "-2.5",
        }
        receipt = {"opening_order_ids": ["222"]}
        result = pnl._confirmed_adjustment(rows, row, receipt)
        self.assertIsNotNone(result)
        _, value = result
        self.assertEqual(value["estimated_event"], "b" * 64)
        self.assertEqual(value["opening_order_id"], "222")
        self.assertAlmostEqual(value["pnl"], -0.5)

    def test_exact_opening_order_lineage_ignores_close_time_drift(self):
        rows = {
            "a" * 64: {
                "pnl": -4.0,
                "source": "ESTIMATED_LOCAL_MARK_AND_FEE_RATE",
                "closed_at": "2026-09-14T15:00:00+00:00",
                "symbol": "NEARUSDT",
                "opening_order_id": "open-near-1",
            }
        }
        row = {
            "closeId": "close-near-late-index",
            "symbol": "NEARUSDTM",
            "closeTime": int(datetime(2026, 9, 14, 15, 8, 0, tzinfo=timezone.utc).timestamp() * 1000),
            "pnl": "-4.75",
        }
        result = pnl._confirmed_adjustment(
            rows, row, {"opening_order_ids": ["open-near-1"]}
        )
        self.assertIsNotNone(result)
        _, value = result
        self.assertEqual(value["opening_order_id"], "open-near-1")
        self.assertEqual(value["estimated_event"], "a" * 64)
        self.assertAlmostEqual(value["pnl"], -0.75)

    def test_contradictory_modern_lineage_never_falls_back_by_time(self):
        rows = {
            "a" * 64: {
                "pnl": -1.0,
                "source": "ESTIMATED_LOCAL_MARK_AND_FEE_RATE",
                "closed_at": "2026-09-14T15:00:41+00:00",
                "symbol": "ATOMUSDT",
                "opening_order_id": "111",
            }
        }
        row = {
            "closeId": "close-atom-2",
            "symbol": "ATOMUSDTM",
            "closeTime": int(datetime(2026, 9, 14, 15, 0, 41, tzinfo=timezone.utc).timestamp() * 1000),
            "pnl": "-2.0",
        }
        self.assertIsNone(pnl._confirmed_adjustment(rows, row, {"opening_order_ids": ["999"]}))

    def test_legacy_single_estimate_still_uses_unique_symbol_time_fallback(self):
        rows = {
            "a" * 64: {
                "pnl": -1.0,
                "source": "ESTIMATED_LOCAL_MARK_AND_FEE_RATE",
                "closed_at": "2026-09-14T15:00:41+00:00",
                "symbol": "ATOMUSDT",
            }
        }
        row = {
            "closeId": "legacy-close",
            "symbol": "ATOMUSDTM",
            "closeTime": int(datetime(2026, 9, 14, 15, 0, 41, tzinfo=timezone.utc).timestamp() * 1000),
            "pnl": "-1.5",
        }
        result = pnl._confirmed_adjustment(rows, row, {"opening_order_ids": ["222"]})
        self.assertIsNotNone(result)
        _, value = result
        self.assertNotIn("opening_order_id", value)
        self.assertAlmostEqual(value["pnl"], -0.5)

    def test_ambiguous_estimates_fail_closed_without_adjustment(self):
        rows = {
            "a" * 64: {
                "pnl": -1.0,
                "source": "ESTIMATED_LOCAL_MARK_AND_FEE_RATE",
                "closed_at": "2026-09-14T00:33:40+00:00",
            },
            "b" * 64: {
                "pnl": -2.0,
                "source": "ESTIMATED_LOCAL_MARK_AND_FEE_RATE",
                "closed_at": "2026-09-14T00:33:42+00:00",
            },
        }
        row = {
            "closeId": "close-1",
            "symbol": "LINKUSDTM",
            "closeTime": int(datetime(2026, 9, 14, 0, 33, 41, tzinfo=timezone.utc).timestamp() * 1000),
            "pnl": "-3.0",
        }
        self.assertIsNone(pnl._confirmed_adjustment(rows, row))

    def test_symbol_metadata_prevents_wrong_trade_match(self):
        rows = {
            "a" * 64: {
                "pnl": -1.0,
                "source": "ESTIMATED_LOCAL_MARK_AND_FEE_RATE",
                "closed_at": "2026-09-14T00:33:41+00:00",
                "symbol": "ETHUSDT",
            }
        }
        row = {
            "closeId": "close-2",
            "symbol": "LINKUSDTM",
            "closeTime": int(datetime(2026, 9, 14, 0, 33, 41, tzinfo=timezone.utc).timestamp() * 1000),
            "pnl": "-3.0",
        }
        self.assertIsNone(pnl._confirmed_adjustment(rows, row))

    def test_non_estimated_event_is_never_rewritten(self):
        rows = {
            "a" * 64: {
                "pnl": -1.0,
                "source": "KUCOIN_RECONCILED_FILLS",
                "closed_at": "2026-09-14T00:33:41+00:00",
                "symbol": "LINKUSDT",
            }
        }
        row = {
            "closeId": "close-3",
            "symbol": "LINKUSDTM",
            "closeTime": int(datetime(2026, 9, 14, 0, 33, 41, tzinfo=timezone.utc).timestamp() * 1000),
            "pnl": "-3.0",
        }
        self.assertIsNone(pnl._confirmed_adjustment(rows, row))


if __name__ == "__main__":
    unittest.main()
