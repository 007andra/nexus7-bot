import hashlib
import json
import unittest
from bot.binance_usdm_oos_exchange_filters import verify_exchange_filters

T = 1791504000000
PAYLOAD = {"symbols": [{"symbol": "SOLUSDT", "status": "TRADING", "filters": [
    {"filterType": "LOT_SIZE", "minQty": "0.1", "stepSize": "0.1"},
    {"filterType": "MARKET_LOT_SIZE", "minQty": "0.1", "stepSize": "0.1"},
    {"filterType": "MIN_NOTIONAL", "notional": "5"},
    {"filterType": "PRICE_FILTER", "tickSize": "0.01"}]}]}
RAW = json.dumps(PAYLOAD).encode()
SHA = hashlib.sha256(RAW).hexdigest()


def check(**kwargs):
    return verify_exchange_filters(raw=kwargs.pop("raw", RAW),
                                   sha256=kwargs.pop("sha256", SHA),
                                   symbol="SOLUSDT",
                                   acquired_ms=kwargs.pop("acquired_ms", T - 1000),
                                   decision_ms=T, **kwargs)


class TestExchangeFilters(unittest.TestCase):
    def test_parses_market_lot_and_notional(self):
        result = check()
        self.assertTrue(result["verified"])
        self.assertEqual(result["min_notional"], "5")
        self.assertTrue(result["historical_snapshot_claim_only"])

    def test_postdecision_snapshot_rejected(self):
        self.assertIn("HISTORICAL_FILTER_SNAPSHOT_MISSING",
                      check(acquired_ms=T + 1)["blockers"])

    def test_source_mutation_rejected(self):
        self.assertIn("EXCHANGE_INFO_SHA256_MISMATCH",
                      check(raw=RAW + b" ")["blockers"])

    def test_missing_filter_rejected(self):
        p = json.loads(RAW)
        p["symbols"][0]["filters"].pop()
        raw = json.dumps(p).encode()
        self.assertIn("FILTER_MISSING_PRICE_FILTER",
                      check(raw=raw, sha256=hashlib.sha256(raw).hexdigest())["blockers"])


if __name__ == "__main__":
    unittest.main()
