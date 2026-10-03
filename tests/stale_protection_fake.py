"""Offline KuCoin-like fake exchange with conditional (stop) orders.

Modelled semantics (as read by bot/conditional_stop_protection.py):
* a triggered ``closeOrder`` stop closes the ENTIRE position on the symbol,
  whatever its direction (KuCoin closeOrder: "close the position");
* a triggered sized ``reduceOnly`` stop only reduces a position it opposes;
* whether KuCoin auto-cancels attached TP/SL legs when a position goes flat is
  UNKNOWN (not documented in this repo); ``auto_cancel_on_flat`` selects it.
"""
import json
from urllib.parse import parse_qs, urlparse


class Resp:
    def __init__(self, payload, fail=None):
        self.status, self.headers, self._payload, self._fail = 200, {}, payload, fail

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        if self._fail:
            raise self._fail
        return self

    async def __aexit__(self, *exc):
        return False


class StopExchange:
    closed = False

    def __init__(self, positions, stops=(), *, auto_cancel_on_flat=False, cancel_fail=()):
        self.positions = dict(positions)            # kucoin symbol -> signed contracts
        self.stops = [dict(s, status="active") for s in stops]
        self.auto_cancel_on_flat = auto_cancel_on_flat
        self.cancel_fail = set(cancel_fail)
        self.calls, self.orders = [], {}
        self.stop_fires_on_close = set()

    # -- helpers -------------------------------------------------------------
    def active_stops(self, symbol=None):
        return [s for s in self.stops if s["status"] == "active"
                and (symbol is None or s["symbol"] == symbol)]

    def _flat_hook(self, sym):
        if self.auto_cancel_on_flat and not self.positions.get(sym):
            for s in self.active_stops(sym):
                s["status"] = "cancelled"

    def tick(self, symbol, price):
        """Move the market; trigger stop orders whose condition is met."""
        for s in self.active_stops(symbol):
            hit = price <= s["stopPrice"] if s["stop"] == "down" else price >= s["stopPrice"]
            if not hit:
                continue
            s["status"] = "triggered"
            cur = self.positions.get(symbol, 0)
            if s.get("closeOrder"):
                self.positions[symbol] = 0
            elif s.get("reduceOnly") and cur:
                opposes = (s["side"] == "sell") == (cur > 0)
                if opposes:
                    size = min(abs(cur), int(s.get("size", 0)))
                    self.positions[symbol] = cur - size if cur > 0 else cur + size
            self._flat_hook(symbol)

    # -- HTTP ----------------------------------------------------------------
    def get(self, url, **kw):
        self.calls.append(("GET", url, None))
        parsed = urlparse(url)
        path, query = parsed.path, parse_qs(parsed.query)
        if path.endswith("/api/v1/positions"):
            rows = [{"symbol": s, "currentQty": c, "avgEntryPrice": 100.0, "markPrice": 100.0}
                    for s, c in self.positions.items() if c]
            return Resp({"code": "200000", "data": rows})
        if path.endswith("/api/v1/stopOrders"):
            sym = query.get("symbol", [None])[0]
            items = [{k: v for k, v in s.items()} for s in self.active_stops(sym)]
            return Resp({"code": "200000", "data": {"items": items}})
        if path.endswith("/byClientOid"):
            oid = query.get("clientOid", [""])[0]
            order = self.orders.get(oid)
            return Resp({"code": "200000", "data": dict(order) if order else {}})
        if "/api/v1/orders/" in path:
            return Resp({"code": "200000", "data": {"isActive": False, "filledSize": "1",
                                                    "cancelExist": False}})
        if path.endswith("/api/v1/orders"):
            return Resp({"code": "200000", "data": {"items": []}})
        return Resp({"code": "200000", "data": []})

    def post(self, url, **kw):
        body = json.loads(kw.get("data") or "{}")
        self.calls.append(("POST", url, body))
        sym = body.get("symbol")
        if sym in self.stop_fires_on_close:
            self.tick(sym, -1 if self.positions.get(sym, 0) > 0 else 10**9)
        if body.get("reduceOnly") is True and "stop" not in body:
            cur = self.positions.get(sym, 0)
            size = int(body["size"])
            if cur and (body["side"] == "sell") == (cur > 0) and size <= abs(cur):
                self.positions[sym] = cur - size if cur > 0 else cur + size
            self._flat_hook(sym)
            oid = body["clientOid"]
            self.orders[oid] = {"id": f"kc-{len(self.orders) + 1}", "clientOid": oid}
            return Resp({"code": "200000", "data": {"orderId": self.orders[oid]["id"]}})
        return Resp({"code": "200000", "data": {"orderId": "kc-other"}})

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url, None))
        order_id = urlparse(url).path.rsplit("/", 1)[-1]
        if order_id in self.cancel_fail:
            return Resp({"code": "100001", "msg": "cancel failed"})
        for s in self.stops:
            if s["id"] == order_id:
                if s["status"] != "active":
                    return Resp({"code": "100004", "msg": "order not active"})
                s["status"] = "cancelled"
                return Resp({"code": "200000", "data": {"cancelledOrderIds": [order_id]}})
        return Resp({"code": "100004", "msg": "order not exists"})

    def deletes(self):
        return [c[1].rsplit("/", 1)[-1] for c in self.calls if c[0] == "DELETE"]

    def posts(self):
        return [c[2] for c in self.calls if c[0] == "POST"]


def native_and_bgx_stops(kc="XBTUSDTM", child_oid=""):
    """Position A LONG: native st-orders TP/SL legs + a BGX break-even stop."""
    return [
        {"id": f"{kc}-A-SL", "clientOid": child_oid, "symbol": kc, "side": "sell",
         "stop": "down", "stopPrice": 95.0, "stopPriceType": "TP", "closeOrder": True,
         "reduceOnly": True, "type": "market"},
        {"id": f"{kc}-A-TP", "clientOid": child_oid, "symbol": kc, "side": "sell",
         "stop": "up", "stopPrice": 110.0, "stopPriceType": "TP", "closeOrder": True,
         "reduceOnly": True, "type": "market"},
        {"id": f"{kc}-A-BE", "clientOid": f"bgx-stop-{kc.lower()}-be", "symbol": kc,
         "side": "sell", "stop": "down", "stopPrice": 100.0, "stopPriceType": "MP",
         "closeOrder": True, "reduceOnly": True, "type": "market"},
    ]
