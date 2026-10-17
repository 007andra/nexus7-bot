"""P1-2 (full audit 2026-09-26): Binance base-asset lots must be sizable.

Metadata is produced by the real BinanceClient.load_instruments mapping, so the
test fails if either the mapping or bot/quantity.py drifts again. Previously
every fractional-lot symbol (BTC 0.001, ETH 0.001, DOT 0.1, ...) raised
"KuCoin lotSize/minQty must be integer contracts" and sized to 0.
"""
import asyncio
import math
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from bot import quantity as q
from bot.config import cfg

EXCHANGE_INFO = {"symbols": [
    {"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING",
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": tick},
                 {"filterType": "MARKET_LOT_SIZE", "stepSize": step, "minQty": mq},
                 {"filterType": "MIN_NOTIONAL", "notional": mn}]}
    for s, tick, step, mq, mn in (
        ("BTCUSDT", "0.10", "0.001", "0.001", "100"),
        ("ETHUSDT", "0.01", "0.001", "0.001", "20"),
        ("DOTUSDT", "0.001", "0.1", "0.1", "5"),
        ("DOGEUSDT", "0.00001", "1", "1", "5"),
        ("ATOMUSDT", "0.001", "0.01", "0.01", "5"),
    )
]}


def binance_instruments():
    from bot import binance
    client = binance.BinanceClient()
    client._time_synced = True

    async def fake_get(endpoint, params=None, auth=False):
        assert endpoint == "/fapi/v1/exchangeInfo"
        return EXCHANGE_INFO

    with patch.object(client, "_get", side_effect=fake_get):
        return asyncio.run(client.load_instruments())


class BaseAssetQuantityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inst = binance_instruments()

    def test_rules_express_step_units(self):
        mult, lot, minimum, notional = q.quantity_rules(self.inst["BTCUSDT"])
        self.assertEqual((mult, lot, minimum, notional),
                         (Decimal("0.001"), Decimal(1), Decimal(1), Decimal("100")))
        mult, lot, minimum, _ = q.quantity_rules(self.inst["DOTUSDT"])
        self.assertEqual((mult, lot, minimum), (Decimal("0.1"), Decimal(1), Decimal(1)))

    def test_minimum_base_quantity_respects_min_qty_and_min_notional(self):
        self.assertAlmostEqual(q.minimum_base_quantity(self.inst["BTCUSDT"], 60000.0), 0.002)  # 100/60000 -> 0.00167 -> 0.002
        self.assertAlmostEqual(q.minimum_base_quantity(self.inst["DOTUSDT"], 4.0), 1.3)        # 5/4 = 1.25 -> 1.3
        self.assertAlmostEqual(q.minimum_base_quantity(self.inst["DOGEUSDT"], 0.2), 25.0)

    def test_validate_base_quantity_on_step_grid(self):
        q.validate_base_quantity(0.003, self.inst["BTCUSDT"], 60000.0)
        q.validate_base_quantity(2.5, self.inst["DOTUSDT"], 4.0)
        with self.assertRaises(ValueError):
            q.validate_base_quantity(0.0035, self.inst["BTCUSDT"], 60000.0)   # off-step
        with self.assertRaises(ValueError):
            q.validate_base_quantity(0.001, self.inst["BTCUSDT"], 60000.0)    # below minNotional

    def test_contract_conversions_still_refuse_base_asset(self):
        for fn in (q.contracts_to_base, q.base_to_contracts):
            with self.assertRaises(ValueError):
                fn(1, self.inst["BTCUSDT"])

    def test_contract_venue_semantics_unchanged(self):
        kucoin = {"multiplier": "0.001", "lotSize": "1", "minQty": "1", "minNotional": "0"}
        self.assertEqual(q.contracts_to_base(5, kucoin), 0.005)
        self.assertEqual(q.base_to_contracts(0.0059, kucoin), 5)
        with self.assertRaises(ValueError):
            q.quantity_rules({"multiplier": "0.001", "lotSize": "0.5", "minQty": "1"})

    def test_min_qty_not_multiple_of_step_fails_closed(self):
        bad = dict(self.inst["DOTUSDT"], minQty=0.15)
        with self.assertRaises(ValueError):
            q.quantity_rules(bad)


class BinanceStopRiskSizingEndToEnd(unittest.TestCase):
    """Real RiskManagerV3 adapter + final sizing hook on Binance metadata."""

    def setUp(self):
        self.old = cfg.LEVERAGE
        cfg.LEVERAGE = 50
        self.inst = binance_instruments()

    def tearDown(self):
        cfg.LEVERAGE = self.old

    def test_fractional_lot_symbols_now_size_within_risk_budget(self):
        from bot import final_sizing_invariants as fsi
        from bot import pilot_risk_cap_hardening as cap
        from bot.professional_risk import CapitalState
        from bot.professional_risk_adapter import ProfessionalRiskAdapter

        for symbol, entry, equity in (("ETHUSDT", 2500.0, 100.0), ("DOTUSDT", 4.0, 10.0),
                                      ("ATOMUSDT", 4.0, 10.0)):
            with self.subTest(symbol=symbol):
                stop = entry * 0.996
                adapter = ProfessionalRiskAdapter(SimpleNamespace(balance=equity, balance_confirmed=True, _ready=True))
                adapter.set_plan(symbol=symbol, entry=entry, stop=stop, risk_pct=0.01)
                adapter.update_capital(CapitalState(equity=equity, available_collateral=equity))
                risk_qty = adapter.size(symbol, entry, self.inst)
                self.assertGreater(risk_qty, 0.0)

                module = SimpleNamespace(minimum_base_quantity=lambda i, p: 0.0,
                                         _final_sizing_invariants_installed=False)
                fsi.install(module, cap, SimpleNamespace(**{k: (lambda *a, **kw: None) for k in
                                                            ("info", "warning", "critical", "error", "debug")}))
                engine = SimpleNamespace(paper_trade=False, pilot=SimpleNamespace(enabled=True),
                                         _pilot_available_balance=equity, risk=adapter,
                                         instruments=self.inst, positions={})
                tokens = (cap._PILOT_ENGINE.set(engine), cap._PILOT_SYMBOL.set(symbol),
                          cap._PILOT_FINAL_QTY.set(None),
                          cap._PILOT_SIGNAL.set(SimpleNamespace(sl=stop, direction="LONG")))
                try:
                    final = module.minimum_base_quantity(self.inst[symbol], entry)
                finally:
                    cap._PILOT_SIGNAL.reset(tokens[3]); cap._PILOT_FINAL_QTY.reset(tokens[2])
                    cap._PILOT_SYMBOL.reset(tokens[1]); cap._PILOT_ENGINE.reset(tokens[0])
                self.assertGreater(final, 0.0)
                self.assertLessEqual(final, risk_qty + 1e-12)
                q.validate_base_quantity(final, self.inst[symbol], entry)
                plan = adapter._plans[symbol]
                loss = final * (abs(entry - stop) + entry * 2 * plan.fee_rate() + entry * plan.slippage())
                self.assertLessEqual(loss, equity * 0.01 * 1.000001)
                step = float(self.inst[symbol]["qtyStep"])
                self.assertTrue(math.isclose(round(final / step), final / step, abs_tol=1e-9))


if __name__ == "__main__":
    unittest.main()
