"""
NEXUS-7 — ORDER STATE MACHINE (Fase 3, P0)

Modela explicitamente o ciclo de vida de uma ordem e PROÍBE transições
inválidas. Sem isso, um retry podia reverter uma ordem já preenchida
para "enviando", abrindo caminho para duplicação.

    CREATED → SUBMITTING → SUBMITTED → PARTIALLY_FILLED → FILLED
                    ↓           ↓              ↓
                 FAILED    REJECTED       CANCELLED

Estados terminais (FILLED, REJECTED, CANCELLED, FAILED) não admitem
saída — uma vez lá, a ordem não volta atrás.
"""
import math
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List

from bot.logger import log


class OrderState(str, Enum):
    CREATED         = "CREATED"
    SUBMITTING      = "SUBMITTING"
    SUBMITTED       = "SUBMITTED"
    PARTIALLY_FILLED= "PARTIALLY_FILLED"
    FILLED          = "FILLED"
    REJECTED        = "REJECTED"
    CANCELLED       = "CANCELLED"
    FAILED          = "FAILED"


TERMINAIS = {OrderState.FILLED, OrderState.REJECTED,
             OrderState.CANCELLED, OrderState.FAILED}

# Transições permitidas. Tudo que não estiver aqui é inválido.
#
# Private-WS hardening: eventos autoritativos da exchange podem chegar antes
# do ACK REST/open do pedido. Portanto SUBMITTING pode avançar diretamente
# para PARTIALLY_FILLED ou FILLED. Isso preserva monotonicidade sem inventar
# um estado intermediário que a aplicação ainda não observou.
TRANSICOES = {
    OrderState.CREATED:          {OrderState.SUBMITTING, OrderState.FAILED},
    OrderState.SUBMITTING:       {OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED,
                                  OrderState.FILLED, OrderState.REJECTED,
                                  OrderState.FAILED},
    OrderState.SUBMITTED:        {OrderState.PARTIALLY_FILLED, OrderState.FILLED,
                                  OrderState.REJECTED, OrderState.CANCELLED},
    OrderState.PARTIALLY_FILLED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED,
                                  OrderState.CANCELLED},
    OrderState.FILLED:           set(),
    OrderState.REJECTED:         set(),
    OrderState.CANCELLED:        set(),
    OrderState.FAILED:           set(),
}


class InvalidTransition(Exception):
    """Transição proibida — indica bug de lógica, não erro operacional."""
    pass


@dataclass
class ManagedOrder:
    """
    Ordem com ciclo de vida controlado.

    client_oid é a chave de idempotência: duas ManagedOrder com o mesmo
    client_oid representam a MESMA ordem lógica, mesmo após restart.
    """
    client_oid: str
    symbol:     str
    side:       str
    qty:        float
    state:      OrderState = OrderState.CREATED
    order_id:   Optional[str] = None
    filled_qty: float = 0.0
    avg_price:  float = 0.0      # preço médio real de execução (dealValue/dealSize)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)   # última transição
    last_source: str  = ""       # "REST" ou "WS" — de onde veio a última atualização
    history:    List[tuple] = field(default_factory=list)
    # FILLED is execution-terminal, not proof that resulting exposure has been
    # observed by reconciliation. This bit is durable and may become True only
    # after exchange position truth has absorbed the fill.
    exposure_reconciliation_complete: bool = False
    # Semantic intent is durable. side alone cannot distinguish an entry from
    # a reduce/close because the same side can have different exposure effect.
    reduce_only: bool = False
    exposure_intent: str = "INCREASE"  # INCREASE | REDUCE
    previous_position_qty: Optional[float] = None
    # Durable protective intent of a BGX entry, persisted before dispatch so an
    # ambiguous submit can still be protected after the fill is proven:
    # {"direction": LONG|SHORT, "entry": float, "sl": float, "tp": float}.
    protection_plan: Optional[dict] = None

    def transition(self, novo: OrderState, **info):
        """
        Aplica uma transição. Levanta InvalidTransition se proibida.

        Um único caso de corrida é tratado como no-op monotônico: o ACK REST
        de SUBMITTED pode chegar depois de um evento privado WS já ter avançado
        a ordem para PARTIALLY_FILLED ou FILLED. Nesse caso o estado mais novo
        da exchange vence; o ACK atrasado serve apenas para anexar order_id e
        evidência de histórico. Nenhuma outra regressão de estado é aceita.
        """
        if novo == self.state and novo != OrderState.PARTIALLY_FILLED:
            return   # idempotente: mesmo estado não é erro

        source = str(info.get("source", "")).upper()
        stale_rest_submit_ack = (
            novo == OrderState.SUBMITTED
            and self.state in (OrderState.PARTIALLY_FILLED, OrderState.FILLED)
            and source == "REST"
        )
        if stale_rest_submit_ack:
            ack_order_id = str(info.get("order_id") or "") or None
            if ack_order_id and self.order_id and self.order_id != ack_order_id:
                raise InvalidTransition(
                    f"{self.symbol} [{self.client_oid[:8]}]: ACK REST tardio "
                    f"trouxe order_id conflitante ({ack_order_id} != {self.order_id})"
                )
            if ack_order_id and not self.order_id:
                self.order_id = ack_order_id

            history_info = dict(info)
            history_info["monotonic_noop"] = True
            history_info["stale_ack_state"] = OrderState.SUBMITTED.value
            self.history.append((
                time.time(), self.state.value, self.state.value, history_info
            ))
            log.info(
                "[ORDER_STATE_RACE] %s [%s]: stale REST SUBMITTED ack after %s; "
                "action=MONOTONIC_NOOP order_id=%s",
                self.symbol,
                self.client_oid[:8],
                self.state.value,
                self.order_id or "NONE",
            )
            return

        permitidos = TRANSICOES.get(self.state, set())
        if novo not in permitidos:
            raise InvalidTransition(
                f"{self.symbol} [{self.client_oid[:8]}]: "
                f"{self.state.value} → {novo.value} é INVÁLIDA "
                f"(permitidas: {sorted(x.value for x in permitidos) or 'nenhuma (terminal)'})"
            )

        anterior = self.state
        self.state = novo
        self.updated_at = time.time()
        self.last_source = info.get("source", self.last_source)
        self.history.append((time.time(), anterior.value, novo.value, info))

        if "order_id" in info and info["order_id"]:
            self.order_id = info["order_id"]
        if "filled_qty" in info:
            self.filled_qty = float(info["filled_qty"])
        if "avg_price" in info and info["avg_price"]:
            self.avg_price = float(info["avg_price"])
        if novo == OrderState.FILLED:
            self.exposure_reconciliation_complete = False

        log.debug(
            f"📋 {self.symbol} [{self.client_oid[:8]}]: "
            f"{anterior.value} → {novo.value}"
        )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAIS

    @property
    def is_open(self) -> bool:
        """Ordem viva na exchange (pode ainda executar)."""
        return self.state in (OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED)

    def to_dict(self) -> dict:
        return {
            "client_oid": self.client_oid, "symbol": self.symbol,
            "side": self.side, "qty": self.qty, "state": self.state.value,
            "order_id": self.order_id, "filled_qty": self.filled_qty,
            "terminal": self.is_terminal,
            "transitions": len(self.history),
        }

    def to_record(self) -> dict:
        """Lossless JSON-safe representation used for durable restart state."""
        return {
            "client_oid": self.client_oid,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "state": self.state.value,
            "order_id": self.order_id,
            "filled_qty": self.filled_qty,
            "avg_price": self.avg_price,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_source": self.last_source,
            "history": self.history,
            "exposure_reconciliation_complete": bool(
                self.exposure_reconciliation_complete
            ),
            "reduce_only": bool(self.reduce_only),
            "exposure_intent": str(self.exposure_intent),
            "previous_position_qty": self.previous_position_qty,
            "protection_plan": (
                dict(self.protection_plan) if self.protection_plan else None
            ),
        }

    @classmethod
    def from_record(cls, record: dict) -> "ManagedOrder":
        if not isinstance(record, dict):
            raise ValueError("managed order record must be a mapping")
        client_oid = str(record.get("client_oid", ""))
        symbol = str(record.get("symbol", ""))
        side = str(record.get("side", ""))
        qty = float(record.get("qty", 0))
        if not client_oid.startswith("bgx7-") or not symbol or side not in ("Buy", "Sell"):
            raise ValueError("invalid managed order identity")
        if not math.isfinite(qty) or qty <= 0:
            raise ValueError("invalid managed order quantity")
        order = cls(client_oid=client_oid, symbol=symbol, side=side, qty=qty)
        order.state = OrderState(str(record.get("state", "")))
        order.order_id = str(record.get("order_id") or "") or None
        order.filled_qty = max(0.0, float(record.get("filled_qty", 0) or 0))
        order.avg_price = max(0.0, float(record.get("avg_price", 0) or 0))
        order.created_at = float(record.get("created_at", time.time()))
        order.updated_at = float(record.get("updated_at", order.created_at))
        if not all(math.isfinite(v) for v in (
            order.filled_qty, order.avg_price, order.created_at, order.updated_at
        )):
            raise ValueError("non-finite managed order state")
        order.last_source = str(record.get("last_source", ""))[:24]
        history = record.get("history", [])
        if not isinstance(history, list):
            raise ValueError("invalid managed order history")
        order.history = history
        # Records written before BGX-MISSED-001 have no durable reconciliation
        # authority. They remain conservative for FILLED orders until exchange
        # truth is observed again; non-FILLED terminal states carry no exposure.
        order.exposure_reconciliation_complete = bool(
            record.get("exposure_reconciliation_complete", False)
        )
        order.reduce_only = bool(record.get("reduce_only", False))
        order.exposure_intent = str(
            record.get("exposure_intent", "REDUCE" if order.reduce_only else "INCREASE")
        )
        previous_qty = record.get("previous_position_qty")
        order.previous_position_qty = (
            None if previous_qty is None else max(0.0, float(previous_qty))
        )
        order.protection_plan = normalize_protection_plan(
            record.get("protection_plan")
        )
        return order


def normalize_protection_plan(plan) -> Optional[dict]:
    """Return a validated protective plan or None (never a partial plan)."""
    if not isinstance(plan, dict):
        return None
    direction = str(plan.get("direction") or "").upper()
    if direction not in ("LONG", "SHORT"):
        return None
    try:
        entry = float(plan.get("entry"))
        sl = float(plan.get("sl"))
        tp = float(plan.get("tp", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (entry, sl, tp)):
        return None
    if entry <= 0 or sl <= 0 or tp < 0:
        return None
    if direction == "LONG" and not sl < entry:
        return None
    if direction == "SHORT" and not sl > entry:
        return None
    return {
        "direction": direction, "entry": entry, "sl": sl, "tp": tp,
        # True once a local Position was materialized for this intent; only
        # never-materialized intents are candidates for ambiguous recovery.
        "materialized": bool(plan.get("materialized", False)),
    }


class OrderRegistry:
    """
    Registro de ordens por client_oid — garante idempotência mesmo com
    múltiplos workers ou após restart.

    Também indexado por order_id (Fase 2: correlação primária quando
    ambos orderId e clientOid estão disponíveis, ex: eventos do WS
    privado que trazem só orderId).
    """

    def __init__(self):
        self._orders: dict = {}
        self._by_order_id: dict = {}   # order_id -> client_oid

    def get_or_create(self, client_oid: str, symbol: str,
                      side: str, qty: float) -> tuple:
        """
        Retorna (ordem, criada_agora).

        Se já existe ordem com este client_oid, devolve a existente —
        é o mesmo sinal lógico, não uma nova ordem.
        """
        if client_oid in self._orders:
            return self._orders[client_oid], False
        o = ManagedOrder(client_oid=client_oid, symbol=symbol,
                         side=side, qty=qty)
        self._orders[client_oid] = o
        return o, True

    def get(self, client_oid: str) -> Optional[ManagedOrder]:
        return self._orders.get(client_oid)

    def get_by_order_id(self, order_id: str) -> Optional[ManagedOrder]:
        """Correlação primária por orderId (Fase 2) — usado pelo WS
        privado, que identifica eventos por orderId, não client_oid."""
        coid = self._by_order_id.get(order_id)
        return self._orders.get(coid) if coid else None

    def index_order_id(self, order_id: str, client_oid: str):
        """Registra o vínculo order_id -> client_oid assim que a
        exchange retorna o orderId (dentro de place_order)."""
        if order_id and client_oid:
            self._by_order_id[order_id] = client_oid

    def all_orders(self) -> List[ManagedOrder]:
        return list(self._orders.values())

    def snapshot(self) -> list:
        return [order.to_record() for order in self._orders.values()]

    def restore(self, records: list):
        """Replace registry state only after validating the whole snapshot."""
        if not isinstance(records, list):
            raise ValueError("order registry snapshot must be a list")
        restored = {}
        by_order_id = {}
        for record in records:
            order = ManagedOrder.from_record(record)
            if order.client_oid in restored:
                raise ValueError("duplicate client_oid in order registry snapshot")
            if order.order_id:
                if order.order_id in by_order_id:
                    raise ValueError("duplicate order_id in order registry snapshot")
                by_order_id[order.order_id] = order.client_oid
            restored[order.client_oid] = order
        self._orders = restored
        self._by_order_id = by_order_id

    def pending_orders(self) -> List[ManagedOrder]:
        """Include submissions whose exchange acknowledgement may be lost."""
        return [o for o in self._orders.values() if not o.is_terminal]

    def unreconciled_filled_orders(self, symbol: str = None) -> List[ManagedOrder]:
        return [
            o for o in self._orders.values()
            if o.state == OrderState.FILLED
            and not o.exposure_reconciliation_complete
            and (symbol is None or o.symbol == symbol)
        ]

    def mark_filled_exposure_reconciled(self, symbol: str) -> int:
        changed = 0
        for order in self.unreconciled_filled_orders(symbol):
            order.exposure_reconciliation_complete = True
            order.updated_at = time.time()
            order.history.append((
                order.updated_at, order.state.value, order.state.value,
                {"source": "EXPOSURE_RECONCILIATION", "exposure_reconciled": True},
            ))
            changed += 1
        return changed

    def open_orders(self, symbol: str = None) -> List[ManagedOrder]:
        return [o for o in self._orders.values()
                if o.is_open and (symbol is None or o.symbol == symbol)]

    def gc(self, max_age: float = 86400):
        """Remove ordens terminais antigas."""
        agora = time.time()
        for k in [k for k, o in list(self._orders.items())
                  if o.is_terminal
                  and not (
                      o.state == OrderState.FILLED
                      and not o.exposure_reconciliation_complete
                  )
                  and agora - o.created_at > max_age]:
            removed = self._orders.pop(k, None)
            if removed and removed.order_id:
                self._by_order_id.pop(removed.order_id, None)

    def __len__(self):
        return len(self._orders)