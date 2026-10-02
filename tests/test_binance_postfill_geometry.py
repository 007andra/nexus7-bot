"""F-013 (Binance USD-M) — post-fill geometry on the composed LIVE runtime.

Native protection is installed at the planned levels right after the ACK. The
engine must never shift the local SL/TP by the fill delta nor read the ticker as
a fill: local geometry == exchange protection, and any tightening required by
the RiskManagerV3 budget at the real fill is created on the exchange and read
back first (INV-POSTFILL-GEOMETRY-001 / INV-POSTFILL-RISK-001 /
INV-FILL-AUTHORITY-001).
"""
from unittest.mock import AsyncMock

from tests.test_binance_cross_stress_dispatch_proof import DispatchProof as _Harness

from bot import postfill_risk_recheck as pfg  # noqa: E402


class BinancePostfillGeometryTests(_Harness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stops = []                    # exchange algo orders (normalized)
        self.rows = []
        self.fail_repair = False
        self.mark = None

        async def set_stops(symbol, sl=0, tp=0):
            if self.stops and self.fail_repair:
                return False
            if not self.rows:              # first call = protection at ACK: position now exists
                self.rows.append({"symbol": "ETHUSDT", "side": "Buy", "size": self.sized_qty,
                                  "sizeUnit": "BASE_ASSET", "entryPrice": self.fill,
                                  "markPrice": self.mark or self.fill})
            for kind, level in (("STOP_MARKET", sl), ("TAKE_PROFIT_MARKET", tp)):
                if level:
                    self.stops.append({"symbol": "ETHUSDT", "side": "sell", "type": kind,
                                       "stopPrice": str(level), "closeOrder": True,
                                       "isActive": True, "clientOid": f"bgx7-{len(self.stops)}"})
            return True
        self.client.set_position_stops = AsyncMock(side_effect=set_stops)
        self.client.get_positions = AsyncMock(side_effect=lambda: list(self.rows))
        self.client.get_stop_orders = AsyncMock(side_effect=lambda symbol: list(self.stops))

    async def _open(self, fill, *, status=True):
        self.fill = fill
        async def wait(order_id, *a, **k):
            if not status:
                return {"filled": True, "status": {}, "timed_out": False}
            q = self.sized_qty
            return {"filled": True, "timed_out": False,
                    "status": {"isActive": False, "dealSize": str(q), "dealValue": str(q * fill),
                               "filledSize": str(q)}}
        self.client.wait_for_fill = AsyncMock(side_effect=wait)
        self.client.get_cached_ticker = lambda s: {"lastPrice": fill}
        await self.engine._open(self.signal)
        return self.engine.positions.get("ETHUSDT")

    def sl_levels(self):
        return sorted(float(s["stopPrice"]) for s in self.stops if s["type"] == "STOP_MARKET")

    async def test_adverse_fill_tightens_on_exchange_and_local_follows(self):
        pos = await self._open(100.5)
        levels = self.sl_levels()
        self.assertEqual(len(levels), 2, "original kept until replacement confirmed")
        self.assertEqual(levels[0], 99.5)
        self.assertEqual((pos.entry, pos.sl, pos.tp), (100.5, levels[1], 104.0), "TP never shifted")
        self.assertLessEqual(pos.entry - pos.sl, 0.5 + 1e-9, "distance never grows")
        ok, metrics = self.engine.risk.validate_fresh_executable_risk("ETHUSDT", 100.5, pos.qty)
        per_unit = (pos.entry - pos.sl) + 100.5 * (2 * metrics["fee_rate_per_side"] + metrics["slippage_pct"])
        self.assertLessEqual(pos.qty * per_unit, metrics["risk_budget"] * (1 + 1e-6),
                             "V3 budget honoured at the REAL fill with the ACTIVE stop")

    async def test_favorable_fill_keeps_exchange_stop_and_tp(self):
        pos = await self._open(99.6)
        self.assertEqual(self.sl_levels(), [99.5])
        self.assertEqual((pos.entry, pos.sl, pos.tp), (99.6, 99.5, 104.0))

    async def test_ticker_is_never_a_fill(self):
        pos = await self._open(100.5, status=False)
        self.assertEqual((pos.entry, pos.sl, pos.tp), (100.0, 99.5, 104.0), "nothing shifted")
        self.assertEqual(self.sl_levels(), [99.5])

    async def test_failed_repair_keeps_exchange_truth(self):
        self.fail_repair = True
        pos = await self._open(100.5)
        self.assertEqual(self.sl_levels(), [99.5])
        self.assertEqual(pos.sl, 99.5, "local follows the stop actually on the exchange")

    async def test_invalid_trigger_side_never_sends(self):
        self.mark = 99.9                        # market already below the tightened stop
        pos = await self._open(100.5)
        self.assertEqual(self.sl_levels(), [99.5])
        self.assertEqual(pos.sl, 99.5)

    async def test_budget_stop_wins_when_tighter_than_distance(self):
        geo_metrics = {"risk_budget": 3.0, "fee_rate_per_side": 0.0005, "slippage_pct": 0.0005}
        stop = pfg.candidate_stop("LONG", 100.5, 13.888, 100.0, 99.5, geo_metrics, 0.01)
        allowed = 3.0 / 13.888 - 100.5 * 0.0015
        self.assertGreater(stop, 100.0)
        self.assertAlmostEqual(stop, pfg.quantize_protective(100.5 - allowed, 0.01, "LONG"))
        self.assertLessEqual(13.888 * (100.5 - stop + 100.5 * 0.0015), 3.0 + 1e-9)

    def test_fill_authority_rejects_unproven_sources(self):
        self.assertIsNone(pfg.fill_price({}))
        self.assertIsNone(pfg.fill_price({"isActive": True, "dealSize": "1", "dealValue": "100"}))
        self.assertIsNone(pfg.fill_price({"_synthetic": True, "dealSize": "1", "dealValue": "100"}))
        self.assertEqual(pfg.fill_price({"isActive": False, "dealSize": "2", "dealValue": "201"}), 100.5)


for _name in [n for n in dir(_Harness) if n.startswith("test_")]:
    if _name not in BinancePostfillGeometryTests.__dict__:
        setattr(BinancePostfillGeometryTests, _name, None)
del _Harness
