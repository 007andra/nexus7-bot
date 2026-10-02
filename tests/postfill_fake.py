"""Offline KuCoin-like session for F-013 post-fill geometry tests.

Entry via ``POST /api/v1/st-orders`` fills at configurable prices (one or more
fills, optionally partial), opens the position and creates the native SL/TP
legs at the EXACT triggers sent. Protective stops posted later
(``/api/v1/orders`` with ``stop``) are registered; DELETE cancels them.
Order status, positions, stop orders and the fills ledger reflect that state.
"""
import json
import time
from urllib.parse import parse_qs, urlparse


class Resp:
    def __init__(self, payload):
        self.status, self.headers, self._payload = 200, {}, payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _ok(data):
    return Resp({"code": "200000", "data": data})


class PostfillExchange:
    closed = False

    def __init__(self, kc_symbol, multiplier, *, fills, mark=None, partial_of=None,
                 fail_stop_create=False, keep_active=False):
        self.kc, self.mult = kc_symbol, multiplier
        self.fill_plan = list(fills)            # [(contracts, price), ...]
        self.partial_of = partial_of             # requested size when partial
        self.mark = mark
        self.fail_stop_create = fail_stop_create
        self.keep_active = keep_active           # F-013A: entry order still working after fills
        self.position = None                     # {"qty": signed contracts, "entry": avg}
        self.stops, self.orders, self.fills = [], {}, []
        self.calls = []
        self._n = 0

    # -- helpers ---------------------------------------------------------------
    def _id(self, prefix):
        self._n += 1
        return f"{prefix}-{self._n}"

    def active_stops(self):
        return [s for s in self.stops if s["status"] == "active"]

    def sl_levels(self):
        long = (self.position or {}).get("qty", 0) > 0
        side = "sell" if long else "buy"
        mark = self.mark or self.position["entry"]
        return sorted(float(s["stopPrice"]) for s in self.active_stops()
                      if s["side"] == side and ((float(s["stopPrice"]) < mark) == long))

    def tp_levels(self):
        long = (self.position or {}).get("qty", 0) > 0
        mark = self.mark or self.position["entry"]
        return sorted(float(s["stopPrice"]) for s in self.active_stops()
                      if (float(s["stopPrice"]) > mark) == long)

    def posts(self):
        return [c[2] for c in self.calls if c[0] == "POST"]

    # -- HTTP ------------------------------------------------------------------
    def get(self, url, **kw):
        self.calls.append(("GET", url, None))
        parsed = urlparse(url)
        path, query = parsed.path, parse_qs(parsed.query)
        if path.endswith("/api/v1/positions"):
            rows = []
            if self.position and self.position["qty"]:
                rows.append({"symbol": self.kc, "currentQty": self.position["qty"],
                             "avgEntryPrice": self.position["entry"],
                             "markPrice": self.mark or self.position["entry"]})
            return _ok(rows)
        if path.endswith("/api/v1/stopOrders"):
            return _ok({"items": [dict(s) for s in self.active_stops()]})
        if path.endswith("/api/v1/recentFills"):
            return _ok([])
        if path.endswith("/api/v1/fills"):
            items = [dict(f) for f in self.fills]
            return _ok({"items": items, "currentPage": 1, "totalPage": 1 if items else 0,
                        "totalNum": len(items)})
        if path.endswith("/byClientOid"):
            oid = query.get("clientOid", [""])[0]
            order = next((o for o in self.orders.values() if o.get("clientOid") == oid), None)
            return _ok(dict(order) if order else {})
        if "/api/v1/orders/" in path:
            order = self.orders.get(path.rsplit("/", 1)[-1])
            return _ok(dict(order) if order else {})
        if "getMarginMode" in path:
            return _ok({"marginMode": "CROSS"})
        return _ok([])

    def post(self, url, **kw):
        body = json.loads(kw.get("data") or "{}")
        self.calls.append(("POST", url, body))
        path = urlparse(url).path
        if path.endswith("/api/v1/st-orders") and body.get("reduceOnly") is not True:
            return self._entry(body)
        if "stop" in body:
            if self.fail_stop_create:
                return Resp({"code": "300000", "msg": "stop rejected"})
            sid = self._id("st")
            self.stops.append(dict(body, id=sid, status="active", isActive=True))
            return _ok({"orderId": sid})
        return _ok({"orderId": self._id("kc")})

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url, None))
        oid = urlparse(url).path.rsplit("/", 1)[-1]
        for s in self.stops:
            if s["id"] == oid and s["status"] == "active":
                s["status"] = "cancelled"
                return _ok({"cancelledOrderIds": [oid]})
        return Resp({"code": "100004", "msg": "order not exists"})

    # -- entry -----------------------------------------------------------------
    def _entry(self, body):
        oid = self._id("entry")
        sign = 1 if body["side"] == "buy" else -1
        requested = int(body["size"])
        plan = self.fill_plan
        if plan and not isinstance(plan[0], tuple):          # prices only: split the order
            share, rest = divmod(requested, len(plan))
            plan = [(share + (1 if i < rest else 0), p) for i, p in enumerate(plan)]
        self.last_plan = plan
        filled = sum(c for c, _ in plan)
        value = sum(c * p * self.mult for c, p in plan)
        now = time.time_ns()
        for i, (c, p) in enumerate(plan):
            self.fills.append({"tradeId": f"{oid}-t{i}", "orderId": oid, "symbol": self.kc,
                               "side": body["side"], "size": c, "price": p, "fee": 0,
                               "feeCurrency": "USDT", "tradeTime": now + i, "tradeType": "trade"})
        self.position = {"qty": sign * filled, "entry": value / (filled * self.mult)}
        self.orders[oid] = {"id": oid, "clientOid": body.get("clientOid"), "symbol": self.kc,
                            "side": body["side"], "size": requested, "filledSize": filled,
                            "dealSize": filled, "dealValue": value,
                            "isActive": bool(self.keep_active),
                            "cancelExist": filled < requested and not self.keep_active,
                            "status": "open" if self.keep_active else "done",
                            "reduceOnly": False,
                            **{k: body[k] for k in ("triggerStopUpPrice", "triggerStopDownPrice")
                               if k in body}}
        close_side = "sell" if sign > 0 else "buy"
        for key, kind in (("triggerStopDownPrice", "down"), ("triggerStopUpPrice", "up")):
            if body.get(key):
                self.stops.append({"id": self._id("leg"), "clientOid": "", "symbol": self.kc,
                                   "side": close_side, "stop": kind, "stopPrice": float(body[key]),
                                   "stopPriceType": "TP", "closeOrder": True, "reduceOnly": True,
                                   "type": "market", "status": "active", "isActive": True})
        return _ok({"orderId": oid})

    # -- F-013A scenario helpers ----------------------------------------------
    def entry_order(self):
        return next(o for o in self.orders.values() if str(o["id"]).startswith("entry"))

    def late_fill(self, contracts, price, *, complete=True, trade_id=None):
        """More fills of the SAME opening order (exchange truth everywhere)."""
        order = self.entry_order()
        sign = 1 if order["side"] == "buy" else -1
        tid = trade_id or f"{order['id']}-late{len(self.fills)}"
        if any(f["tradeId"] == tid for f in self.fills):
            return                                   # duplicate event: idempotent
        self.fills.append({"tradeId": tid, "orderId": order["id"], "symbol": self.kc,
                           "side": order["side"], "size": contracts, "price": price, "fee": 0,
                           "feeCurrency": "USDT", "tradeTime": time.time_ns(), "tradeType": "trade"})
        old = abs(self.position["qty"])
        new = old + contracts
        self.position = {"qty": sign * new,
                         "entry": (self.position["entry"] * old + price * contracts) / new}
        order["dealSize"] = order["filledSize"] = order["dealSize"] + contracts
        order["dealValue"] = order["dealValue"] + contracts * price * self.mult
        if complete:
            order.update(isActive=False, status="done",
                         cancelExist=order["dealSize"] < order["size"])

    def manual_add(self, contracts, price):
        """Exposure added OUTSIDE BGX (other order id) — not a fill of ours."""
        sign = 1 if self.position["qty"] > 0 else -1
        old = abs(self.position["qty"])
        self.fills.append({"tradeId": f"manual-{len(self.fills)}", "orderId": "manual-order",
                           "symbol": self.kc, "side": "buy" if sign > 0 else "sell",
                           "size": contracts, "price": price, "fee": 0, "feeCurrency": "USDT",
                           "tradeTime": time.time_ns(), "tradeType": "trade"})
        self.position = {"qty": sign * (old + contracts),
                         "entry": (self.position["entry"] * old + price * contracts) / (old + contracts)}

    def partial_exit(self, contracts):
        sign = 1 if self.position["qty"] > 0 else -1
        self.position["qty"] = sign * (abs(self.position["qty"]) - contracts)
