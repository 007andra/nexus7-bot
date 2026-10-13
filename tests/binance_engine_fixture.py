"""Engine fixture on the PRODUCTION venue: Binance USD-M, LIVE pilot path.

Mirrors how production runs: ``runtime_bootstrap`` installed, the real
``BinanceClient`` talking to a loopback fake exchange that fills MARKET orders
and stores protection, pilot enabled with its release/account gates, healthy
public and private streams, durable state restored into a disposable
PostgreSQL database. The REAL ``place_order`` runs (wrapped only to count
calls): execution ownership lease, durable dispatch provenance, the pilot
submission budget at the HTTP boundary and SL/TP installation are all
exercised. Nothing leaves 127.0.0.1.

Import this module before any ``bot`` module in a test file: it pins the
process environment the production venue reads at import time.
"""
import asyncio
import itertools
import os
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.update(
    EXCHANGE="binance", PAPER_TRADE="false",
    LIVE_TRADING_CONFIRMED="I_UNDERSTAND_THE_RISK",
    BINANCE_LIVE_MIGRATION_READY="true",
    BINANCE_API_KEY="offline-key", BINANCE_API_SECRET="offline-secret",
    BINANCE_SIGNING_METHOD="hmac", NEXUS_TELEGRAM="false",
    EXECUTION_CAPABILITY="LIVE",
    # Same seven release acknowledgements production runs with, so the
    # LIVE pilot runtime (not the validation SHADOW lock) is installed.
    REAL_TRADING_PILOT="true", PILOT_ACCOUNT_CONFIRMED="true",
    PILOT_RELEASE_APPROVED="I_APPROVE_TWO_LIVE_PILOT_ORDERS",
    VALIDATION_LOCK_RELEASE_APPROVED="I_APPROVE_CONTROLLED_LIVE_PILOT_EXECUTION",
    # Production pilot limits ([CONTROLLED_PILOT_RELEASE] startup log). Read
    # once at import by bot.pilot and copied by several modules, so they are
    # pinned here for every suite sharing this process.
    PILOT_MAX_NEW_ORDER_SUBMISSIONS="1", PILOT_MAX_CONCURRENT_POSITIONS="1",
)

from bot.runtime_bootstrap import install as _install_runtime  # noqa: E402

_install_runtime()

from bot import database as db  # noqa: E402
from bot import engine as E  # noqa: E402
from bot import nexus_persistence  # noqa: E402
from bot.binance import BinanceClient  # noqa: E402
from bot.config import cfg  # noqa: E402
from bot.nexus_runtime_engine import TradingEngine  # noqa: E402  (main.py's class)
from bot.nexus_types import NexusDecision  # noqa: E402
from bot.pilot import PILOT_RELEASE_TOKEN  # noqa: E402
from bot.private_stream_health import PRIVATE_ROUTE  # noqa: E402
from bot.strategy import Signal  # noqa: E402
from tests.binance_fake_exchange import FakeBinance, default_state  # noqa: E402

SYMBOLS = ("TESTUSDT", "OTHERUSDT")
_DB_SEQ = itertools.count()


def approval(symbol="TESTUSDT", allowed=True):
    return NexusDecision(symbol=symbol, decision="LONG", execution_allowed=allowed,
                         confidence=80., setup_quality=80., entry=100.,
                         stop_loss=99., take_profit=103., expected_value=1.,
                         risk_reward=3., reasoning=["offline fixture"])


def instrument(step=0.001, min_qty=0.001, min_notional=5.0, tick=0.01):
    # Same shape BinanceClient.load_instruments produces.
    return {"minQty": min_qty, "lotSize": min_qty, "qtyStep": step,
            "tickSize": tick, "multiplier": 1.0, "minBaseQty": min_qty,
            "minNotional": min_notional, "maxLeverage": 0.0,
            "quantityUnit": "BASE_ASSET"}


class BinanceEngineFixture(unittest.IsolatedAsyncioTestCase):
    async def _isolated_database(self):
        """Fresh PostgreSQL database per test: durable state cannot leak.

        The LIVE path persists HWM, cash-flow evidence and durable intent
        before dispatch, exactly as production. Missing PostgreSQL FAILS the
        test instead of skipping it, so the suite never reports false coverage.
        """
        import asyncpg

        dsn = os.environ.get("TEST_POSTGRES_DSN", "")
        if not dsn.startswith("postgresql"):
            self.fail("TEST_POSTGRES_DSN is required for Binance LIVE-path tests")
        self.db_name = f"nexus_fx_{os.getpid()}_{next(_DB_SEQ)}"
        self.admin_dsn = dsn
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute(f'CREATE DATABASE "{self.db_name}"')
        finally:
            await admin.close()
        self.saved_db = (db._conn, db._is_pg, db._io_lock, db.DATABASE_URL)
        db._conn, db._is_pg, db._io_lock = None, False, asyncio.Lock()
        db.DATABASE_URL = dsn.rsplit("/", 1)[0] + "/" + self.db_name
        await db.init()
        # NEXUS decision history keeps its own connection (read from the env
        # at import); point it at the same disposable database.
        self.saved_nexus = (nexus_persistence._conn, nexus_persistence._is_pg,
                            nexus_persistence._DB_URL)
        nexus_persistence._conn, nexus_persistence._is_pg = None, False
        nexus_persistence._DB_URL = db.DATABASE_URL

    async def _drop_database(self):
        import asyncpg

        await nexus_persistence.close()
        (nexus_persistence._conn, nexus_persistence._is_pg,
         nexus_persistence._DB_URL) = self.saved_nexus
        if db._conn is not None:
            try:
                await asyncio.wait_for(db._conn.close(), 5)
            except Exception:
                db._conn.terminate()
        db._conn, db._is_pg, db._io_lock, db.DATABASE_URL = self.saved_db
        admin = await asyncpg.connect(self.admin_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{self.db_name}" WITH (FORCE)')
        finally:
            await admin.close()

    async def asyncSetUp(self):
        self.patches = []
        await self._isolated_database()
        # Mid 99.99: executable ask (~99.999) does not exceed the 100.0 signal
        # entry, so the fresh risk recheck sees no adverse drift. Tests that
        # need adverse drift move self.fake.state["prices"].
        self.fake = FakeBinance(default_state(SYMBOLS, price=99.99))
        self.fake.state["leverage"] = int(cfg.LEVERAGE)
        base = await self.fake.start()
        self.replace("bot.binance.REST_BASE", base)
        self.replace("bot.engine._NEXUS_ENABLED", True)
        self.replace("bot.pilot.PILOT_ENABLED", True)
        self.replace("bot.engine.notify", AsyncMock())
        self.replace("bot.engine.notify_nexus", AsyncMock())
        self.replace("bot.engine.db.save_signal", AsyncMock())
        self.replace("bot.engine.db.save_snapshot", AsyncMock())
        self.replace("bot.engine.scoring.calculate",
                     AsyncMock(return_value={"aprovado": True, "total": 90}))
        self.env = patch.dict("os.environ", PILOT_ACCOUNT_CONFIRMED="true",
                              PILOT_RELEASE_APPROVED=PILOT_RELEASE_TOKEN)
        self.env.start()

        self.client = BinanceClient()
        self.engine = TradingEngine(self.client)
        self.client._engine = self.engine
        self.engine.risk.init(100.)
        self.engine.viable_symbols = list(SYMBOLS)
        self.engine.instruments = {s: instrument() for s in SYMBOLS}
        self.client._instruments = self.engine.instruments
        self.client._order_registry = self.engine.orders
        self.engine._pilot_available_balance = 100.0
        self.client.get_cached_klines = lambda *a, **k: [
            {"c": 100., "h": 101., "l": 99., "v": 1000.}] * 200
        self.fake.state["accept_mutations"] = True
        from bot import durable_execution
        # run() enables durable enforcement right after db.init(); without it
        # the before_dispatch persistence of the SUBMITTING intent is skipped.
        self.engine._durable_state_enforced = True
        await durable_execution.restore_engine_state(self.engine)
        # Startup facts production establishes in _connect.
        self.engine.connected = True
        self.engine._financial_state_sane = True
        self.engine._initial_reconciliation_complete = True
        self.engine._market_data_ready = True
        self.engine._protection_system_ready = True
        self.client.place_order = AsyncMock(wraps=self.client.place_order)
        # Keep the production _nexus_validate (it registers the RiskManagerV3
        # stop plan); only the pure decision function is substituted.
        self.decide = self.replace("bot.nexus_ai.decide", Mock(return_value=approval()))
        self._healthy_streams()
        self.sig = Signal("TESTUSDT", "LONG", 100., 99., 103., 80., "offline", 90)
        self.logs = self.replace("bot.engine.log", Mock())

    def mutations(self, path=None):
        return [c for c in self.fake.state["calls"]
                if c[0] != "GET" and (path is None or c[1] == path)]

    def _healthy_streams(self):
        market = self.client.market_data_health
        market.mark_connected()
        for s in SYMBOLS:
            market.record_public_event("kline", s)
        private = self.client.private_stream_health
        private.mark_connecting()
        private.mark_listen_key_confirmed()
        private.mark_reconciled(private.mark_connected(PRIVATE_ROUTE))
        # Startup handshake probe result (production sets it at connect time).
        self.client._prelive_private_ws_probe_ok = True

    def replace(self, name, value):
        p = patch(name, value, create=True)
        self.patches.append(p)
        return p.start()

    def blockers(self):
        return list(self.engine.pilot.state.blocked_reasons)

    async def attempt(self, decision):
        self.decide.return_value = decision
        await self.engine._open(self.sig)

    async def asyncTearDown(self):
        await asyncio.sleep(0)
        await self.client.close()
        self.env.stop()
        for p in reversed(self.patches):
            p.stop()
        await self.fake.stop()
        await self._drop_database()


__all__ = ["BinanceEngineFixture", "approval", "instrument", "SYMBOLS",
           "Signal", "E", "cfg", "time"]
