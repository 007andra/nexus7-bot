import asyncio
from types import SimpleNamespace

from bot import partial_tp_execution_hardening as hardening


class Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def critical(self, *a, **k): pass


class Client:
    def __init__(self):
        self.posts = []
        self.fill = {"filled": True, "timed_out": False}
        self.positions = []
        self.be_ok = True

    async def _post(self, endpoint, body=None, *args, **kwargs):
        self.posts.append((endpoint, body))
        return body or {}

    async def place_order(self, **kwargs):
        # Exercise the installed _post wrapper with the legacy payload shape.
        await self._post("/api/v1/orders", {
            "size": "5", "reduceOnly": True, "closeOrder": True
        })
        return {"orderId": "partial-1"}

    async def wait_for_fill(self, order_id, timeout_s=8.0):
        return self.fill

    async def get_positions(self):
        return self.positions

    async def set_sl(self, symbol, sl):
        return self.be_ok


class Engine:
    def __init__(self, client):
        self.client = client
        self.positions = {}
        self.instruments = {"LTCUSDT": {"multiplier": 0.1}}
        self._unprotected_symbols = set()

    def _contracts_to_base_qty(self, symbol, contracts):
        return float(contracts) * 0.1

    async def _reconcile_exchange_positions(self, only_symbol=None):
        return []


hardening.install(Engine, Client, 0.0006, Log())


def pos():
    return SimpleNamespace(
        tp1_hit=False, current_price=101.1, entry=100.0, sl=99.0, initial_sl=99.0,
        direction="LONG", qty_original=10.0, qty=10.0, trailing_sl=99.0,
    )


def test_sized_reduce_only_removes_close_order():
    c = Client()
    asyncio.run(c._post("/api/v1/orders", {
        "size": "5", "reduceOnly": True, "closeOrder": True
    }))
    body = c.posts[-1][1]
    assert body["reduceOnly"] is True
    assert body["size"] == "5"
    assert "closeOrder" not in body


def test_unconfirmed_fill_never_mutates_local_position():
    c = Client()
    c.fill = {"filled": False, "timed_out": True}
    e = Engine(c)
    p = pos()
    e.positions["LTCUSDT"] = p
    asyncio.run(e._manage_partial_tp())
    assert p.tp1_hit is False
    assert p.qty == 10.0
    assert p.sl == 99.0


def test_flat_after_confirmed_partial_skips_bogus_be_move():
    c = Client()
    c.positions = []
    e = Engine(c)
    p = pos()
    e.positions["LTCUSDT"] = p
    asyncio.run(e._manage_partial_tp())
    assert p.tp1_hit is True
    assert p.sl == 99.0
    assert p.qty == 10.0


def test_be_failure_keeps_real_local_sl_and_blocks_entries():
    c = Client()
    c.positions = [{"symbol": "LTCUSDT", "size": 50}]
    c.be_ok = False
    e = Engine(c)
    p = pos()
    e.positions["LTCUSDT"] = p
    asyncio.run(e._manage_partial_tp())
    assert p.tp1_hit is True
    assert p.qty == 5.0
    assert p.sl == 99.0
    assert p.trailing_sl == 99.0
    assert "LTCUSDT" in e._unprotected_symbols


def test_confirmed_fill_and_be_use_exchange_remaining_quantity():
    c = Client()
    c.positions = [{"symbol": "LTCUSDT", "size": 50}]
    e = Engine(c)
    p = pos()
    e.positions["LTCUSDT"] = p
    asyncio.run(e._manage_partial_tp())
    assert p.tp1_hit is True
    assert p.qty == 5.0
    assert p.sl == 100.0
    assert p.trailing_sl == 100.0
    assert not e._unprotected_symbols
