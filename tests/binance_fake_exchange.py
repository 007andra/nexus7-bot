"""Loopback fake of the Binance USD-M REST surface the engine reads.

Serves realistic, configurable payloads on 127.0.0.1 so the production
``BinanceClient`` runs unmodified (signing, parsing, validation). Mutations
are refused unless ``state["accept_mutations"]`` is true; then MARKET orders
fill at the book, positions update, and conditional (algo) protection orders
are stored, served back and cancellable, like a minimal matching venue.
"""
from __future__ import annotations

import copy

from aiohttp import web

DEFAULT_SYMBOLS = ("TESTUSDT", "OTHERUSDT")


def default_state(symbols=DEFAULT_SYMBOLS, *, price=100.0, wallet=100.0):
    return {
        "server_time_ms": None,          # None -> real clock
        "wallet": wallet,
        "available": wallet,
        "can_trade": True,
        "multi_assets": False,
        "dual_side": False,
        "margin_type": "CROSSED",
        "leverage": 50,
        "positions": [],                 # raw positionRisk rows
        "open_orders": [],
        "algo_orders": [],
        "prices": {s: price for s in symbols},
        "filters": {s: {"tickSize": "0.01", "stepSize": "1", "minQty": "1",
                        "notional": "5"} for s in symbols},
        "brackets": [{"bracket": 1, "initialLeverage": 75, "notionalFloor": 0,
                      "notionalCap": 1_000_000, "maintMarginRatio": 0.005,
                      "cum": 0}],
        "income": [],                    # raw /fapi/v1/income rows
        "orders": {},                    # orderId -> raw /fapi/v1/order row
        "algos": {},                     # algoId -> raw algo row
        "accept_mutations": False,
        "order_fill": "FILLED",          # or "NEW": accepted, never fills
        "lose_next_order_response": False,  # execute, then answer HTTP 503
        "next_id": 5000,
        "taker_fee": "0.0005", "maker_fee": "0.0002",
        "calls": [],
    }


class FakeBinance:
    def __init__(self, state=None):
        self.state = state or default_state()
        self.runner = None
        self.base = None

    async def start(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._dispatch)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return self.base

    async def stop(self):
        if self.runner is not None:
            await self.runner.cleanup()

    async def _dispatch(self, request):
        path, method = request.path, request.method
        self.state["calls"].append((method, path, dict(request.query)))
        if method != "GET" and path != "/fapi/v1/listenKey":
            if not self.state["accept_mutations"]:
                return web.json_response(
                    {"code": -2015, "msg": "fake exchange refuses mutations"},
                    status=403)
            name = f"_{method.lower()}_" + path.strip("/").replace("/", "_")
            handler = getattr(self, name, None)
            if handler is None:
                return web.json_response({"code": -1000, "msg": f"unmapped {method} {path}"},
                                         status=404)
            body, status = handler(dict(request.query))
            if (path == "/fapi/v1/order" and status == 200
                    and self.state["lose_next_order_response"]):
                self.state["lose_next_order_response"] = False
                return web.json_response({"code": -1001, "msg": "lost"}, status=503)
            return web.json_response(body, status=status)
        handler = getattr(self, "_" + path.strip("/").replace("/", "_"), None)
        if handler is None:
            return web.json_response({"code": -1000, "msg": f"unmapped {path}"},
                                     status=404)
        return web.json_response(handler(request.query))

    # ── reads ──────────────────────────────────────────────────────
    def _fapi_v1_ping(self, q):
        return {}

    def _fapi_v1_time(self, q):
        import time
        t = self.state["server_time_ms"]
        return {"serverTime": int(time.time() * 1000) if t is None else t}

    def _fapi_v1_exchangeInfo(self, q):
        symbols = []
        for s, f in self.state["filters"].items():
            symbols.append({
                "symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT",
                "status": "TRADING",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": f["tickSize"]},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": f["stepSize"],
                     "minQty": f["minQty"]},
                    {"filterType": "MIN_NOTIONAL", "notional": f["notional"]},
                ]})
        return {"symbols": symbols}

    def _margin(self):
        used = sum(abs(float(p["positionAmt"])) * float(p["entryPrice"])
                   / self.state["leverage"] for p in self.state["positions"])
        return used

    def _fapi_v3_account(self, q):
        s = self.state
        return {
            "totalMarginBalance": str(s["wallet"]),
            "availableBalance": str(s["available"]),
            "totalWalletBalance": str(s["wallet"]),
            "totalCrossWalletBalance": str(s["wallet"]),
            "totalUnrealizedProfit": "0", "totalCrossUnPnl": "0",
            "totalPositionInitialMargin": str(self._margin()),
            "totalOpenOrderInitialMargin": "0", "totalMaintMargin": "0",
        }

    def _fapi_v1_accountConfig(self, q):
        return {"canTrade": self.state["can_trade"],
                "multiAssetsMargin": self.state["multi_assets"]}

    def _fapi_v3_balance(self, q):
        s = self.state
        return [{"asset": "USDT", "balance": str(s["wallet"]),
                 "availableBalance": str(s["available"])}]

    def _fapi_v3_positionRisk(self, q):
        return copy.deepcopy(self.state["positions"])

    def _fapi_v1_symbolConfig(self, q):
        return [{"symbol": q.get("symbol"), "marginType": self.state["margin_type"],
                 "leverage": self.state["leverage"]}]

    def _fapi_v1_leverageBracket(self, q):
        return [{"symbol": q.get("symbol"), "notionalCoef": 1.0,
                 "brackets": copy.deepcopy(self.state["brackets"])}]

    def _fapi_v1_positionSide_dual(self, q):
        return {"dualSidePosition": self.state["dual_side"]}

    def _fapi_v1_openOrders(self, q):
        return copy.deepcopy(self.state["open_orders"])

    def _fapi_v1_openAlgoOrders(self, q):
        live = [r for r in self.state["algos"].values()
                if r["algoStatus"] == "NEW"
                and (not q.get("symbol") or r["symbol"] == q.get("symbol"))]
        return copy.deepcopy(self.state["algo_orders"] + live)

    _fapi_v1_algoOpenOrders = _fapi_v1_openAlgoOrders

    def _price(self, q):
        return float(self.state["prices"].get(q.get("symbol"), 100.0))

    def _fapi_v1_ticker_bookTicker(self, q):
        p = self._price(q)
        return {"symbol": q.get("symbol"), "bidPrice": f"{p * 0.9999:.8f}",
                "askPrice": f"{p * 1.0001:.8f}", "bidQty": "1000", "askQty": "1000"}

    def _fapi_v1_ticker_24hr(self, q):
        p = self._price(q)
        return {"symbol": q.get("symbol"), "lastPrice": str(p),
                "volume": "100000", "quoteVolume": str(100000 * p)}

    def _fapi_v1_premiumIndex(self, q):
        p = self._price(q)
        return {"symbol": q.get("symbol"), "markPrice": str(p),
                "lastFundingRate": "0.0001"}

    def _fapi_v1_depth(self, q):
        p = self._price(q)
        return {"bids": [[f"{p * (1 - i * 1e-4):.8f}", "500"] for i in range(1, 11)],
                "asks": [[f"{p * (1 + i * 1e-4):.8f}", "500"] for i in range(1, 11)]}

    def _fapi_v1_openInterest(self, q):
        return {"symbol": q.get("symbol"), "openInterest": "100000"}

    def _fapi_v1_klines(self, q):
        import time
        p, period = self._price(q), 900_000
        now = int(time.time() * 1000) // period * period
        limit = int(q.get("limit", 100))
        return [[now - (limit - i) * period, str(p), str(p * 1.001), str(p * 0.999),
                 str(p), "1000", now - (limit - i - 1) * period - 1, "0", 1, "0",
                 "0", "0"] for i in range(limit)]

    def accept_market_order(self, *, symbol, side, qty, order_id, client_oid,
                            status="FILLED", fill_price=None):
        """Record what the exchange would hold after a MARKET order."""
        price = float(fill_price if fill_price is not None
                      else self.state["prices"].get(symbol, 100.0))
        executed = qty if status in ("FILLED", "PARTIALLY_FILLED") else 0.0
        self.state["orders"][str(order_id)] = {
            "orderId": int(order_id) if str(order_id).isdigit() else order_id,
            "symbol": symbol, "status": status, "clientOrderId": client_oid,
            "side": side, "type": "MARKET", "origQty": str(qty),
            "executedQty": str(executed), "avgPrice": str(price),
            "cumQuote": str(executed * price), "reduceOnly": False,
            "updateTime": 0,
        }

    def _fapi_v1_order(self, q):
        key = q.get("orderId")
        row = self.state["orders"].get(str(key)) if key else None
        if row is None and q.get("origClientOrderId"):
            row = next((r for r in self.state["orders"].values()
                        if r["clientOrderId"] == q["origClientOrderId"]), None)
        return copy.deepcopy(row) if row else {"code": -2013, "msg": "Order does not exist."}

    # ── mutations (only with accept_mutations) ─────────────────────
    def _new_id(self):
        self.state["next_id"] += 1
        return self.state["next_id"]

    def _position(self, symbol):
        for row in self.state["positions"]:
            if row["symbol"] == symbol:
                return row
        return None

    def _post_fapi_v1_order(self, q):
        symbol, side = q.get("symbol"), q.get("side")
        qty = float(q.get("quantity", 0))
        if q.get("type") != "MARKET" or qty <= 0 or side not in ("BUY", "SELL"):
            return {"code": -1102, "msg": "fake supports MARKET only"}, 400
        mid = float(self.state["prices"].get(symbol, 100.0))
        price = mid * (1.0001 if side == "BUY" else 0.9999)
        if self.state["order_fill"] == "NEW":
            order_id = self._new_id()
            row = {"orderId": order_id, "symbol": symbol, "status": "NEW",
                   "clientOrderId": q.get("newClientOrderId", ""), "side": side,
                   "type": "MARKET", "origQty": str(qty), "executedQty": "0",
                   "avgPrice": "0", "cumQuote": "0", "reduceOnly": False,
                   "updateTime": 0}
            self.state["orders"][str(order_id)] = row
            return copy.deepcopy(row), 200
        signed = qty if side == "BUY" else -qty
        pos = self._position(symbol)
        reduce_only = str(q.get("reduceOnly", "")).lower() == "true"
        if pos is None:
            if reduce_only:
                return {"code": -2022, "msg": "ReduceOnly Order is rejected."}, 400
            pos = {"symbol": symbol, "positionAmt": "0", "entryPrice": "0",
                   "markPrice": str(mid), "unRealizedProfit": "0",
                   "leverage": str(self.state["leverage"]), "liquidationPrice": "0",
                   "marginType": "cross", "isolatedMargin": "0",
                   "positionSide": "BOTH"}
            self.state["positions"].append(pos)
        new_amt = float(pos["positionAmt"]) + signed
        if abs(new_amt) < 1e-12:
            self.state["positions"].remove(pos)
        else:
            if float(pos["positionAmt"]) == 0:
                pos["entryPrice"] = str(price)
            pos["positionAmt"] = str(new_amt)
        order_id = self._new_id()
        row = {"orderId": order_id, "symbol": symbol, "status": "FILLED",
               "clientOrderId": q.get("newClientOrderId", ""), "side": side,
               "type": "MARKET", "origQty": str(qty), "executedQty": str(qty),
               "avgPrice": str(price), "cumQuote": str(qty * price),
               "reduceOnly": reduce_only, "updateTime": 0}
        self.state["orders"][str(order_id)] = row
        return copy.deepcopy(row), 200

    def _post_fapi_v1_algoOrder(self, q):
        algo_id = self._new_id()
        row = {"algoId": algo_id, "clientAlgoId": q.get("clientAlgoId", ""),
               "symbol": q.get("symbol"), "side": q.get("side"),
               "orderType": q.get("type"), "triggerPrice": q.get("triggerPrice"),
               "closePosition": str(q.get("closePosition")).lower() == "true",
               "reduceOnly": False, "algoStatus": "NEW", "quantity": "0"}
        self.state["algos"][str(algo_id)] = row
        return copy.deepcopy(row), 200

    def _delete_fapi_v1_algoOrder(self, q):
        for row in self.state["algos"].values():
            if (str(row["algoId"]) == str(q.get("algoId"))
                    or (q.get("clientAlgoId") and row["clientAlgoId"] == q["clientAlgoId"])):
                row["algoStatus"] = "CANCELED"
                return {"algoId": row["algoId"], "code": "200"}, 200
        return {"code": -2011, "msg": "Unknown order sent."}, 400

    def _delete_fapi_v1_algoOpenOrders(self, q):
        for row in self.state["algos"].values():
            if row["symbol"] == q.get("symbol"):
                row["algoStatus"] = "CANCELED"
        return {"code": 200, "msg": "success"}, 200

    def _delete_fapi_v1_allOpenOrders(self, q):
        return {"code": 200, "msg": "success"}, 200

    def _post_fapi_v1_leverage(self, q):
        self.state["leverage"] = int(q.get("leverage", self.state["leverage"]))
        return {"symbol": q.get("symbol"), "leverage": self.state["leverage"]}, 200

    def _fapi_v1_income(self, q):
        rows = self.state["income"]
        kind = q.get("incomeType")
        return copy.deepcopy([r for r in rows if not kind or r.get("incomeType") == kind])

    def _fapi_v1_commissionRate(self, q):
        return {"symbol": q.get("symbol"),
                "makerCommissionRate": self.state["maker_fee"],
                "takerCommissionRate": self.state["taker_fee"]}

    def _fapi_v1_listenKey(self, q):
        return {"listenKey": "fake-listen-key"}
