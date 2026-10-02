import io
import unittest
import zipfile

from bot.binance_research_data import (
    archive_sha256,
    agg_trade_gap_diagnostics,
    daily_agg_trades_url,
    daily_book_depth_url,
    daily_metrics_url,
    monthly_funding_url,
    monthly_kline_url,
    parse_agg_trades_archive,
    parse_checksum_text,
    parse_funding_archive,
    parse_kline_archive,
    verify_archive_checksum,
)


def _zip_csv(name, text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, text)
    return buf.getvalue()


class BinanceResearchDataTests(unittest.TestCase):
    def test_kline_url_is_usdm_monthly_archive(self):
        url = monthly_kline_url("btcusdt", "15m", 2026, 1)
        self.assertIn("/futures/um/monthly/klines/BTCUSDT/15m/", url)
        self.assertTrue(url.endswith("BTCUSDT-15m-2026-01.zip"))

    def test_funding_url_is_usdm_monthly_archive(self):
        url = monthly_funding_url("ETHUSDT", 2026, 2)
        self.assertTrue(url.endswith("ETHUSDT-fundingRate-2026-02.zip"))

    def test_daily_agg_trades_url_is_usdm_archive(self):
        url = daily_agg_trades_url("btcusdt", "2026-08-31")
        self.assertTrue(url.endswith("BTCUSDT-aggTrades-2026-08-31.zip"))
        self.assertIn("/futures/um/daily/aggTrades/BTCUSDT/", url)

    def test_daily_context_urls_are_usdm_archives(self):
        metrics = daily_metrics_url("btcusdt", "2026-08-31")
        depth = daily_book_depth_url("BTCUSDT", "2026-08-31")
        self.assertTrue(metrics.endswith("BTCUSDT-metrics-2026-08-31.zip"))
        self.assertIn("/futures/um/daily/metrics/BTCUSDT/", metrics)
        self.assertTrue(depth.endswith("BTCUSDT-bookDepth-2026-08-31.zip"))
        self.assertIn("/futures/um/daily/bookDepth/BTCUSDT/", depth)

    def test_parse_kline_archive(self):
        payload = _zip_csv(
            "x.csv",
            "open_time,open,high,low,close,volume,close_time\n"
            "1000,100,110,90,105,12,1999\n"
            "2000,105,115,100,112,9,2999\n",
        )
        rows = parse_kline_archive(payload)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].as_project_candle()["c"], 105.0)

    def test_parse_funding_archive(self):
        payload = _zip_csv(
            "x.csv",
            "calc_time,funding_interval_hours,last_funding_rate\n"
            "1000,8,0.0001\n2000,8,-0.0002\n",
        )
        rows = parse_funding_archive(payload)
        self.assertEqual(rows[1].funding_rate, -0.0002)

    def test_checksum_sidecar_is_filename_bound(self):
        payload = b"archive-bytes"
        digest = archive_sha256(payload)
        text = f"{digest}  BTCUSDT-15m-2026-01.zip\n"
        self.assertEqual(
            parse_checksum_text(
                text, expected_filename="BTCUSDT-15m-2026-01.zip"
            ),
            digest,
        )
        self.assertEqual(
            verify_archive_checksum(
                payload, text,
                expected_filename="BTCUSDT-15m-2026-01.zip",
            ),
            digest,
        )
        with self.assertRaises(ValueError):
            parse_checksum_text(text, expected_filename="ETHUSDT.zip")

    def test_parse_agg_trades_and_report_gaps_without_interpolation(self):
        payload = _zip_csv(
            "x.csv",
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "timestamp,buyer_was_maker\n"
            "10,100.0,2.0,100,101,1700000000000,false\n"
            "12,99.0,1.5,103,103,1700000000100,true\n",
        )
        rows = parse_agg_trades_archive(payload)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].aggressor_side, "BUY")
        self.assertEqual(rows[1].aggressor_side, "SELL")
        self.assertEqual(rows[0].notional, 200.0)
        gaps = agg_trade_gap_diagnostics(rows)
        self.assertEqual(gaps["aggregate_id_gap_events"], 1)
        self.assertEqual(gaps["missing_aggregate_ids"], 1)
        self.assertEqual(gaps["underlying_id_gap_events"], 1)
        self.assertEqual(gaps["missing_underlying_ids"], 1)
        self.assertFalse(gaps["interpolation_applied"])

    def test_invalid_symbol_is_rejected(self):
        with self.assertRaises(ValueError):
            monthly_kline_url("../BTC", "15m", 2026, 1)


if __name__ == "__main__":
    unittest.main()
