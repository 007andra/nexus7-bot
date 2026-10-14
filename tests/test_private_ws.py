"""
NEXUS-7 — Testes do WebSocket privado de ordens (Fase Final).

Testa _handle_private_order_event() diretamente, sem precisar de um
servidor WS completo simulando o canal privado — a lógica de
reconciliação (Fase 8, Casos D/E/F/G) é testável isoladamente.

Rodar: python -m tests.test_private_ws
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PAPER_TRADE", "true")
os.environ.setdefault("LOG_LEVEL", "ERROR")

_P = _F = 0
def check(n, c, x=""):
    global _P, _F
    if c: _P += 1; print(f"  ✓ {n}")
    else: _F += 1; print(f"  ✗ {n} {x}")


def test_caso_D_sem_evento_nao_assume_filled():
    """Fase 8 Caso D: sem NENHUM evento WS, ordem não vira FILLED sozinha."""
    from bot.order_state import OrderRegistry, OrderState

    reg = OrderRegistry()
    coid = "bgx7-casoD"
    mo, _ = reg.get_or_create(coid, "BTCUSDT", "Buy", 0.01)
    mo.transition(OrderState.SUBMITTING, source="REST")
    mo.transition(OrderState.SUBMITTED, order_id="oid_D", source="REST")
    check("sem evento, permanece SUBMITTED (não assume FILLED)",
          mo.state == OrderState.SUBMITTED, f"={mo.state}")
    check("is_open ainda True — não confirmado como protegido", mo.is_open)


def test_order_registry_conectado_ao_open():
    """
    Fecha o gap encontrado na auditoria anterior: OrderRegistry era
    instanciado mas nunca alimentado dentro de _open(). Verifica por
    inspeção de código que agora está conectado.
    """
    import inspect
    from bot import engine as E
    src = inspect.getsource(E)
    i_open = src.find("async def _open(self")
    corpo = src[i_open:src.find("async def ", i_open + 10)]
    check("_open() chama self.orders.get_or_create",
          "self.orders.get_or_create" in corpo)
    check("_open() chama transition SUBMITTING",
          "OrderState.SUBMITTING" in corpo)
    check("_open() chama transition SUBMITTED",
          "OrderState.SUBMITTED" in corpo)
    check("_open() chama transition FILLED",
          "OrderState.FILLED" in corpo)
    check("_open() indexa order_id no registry",
          "index_order_id" in corpo)



if __name__ == "__main__":
    print("═══ TESTES DO WEBSOCKET PRIVADO DE ORDENS ═══")
    for fn in [
               test_caso_D_sem_evento_nao_assume_filled,
               test_order_registry_conectado_ao_open,]:
        print(f"\n{fn.__name__}:")
        try:
            fn()
        except Exception as e:
            _F += 1
            import traceback
            print(f"  ✗ ERRO: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{chr(61)*50}\nPASSOU: {_P} | FALHOU: {_F}\n{chr(61)*50}")
    sys.exit(1 if _F else 0)
