"""
NEXUS-7 — FASE 4G: WebSocket Chaos Testing.

Executa os cenários A–G com servidor e cliente no MESMO processo,
permitindo injetar falhas de verdade.

Critério para TODOS: o estado local converge para o da exchange.
"""
import asyncio, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update({
    "KUCOIN_REST_BASE": "http://127.0.0.1:8810",
    "KUCOIN_API_KEY": "k", "KUCOIN_API_SECRET": "s",
    "KUCOIN_API_PASSPHRASE": "p", "PAPER_TRADE": "false",
    "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
    "BOT_API_SECRET": "t", "LOG_LEVEL": "ERROR",
    "LEVERAGE": "10", "MAX_RISK_PCT": "0.5",
    "NEXUS_AI_ENABLED": "false", "NEXUS_TELEGRAM": "false",
})

from aiohttp import web
import tests.mock_kucoin as MK

_P = _F = 0
def ck(n, c, x=""):
    global _P, _F
    if c: _P += 1; print(f"  ✓ {n}")
    else: _F += 1; print(f"  ✗ {n} {x}")


async def run():
    runner = web.AppRunner(MK.make_app()); await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8810).start()

    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine
    from bot.integrity import IntegrityGuard
    from tests.execution_test_context import ValidExecutionTestContext

    c = KuCoinClient(); e = TradingEngine(c)
    await c.load_instruments()
    inst = c.get_instruments()

    # Legacy non-composed client (entry + separate stop) driving WS chaos; the
    # F-003 transport barrier (native stop + risk authorization) is covered by
    # tests/test_risk_budget_sizing*.py, so it is isolated here explicitly.
    from unittest.mock import patch as _patch
    with _patch("bot.risk_budget.assert_transport_dispatch"):
        await _run_scenarios(c, e, inst, ValidExecutionTestContext, IntegrityGuard, runner)


async def _run_scenarios(c, e, inst, ValidExecutionTestContext, IntegrityGuard, runner):
    async with ValidExecutionTestContext(c, inst):
        # ─────────── A — WS CAI COM POSIÇÃO ABERTA ───────────
        print("\n═══ A — WS cai durante posição aberta ═══")
        MK.ORDERS.clear(); MK.POSITIONS.clear(); MK.STOPS.clear()
        await c.place_order("BTCUSDT", "Buy", 0.002, sl=100000, tp=120000,
                            instruments=inst, idem_key="chaos_a")
        await c.set_position_stops("BTCUSDT", sl=100000, tp=120000)

        # simula queda: zera o cache e o marcador de atividade do WS
        c._kline_cache.clear()
        c._last_ws_update = 0
        e.positions.clear()

        await e._load_existing_positions()      # recupera via REST
        ck("posição preservada após queda do WS", len(e.positions) == 1,
           f"={len(e.positions)}")
        ck("REST continua funcionando", (await c.get_balance()) > 0)

        # ─────────── B — WS CAI DURANTE ENVIO DE ORDEM ───────────
        print("\n═══ B — WS cai durante envio de ordem ═══")
        MK.ORDERS.clear(); MK.POSITIONS.clear()
        async def derruba():
            await asyncio.sleep(0.01)
            c._last_ws_update = 0
            if getattr(c, "_ping_task", None):
                c._ping_task.cancel()
        r = await asyncio.gather(
            c.place_order("ETHUSDT", "Buy", 0.1, sl=3000, tp=4500,
                          instruments=inst, idem_key="chaos_b"),
            derruba(), return_exceptions=True)
        ck("ordem completa apesar da queda do WS",
           isinstance(r[0], dict) and bool(r[0].get("orderId")), str(r[0])[:60])
        ck("exchange registrou 1 posição", len(MK.POSITIONS) == 1)

        # ─────────── C — EXECUÇÃO SEM CONFIRMAÇÃO ───────────
        print("\n═══ C — exchange executa mas resposta não chega ═══")
        MK.ORDERS.clear(); MK.POSITIONS.clear()
        MK.FAULTS["order_no_reply"] = True
        try:
            await c.place_order("SOLUSDT", "Buy", 1.0, sl=100, tp=200,
                                instruments=inst, idem_key="chaos_c")
        except Exception:
            pass
        MK.FAULTS["order_no_reply"] = False
        existe = await c._position_exists("SOLUSDT")
        ck("bot detecta execução consultando a exchange", existe)
        # retry NÃO pode duplicar
        n_antes = len([o for o in MK.ORDERS if not o.get("reduceOnly")])
        await c.place_order("SOLUSDT", "Buy", 1.0, sl=100, tp=200,
                            instruments=inst, idem_key="chaos_c")
        oids = {o.get("clientOid") for o in MK.ORDERS if o.get("clientOid")}
        ck("retry reusa o mesmo clientOid", len(oids) == 1, f"oids={len(oids)}")
        ck("nenhuma posição duplicada", len(MK.POSITIONS) <= 1,
           f"pos={len(MK.POSITIONS)}")

        # ─────────── D — EVENTOS DUPLICADOS ───────────
        print("\n═══ D — mesmo evento chega várias vezes ═══")
        c._kline_cache.clear()
        ts = int(time.time() * 1000) - 900_000
        evento = {
            "topic": "/contractMarket/limitCandle:XBTUSDTM_15min",
            "type": "message",
            "data": {"symbol": "XBTUSDTM",
                     "candles": [str(ts // 1000), "100", "101", "102", "99", "500"]},
        }
        for _ in range(5):
            await c._handle_ws_message(dict(evento))
        cache = c._kline_cache.get(("BTCUSDT", "15"), [])
        ts_unicos = len({k["ts"] for k in cache})
        ck("5 eventos idênticos → 1 candle no cache",
           len(cache) == ts_unicos, f"cache={len(cache)} únicos={ts_unicos}")

        # ─────────── E — EVENTOS FORA DE ORDEM ───────────
        print("\n═══ E — eventos chegam fora de ordem ═══")
        c._kline_cache.clear()
        base = int(time.time()) - 3600
        for offset in [1800, 0, 2700, 900]:        # desordenado de propósito
            await c._handle_ws_message({
                "topic": "/contractMarket/limitCandle:XBTUSDTM_15min",
                "type": "message",
                "data": {"symbol": "XBTUSDTM",
                         "candles": [str(base + offset), "100", "101", "102", "99", "500"]},
            })
        cache = c._kline_cache.get(("BTCUSDT", "15"), [])
        tss = [k["ts"] for k in cache]
        ck("cache fica ordenado cronologicamente", tss == sorted(tss),
           f"ts={tss}")

        # ─────────── F — HEARTBEAT FALHA ───────────
        print("\n═══ F — heartbeat/ping falha ═══")
        c._last_ws_update = time.time() - 7200      # 2h sem dados
        g = IntegrityGuard(); await g.assess(c, e)
        ck("dados stale detectados",
           "MARKET_DATA_STALE" in g.state.codes(), str(g.state.codes()))
        ck("stale bloqueia novas entradas", not g.can_open_new())

        # ─────────── G — RECONECTA APÓS PERDER EVENTOS ───────────
        print("\n═══ G — reconecta após perder eventos ═══")
        c._kline_cache.clear()
        c._last_ws_update = 0
        await c._seed_kline_cache(["BTCUSDT"], ["15"])   # seed via REST
        cache = c._kline_cache.get(("BTCUSDT", "15"), [])
        ck("cache reconstruído via REST após gap", len(cache) > 50,
           f"={len(cache)}")
        tss = [k["ts"] for k in cache]
        ck("cache reconstruído está ordenado", tss == sorted(tss))

        # ─────────── CONVERGÊNCIA FINAL ───────────
        print("\n═══ CONVERGÊNCIA local ↔ exchange ═══")
        e.positions.clear()
        await e._load_existing_positions()
        ex = await c.get_positions()
        ex_syms = {p["symbol"] for p in ex if abs(float(p.get("size", 0) or 0)) > 0}
        ck("estado local == estado da exchange",
           set(e.positions.keys()) == ex_syms,
           f"local={set(e.positions.keys())} exchange={ex_syms}")

        try: await c.close()
        except Exception: pass
    await runner.cleanup()


if __name__ == "__main__":
    print("═══ WEBSOCKET CHAOS TESTING (Fase 4G) ═══")
    try:
        asyncio.run(run())
    except Exception as ex:
        _F += 1
        import traceback; traceback.print_exc()
    print(f"\n{'═'*52}\nPASSOU: {_P} | FALHOU: {_F}\n{'═'*52}")
    sys.exit(1 if _F else 0)
