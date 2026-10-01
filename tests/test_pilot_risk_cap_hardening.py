import asyncio
import unittest

from bot import pilot_risk_cap_hardening as guard


class PilotRiskCapPureTests(unittest.TestCase):
    def test_target_used_when_below_risk_limit(self):
        self.assertEqual(
            guard._select_final_quantity(target_qty=10.0, risk_qty=15.0),
            10.0,
        )

    def test_risk_limit_overrides_50pct_target_when_lower(self):
        self.assertEqual(
            guard._select_final_quantity(target_qty=10.0, risk_qty=6.5),
            6.5,
        )

    def test_never_exceeds_either_cap(self):
        for target, risk in ((10.0, 20.0), (20.0, 10.0), (7.25, 7.25)):
            final = guard._select_final_quantity(target_qty=target, risk_qty=risk)
            self.assertLessEqual(final, target)
            self.assertLessEqual(final, risk)

    def test_invalid_inputs_fail_closed(self):
        self.assertEqual(guard._select_final_quantity(target_qty=0, risk_qty=10), 0.0)
        self.assertEqual(guard._select_final_quantity(target_qty=10, risk_qty=0), 0.0)
        self.assertEqual(guard._select_final_quantity(target_qty=float("nan"), risk_qty=10), 0.0)
        self.assertEqual(guard._select_final_quantity(target_qty=10, risk_qty=float("inf")), 0.0)


class _Pilot:
    enabled = True


class _Risk:
    def __init__(self, qty):
        self.qty = qty
        self.calls = []

    def size(self, symbol, entry, instruments, size_mult=1.0, open_positions=None):
        self.calls.append((symbol, entry, instruments, open_positions))
        return self.qty


class _Log:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def critical(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class PilotRiskCapInstallTests(unittest.TestCase):
    def test_live_pilot_hook_caps_target_with_risk_quantity(self):
        from bot import engine as engine_module

        original_module_minimum = engine_module.minimum_base_quantity

        class FakeEngine:
            _pilot_risk_cap_hardening_installed = False

            def __init__(self):
                self.paper_trade = False
                self.pilot = _Pilot()
                self.risk = _Risk(6.0)
                self.instruments = {"DOTUSDT": {"multiplier": 1}}
                self.positions = {}
                self.observed_qty = None

            async def _refresh_entry_balance(self):
                return True

            async def _open(self, sig):
                self.observed_qty = engine_module.minimum_base_quantity(
                    self.instruments["DOTUSDT"], 1.0
                )
                return self.observed_qty

        class Sig:
            symbol = "DOTUSDT"

        try:
            # Emulate the already-installed pilot 50%-notional hook returning 10.
            engine_module.minimum_base_quantity = lambda info, price: 10.0
            guard.install(FakeEngine, _Log())
            instance = FakeEngine()
            result = asyncio.run(instance._open(Sig()))

            self.assertEqual(result, 6.0)
            self.assertEqual(instance.observed_qty, 6.0)
            self.assertEqual(len(instance.risk.calls), 1)
            self.assertEqual(instance.risk.calls[0][0], "DOTUSDT")
        finally:
            engine_module.minimum_base_quantity = original_module_minimum


class _MarketClient:
    def __init__(self, ticker, book):
        self.ticker = ticker
        self.book = book
        self.place_calls = 0
        self.ticker_calls = 0
        self.book_calls = 0

    async def get_ticker(self, symbol):
        self.ticker_calls += 1
        return self.ticker

    async def get_orderbook(self, symbol, depth=20):
        self.book_calls += 1
        return self.book

    async def place_order(self, **kwargs):
        self.place_calls += 1
        return {"orderId": "should-only-exist-on-pass"}


class PilotRiskCapLiveParityTests(unittest.IsolatedAsyncioTestCase):
    async def _exercise(self, *, ticker, book):
        from bot import engine as engine_module

        original_module_minimum = engine_module.minimum_base_quantity

        class FakeEngine:
            _pilot_risk_cap_hardening_installed = False

            def __init__(self):
                self.paper_trade = False
                self.pilot = _Pilot()
                self.risk = _Risk(6.0)
                self.instruments = {"DOTUSDT": {"multiplier": 1.0}}
                self.positions = {}
                self.client = _MarketClient(ticker, book)

            async def _refresh_entry_balance(self):
                return True

            async def _open(self, sig):
                qty = engine_module.minimum_base_quantity(
                    self.instruments[sig.symbol], sig.entry
                )
                if not await self._refresh_entry_balance():
                    return None
                return await self.client.place_order(
                    symbol=sig.symbol,
                    side="Buy" if sig.direction == "LONG" else "Sell",
                    qty=qty,
                )

        class Sig:
            symbol = "DOTUSDT"
            entry = 100.0
            sl = 99.6  # Explicit stop required by final projected-loss contract.
            direction = "LONG"

        try:
            engine_module.minimum_base_quantity = lambda info, price: 10.0
            guard.install(FakeEngine, _Log())
            instance = FakeEngine()
            result = await instance._open(Sig())
            return instance, result
        finally:
            engine_module.minimum_base_quantity = original_module_minimum

    async def test_wide_spread_blocks_before_place_order(self):
        instance, result = await self._exercise(
            ticker={"bid": 99.0, "ask": 101.0, "lastPrice": 100.0},
            book={"b": [[99.0, 100]], "a": [[101.0, 100]]},
        )
        self.assertIsNone(result)
        self.assertEqual(instance.client.place_calls, 0)
        self.assertEqual(instance.client.ticker_calls, 1)
        self.assertEqual(instance.client.book_calls, 1)

    async def test_insufficient_depth_blocks_before_place_order(self):
        instance, result = await self._exercise(
            ticker={"bid": 99.98, "ask": 100.02, "lastPrice": 100.0},
            # final qty=6, depth=1 => 0.166x, below 3x requirement
            book={"b": [[99.98, 1]], "a": [[100.02, 1]]},
        )
        self.assertIsNone(result)
        self.assertEqual(instance.client.place_calls, 0)

    async def test_signal_price_drift_blocks_before_place_order(self):
        instance, result = await self._exercise(
            ticker={"bid": 100.95, "ask": 101.0, "lastPrice": 100.98},
            book={"b": [[100.95, 100]], "a": [[101.0, 100]]},
        )
        self.assertIsNone(result)
        self.assertEqual(instance.client.place_calls, 0)

    async def test_good_market_can_reach_normal_place_order_path(self):
        instance, result = await self._exercise(
            ticker={"bid": 99.98, "ask": 100.02, "lastPrice": 100.0},
            book={"b": [[99.98, 100]], "a": [[100.02, 100]]},
        )
        self.assertEqual(result["orderId"], "should-only-exist-on-pass")
        self.assertEqual(instance.client.place_calls, 1)


class PilotRiskCapEngineOrderRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def _exercise_engine_order(self, *, ticker, book):
        """Mirror the real engine order: refresh -> size -> final refresh -> dispatch."""
        from bot import engine as engine_module

        original_module_minimum = engine_module.minimum_base_quantity

        class FakeEngine:
            _pilot_risk_cap_hardening_installed = False

            def __init__(self):
                self.paper_trade = False
                self.pilot = _Pilot()
                self.risk = _Risk(6.0)
                self.instruments = {"ADAUSDT": {"multiplier": 1.0}}
                self.positions = {}
                self.client = _MarketClient(ticker, book)
                self.refresh_calls = 0

            async def _refresh_entry_balance(self):
                self.refresh_calls += 1
                return True

            async def _open(self, sig):
                # This first refresh is present in the production engine before
                # minimum_base_quantity() has created the final quantity.
                if not await self._refresh_entry_balance():
                    return "blocked_pre_sizing"

                qty = engine_module.minimum_base_quantity(
                    self.instruments[sig.symbol], sig.entry
                )

                # This is the final pre-dispatch refresh where the market guard
                # must actually run against the computed quantity.
                if not await self._refresh_entry_balance():
                    return "blocked_final"

                return await self.client.place_order(
                    symbol=sig.symbol,
                    side="Sell",
                    qty=qty,
                )

        class Sig:
            symbol = "ADAUSDT"
            entry = 0.20461
            sl = 0.2054  # Costs plus stop remain within 50% entry margin.
            direction = "SHORT"

        try:
            engine_module.minimum_base_quantity = lambda info, price: 10.0
            guard.install(FakeEngine, _Log())
            instance = FakeEngine()
            result = await instance._open(Sig())
            return instance, result
        finally:
            engine_module.minimum_base_quantity = original_module_minimum

    async def test_pre_sizing_refresh_does_not_false_block_valid_live_candidate(self):
        instance, result = await self._exercise_engine_order(
            ticker={"bid": 0.20460, "ask": 0.20462, "lastPrice": 0.20461},
            book={"b": [[0.20460, 100]], "a": [[0.20462, 100]]},
        )
        self.assertIsInstance(result, dict)
        self.assertEqual(result["orderId"], "should-only-exist-on-pass")
        self.assertEqual(instance.refresh_calls, 2)
        # Market quality is fetched only at the final, post-sizing boundary.
        self.assertEqual(instance.client.ticker_calls, 1)
        self.assertEqual(instance.client.book_calls, 1)
        self.assertEqual(instance.client.place_calls, 1)

    async def test_bad_market_still_blocks_at_final_post_sizing_boundary(self):
        instance, result = await self._exercise_engine_order(
            ticker={"bid": 0.20200, "ask": 0.20700, "lastPrice": 0.20461},
            book={"b": [[0.20200, 100]], "a": [[0.20700, 100]]},
        )
        self.assertEqual(result, "blocked_final")
        self.assertEqual(instance.refresh_calls, 2)
        self.assertEqual(instance.client.ticker_calls, 1)
        self.assertEqual(instance.client.book_calls, 1)
        self.assertEqual(instance.client.place_calls, 0)


if __name__ == "__main__":
    unittest.main()
