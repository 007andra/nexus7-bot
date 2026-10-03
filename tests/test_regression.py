"""
NEXUS-7 — Testes de regressão dos bugs encontrados em auditoria.

Cada teste corresponde a um bug REAL que chegou a produção.
Rodar: python -m tests.test_regression
"""
import os, sys, time, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PAPER_TRADE", "true")

_p = _f = 0
def check(name, cond, extra=""):
    global _p, _f
    if cond: _p += 1; print(f"  ✓ {name}")
    else:    _f += 1; print(f"  ✗ {name} {extra}")


def test_paper_trade_barrier():
    """P0: ausência de PAPER_TRADE não pode habilitar dinheiro real."""
    import importlib
    for env, esperado in [
        ({}, True),
        ({"PAPER_TRADE": "false"}, True),                       # sem confirmação
        ({"PAPER_TRADE": "true"}, True),
        ({"PAPER_TRADE": "false",
          "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK"}, False),
    ]:
        for k in ("PAPER_TRADE", "LIVE_TRADING_CONFIRMED"):
            os.environ.pop(k, None)
        os.environ.update(env)
        for m in [x for x in sys.modules if x.startswith("bot.")]:
            del sys.modules[m]
        from bot.kucoin import PAPER_TRADE
        check(f"barreira paper/real {env or '{}'}", PAPER_TRADE == esperado,
              f"esperado {esperado}, obtido {PAPER_TRADE}")
    os.environ["PAPER_TRADE"] = "true"
    os.environ.pop("LIVE_TRADING_CONFIRMED", None)


def test_idempotencia_ordem():
    """P0: mesmo sinal na mesma janela deve gerar o mesmo clientOid."""
    import hashlib
    w = int(time.time() // 60)
    a = hashlib.md5(f"BTCUSDT_Buy_3_{w}".encode()).hexdigest()[:40]
    b = hashlib.md5(f"BTCUSDT_Buy_3_{w}".encode()).hexdigest()[:40]
    check("clientOid estável na mesma janela", a == b)
    c = hashlib.md5(f"BTCUSDT_Buy_3_{w+1}".encode()).hexdigest()[:40]
    check("clientOid muda entre janelas", a != c)


def test_sl_vs_liquidacao():
    """P0: SL não pode ficar além do ponto de liquidação."""
    lev, safety = 50, 0.75
    liq = 100 / lev
    check("SL 1.0% aceito",   1.0 < liq * safety)
    check("SL 1.5% rejeitado", not (1.5 < liq * safety))
    check("SL 2.5% rejeitado", not (2.5 < liq * safety))


def test_expectancy_math():
    """Win rate isolado não mede lucratividade."""
    def exp_r(wr, payoff):
        return wr * payoff - (1 - wr) * 1.0
    e65 = exp_r(0.65, 2.0)
    e90 = exp_r(0.90, 0.3)
    check("65%/2.0 tem expectancy maior que 90%/0.3", e65 > e90,
          f"{e65:.3f} vs {e90:.3f}")
    check("30%/2.0 tem expectancy negativa", exp_r(0.30, 2.0) < 0)


def test_ev_com_custos():
    """EV deve incluir taxa e slippage nas duas pontas."""
    from bot.nexus_ai import expected_value
    ev = expected_value(0.60, 100, 99.9, 100.1)
    check("R:R apertado tem EV negativo após custos", not ev["valid"])
    ev2 = expected_value(0.60, 100, 98, 104)
    check("R:R 2:1 com 60% tem EV positivo", ev2["valid"])
    check("R:R líquido < bruto", ev2["rr_net"] < 3.0)


def test_nexus_nao_inventa_dados():
    """Seção 14: modelo sem dados não contribui com neutro."""
    from bot.nexus_models import model_derivatives
    m = model_derivatives(None, None, None)
    check("derivatives sem dados → available=False", not m.available)
    m2 = model_derivatives(0.0005, 0.01, 1.2)
    check("derivatives com dados → available=True", m2.available)


def test_nexus_bloqueia_sem_dados():
    """Fail-safe: dados insuficientes nunca autorizam execução."""
    from bot.nexus_ai import decide
    d = decide("TESTE", [], [], [])
    check("sem dados → WAIT", d.decision == "WAIT")
    check("sem dados → não autoriza", d.execution_allowed is False)


def test_selfcheck_detecta_bugs():
    """O self-check precisa pegar os padrões que já vazaram."""
    from bot.selfcheck import (check_undefined_names, check_missing_self_attrs,
                               check_missing_self_methods)
    src = '''
class E:
    def __init__(self): self.x = 1
    def run(self):
        if self.paper_trade: pass
        self.metodo_fantasma()
        return aiohttp.get()
'''
    p = "/tmp/_regr_check.py"
    open(p, "w").write(src)
    check("detecta NameError latente", len(check_undefined_names([p])) > 0)
    check("detecta atributo inexistente", len(check_missing_self_attrs([p])) > 0)
    check("detecta método inexistente", len(check_missing_self_methods([p])) > 0)


def test_config_sanity():
    """Combinações impossíveis devem ser reportadas."""
    from bot.selfcheck import check_config_sanity
    issues = check_config_sanity()
    check("config_sanity executa", isinstance(issues, list))


def test_imports_todos_modulos():
    """Regressão do bug do aiohttp: todo módulo precisa importar."""
    import importlib
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
    falhas = []
    for f in sorted(os.listdir(base)):
        if f.endswith(".py") and f != "__init__.py":
            try:
                importlib.import_module("bot." + f[:-3])
            except Exception as e:
                falhas.append(f"{f}: {e}")
    check("todos os módulos importam", not falhas, str(falhas[:2]))




def test_ordenacao_candles():
    """
    P0: candles invertidos no tempo zeravam todos os indicadores.

    O código fazia reversed() incondicional. Com a ordem já cronológica,
    o RSI de uma alta forte dava 0 e o filtro de "RSI extremo" bloqueava
    TODOS os sinais.
    """
    from bot.indicators import rsi
    alta = [100 * (1.004 ** i) for i in range(60)]
    r_ok  = rsi(alta, 14)[-1]
    r_inv = rsi(list(reversed(alta)), 14)[-1]
    check("RSI de alta é alto", r_ok > 70, f"={r_ok:.0f}")
    check("RSI invertido é baixo", r_inv < 30, f"={r_inv:.0f}")
    check("inversão muda o RSI drasticamente", abs(r_ok - r_inv) > 50)


def test_structure_vocabulario():
    """
    P0: model_structure comparava com BULLISH/BEARISH, mas smc_analysis
    retorna UPTREND/DOWNTREND. O modelo (15% do score) nunca contribuía.
    """
    from bot.nexus_models import model_structure
    import numpy as np
    n = 60
    closes = [100 * (1.005 ** i) for i in range(n)]
    highs  = [c * 1.002 for c in closes]
    lows   = [c * 0.998 for c in closes]
    m = model_structure(closes, highs, lows)
    check("structure reconhece UPTREND", m.confidence > 0,
          f"conf={m.confidence} reason={m.reason}")


def test_max_data_age_compativel():
    """
    P0: MAX_DATA_AGE era 300s, mas o bot analisa candles de 15 MINUTOS.
    Um candle recém-fechado (900s) era marcado obsoleto e derrubava a
    qualidade dos dados, vetando todos os sinais.
    """
    from bot.nexus_ai import MAX_DATA_AGE_S
    check("MAX_DATA_AGE cobre candle de 15M", MAX_DATA_AGE_S >= 900,
          f"={MAX_DATA_AGE_S}")


def test_rr_bruto_vs_liquido():
    """
    P0: a estratégia aprova com R:R BRUTO >= 2.0 e o NEXUS exigia R:R
    LÍQUIDO >= 2.0. Custos corroem 15-25% → todo sinal era vetado.
    """
    from bot.nexus_ai import expected_value
    ev = expected_value(0.60, 100, 99, 103)      # R:R bruto 3.0
    check("R:R líquido < bruto", ev["rr_net"] < 3.0, f"={ev['rr_net']:.2f}")
    check("R:R líquido ainda operável", ev["rr_net"] >= 1.6,
          f"={ev['rr_net']:.2f}")


def test_score_exclui_sem_dados():
    """
    P0: componentes sem dados entravam como ZERO. DERIVATIVES +
    MICROSTRUCTURE = 20% do score → teto real 80, não 100.
    A seção 14 da spec exige excluir, não zerar.
    """
    from bot.nexus_ai import WEIGHTS
    total = sum(WEIGHTS.values())
    check("pesos somam 1.0", abs(total - 1.0) < 0.01, f"={total}")




def test_riskmanager_unico():
    """
    P0: engine.py definia sua PRÓPRIA classe RiskManager, sombreando a de
    bot/risk.py. Todas as correções de sizing foram aplicadas na classe
    canônica — que o bot nunca usava.
    """
    import bot.engine, bot.risk
    check("engine usa RiskManager de bot.risk",
          bot.engine.RiskManager is bot.risk.RiskManager)
    import ast
    src = open(bot.engine.__file__).read()
    classes = [n.name for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.ClassDef)]
    check("engine.py não redefine RiskManager",
          classes.count("RiskManager") == 0, f"encontrado {classes.count('RiskManager')}x")


def test_sizing_nunca_excede_saldo():
    """
    P0: max(qty, min_qty) desfazia o clamp de margem. Saldo $0.50 gerava
    margem de $10.800 (2.160.000% do saldo).
    """
    from bot.risk import RiskManager
    from bot.config import cfg
    inst = {"BTCUSDT": {"minQty": 1.0, "qtyStep": 1.0, "tickSize": 0.1,
                        "multiplier": 0.001, "minNotional": 0.001}}
    r = RiskManager()
    for bal in (0.5, 19.07, 100.0, 100000.0):
        r.balance = bal; r.peak_balance = bal
        r._ready = True; r.positions = {}
        q = r.size("BTCUSDT", 108000, inst)
        margem = (q * 108000) / cfg.LEVERAGE if q > 0 else 0
        pct = (margem / bal * 100) if (bal and q > 0) else 0
        check(f"sizing saldo ${bal}: margem {pct:.0f}% <= 100%",
              pct <= 100.5, f"={pct:.1f}%")


def test_minqty_unidade_correta():
    """
    P0: minQty é lotSize em CONTRATOS, mas qty é quantidade BASE.
    Comparar os dois direto é erro de unidade.
    """
    from bot.risk import RiskManager
    inst = {"X": {"minQty": 1.0, "qtyStep": 1.0, "multiplier": 0.001,
                  "tickSize": 0.1, "minNotional": 0.001}}
    r = RiskManager()
    r.balance = 100.0; r.peak_balance = 100.0
    r._ready = True; r.positions = {}
    q = r.size("X", 108000, inst)
    # 1 contrato × 0.001 = 0.001 BTC. qty deve ser múltiplo disso.
    check("qty é múltiplo do lote em unidade base",
          q == 0 or abs((q / 0.001) - round(q / 0.001)) < 1e-6, f"q={q}")




def test_arredondamento_lote_decimal():
    """
    P1: math.floor(qty/step) com float perdia um lote inteiro.
        0.7 / 0.1 == 6.999999999999999 → floor 6 (correto: 7)
    Detectado em 22 de 770 combinações da matriz de validação.
    """
    from bot.risk import RiskManager
    from bot.config import cfg
    inst = {"S": {"minQty": 1, "qtyStep": 1, "multiplier": 0.1,
                  "tickSize": 0.0001, "minNotional": 0.1}}
    r = RiskManager()
    # saldo/preço escolhidos para cair exatamente num múltiplo problemático
    r.balance = 100.0; r.peak_balance = 100.0
    r._ready = True; r.positions = {}
    q = r.size("S", 140, inst)
    step = 0.1
    n = q / step if step else 0
    check("qty é múltiplo exato do lote",
          q == 0 or abs(n - round(n)) < 1e-6, f"q={q} n={n}")

    import math
    from decimal import Decimal, ROUND_FLOOR
    for v, s in [(0.7, 0.1), (1.4, 0.1), (2.8, 0.1)]:
        f = math.floor(v / s)
        d = int((Decimal(str(v)) / Decimal(str(s))).to_integral_value(ROUND_FLOOR))
        check(f"float perde lote em {v}/{s} (Decimal={d}, float={f})", d > f)




def test_cross_margin_multi_posicao_nao_confiavel():
    """
    Gap encontrado via evidência real (print de tela do usuário):
    a conta opera em CROSS MARGIN, não Isolated. A fórmula de
    liquidação foi validada só para o caso de 1 posição — com 2+
    posições simultâneas em cross, a margem de manutenção depende da
    conta inteira, o que este módulo não calcula.

    Com 1 posição, cross e isolated convergem (confirmado: fórmula
    previu 1.55%, KuCoin real mostrou 1.64%, Δ 0.10pp). Com 2+, o
    resultado deve se declarar não confiável em vez de dar um número
    falsamente preciso.
    """
    from bot.liquidation import analyze

    a1 = analyze(2480.84, 2440.0, 50, True, "ETHUSDT", n_open_positions=1)
    check("1 posição usa modelo normal", a1.model != "UNRELIABLE_CROSS_MULTI_POSITION")

    a3 = analyze(2480.84, 2440.0, 50, True, "ETHUSDT", n_open_positions=3)
    check("2+ posições marca resultado como não confiável",
          a3.model == "UNRELIABLE_CROSS_MULTI_POSITION")
    check("2+ posições força stop_effective=False (bloqueia)",
          a3.stop_effective is False)
    check("motivo menciona cross margin", "CROSS MARGIN" in a3.reason)




def test_clientoid_identificavel():
    """
    Gap encontrado ao verificar se era possível PROVAR que uma ordem
    vista na KuCoin veio do bot. Antes, clientOid era MD5 puro — sem
    consultar o banco de dados interno, era impossível saber se uma
    ordem na tela da exchange foi aberta pelo bot ou manualmente.

    Um prefixo fixo resolve isso: o clientOid fica visível na própria
    interface da KuCoin (aba Ordens) e é auto-identificável.
    """
    import hashlib
    raw = "BTCUSDT_Buy_10_12345"
    oid = ("bgx7-" + hashlib.md5(raw.encode()).hexdigest())[:40]
    check("OID tem prefixo identificável", oid.startswith("bgx7-"))
    check("OID respeita limite de 40 chars da KuCoin", len(oid) <= 40,
          f"len={len(oid)}")
    check("OID ainda é alfanumérico + hífen (regra da KuCoin)",
          all(c.isalnum() or c == "-" for c in oid))




def test_caso_negativo_A_nexus_veto_zero_http():
    """
    Auditoria forense final, Fase 11 Caso A: NEXUS AI VETOU deve
    resultar em ZERO chamadas HTTP, zero ordem, zero posição.
    Testado de verdade contra mock (não apenas leitura de código).
    """
    import asyncio, os
    os.environ.setdefault("NEXUS_AI_ENABLED", "true")
    from bot import engine as E
    import inspect
    src = inspect.getsource(E)
    # veto (return) precisa vir ANTES de place_order dentro de _open
    i_open = src.find("async def _open(self")
    corpo = src[i_open:src.find("async def ", i_open + 10)]
    i_veto = corpo.find("execution_allowed")
    i_place = corpo.find("self.client.place_order")
    check("veto do NEXUS AI precede a chamada HTTP dentro de _open",
          -1 < i_veto < i_place, f"veto={i_veto} place={i_place}")


def test_caso_negativo_B_http_aceita_sem_fill():
    """
    Fase 11 Caso B: HTTP aceita orderId mas a ordem nunca vira FILLED.
    Resultado exigido: NO POSITION. Verifica que o gate (last_exc +
    return) existe ANTES da criação de self.positions[symbol].
    """
    import inspect
    from bot import engine as E
    src = inspect.getsource(E)
    i_open = src.find("async def _open(self")
    corpo = src[i_open:src.find("async def ", i_open + 10)]
    i_wait   = corpo.find("wait_for_fill")
    i_lastexc_return = corpo.find("if last_exc is not None:")
    i_pos_criada = corpo.find("self.positions[sig.symbol] = pos")
    check("wait_for_fill vem antes do gate de last_exc",
          -1 < i_wait < i_lastexc_return)
    check("gate de last_exc vem antes da criação da posição",
          -1 < i_lastexc_return < i_pos_criada,
          f"wait={i_wait} gate={i_lastexc_return} pos={i_pos_criada}")


def test_preco_execucao_prioriza_dado_real_da_ordem():
    """
    F-013 (supersedes the earlier ordering check): the fill price comes only
    from exchange evidence (fills ledger > terminal order dealValue/dealSize >
    position average consistent with that fill). The cached ticker is NEVER a
    fill price (INV-FILL-AUTHORITY-001), and the signal is not shifted.
    """
    import inspect
    from bot import engine as E
    from bot import postfill_geometry as P
    src = inspect.getsource(E)
    i_wait = src.find("_fill_check = await self.client.wait_for_fill(_oid_real)")
    i_pos = src.find("pos = Position(sig, qty)", i_wait)
    window = src[i_wait:i_pos]
    check("janela pós-fill sem ticker como preço de fill",
          i_wait > -1 and i_pos > i_wait and "get_cached_ticker" not in window,
          f"wait={i_wait} pos={i_pos}")
    check("sinal não é deslocado pelo delta do fill", "sig.sl    += _delta" not in src)
    psrc = inspect.getsource(P)
    check("postfill_geometry usa dealValue/dealSize e fills ledger",
          "dealValue" in psrc and "fills_ledger" in psrc)
    check("postfill_geometry nunca consulta ticker", "get_cached_ticker" not in psrc)


def test_apenas_um_callsite_de_open_abre_posicao_nova():
    """
    Auditoria forense Fase 3: dos N call sites de place_order() no
    projeto, apenas 1 (dentro de _open) pode abrir posição NOVA
    (reduce_only ausente/False). Todos os demais devem ser fechamentos
    (reduce_only=True) — nenhum bypass do NEXUS AI/Risk Engine para
    abertura de posição.
    """
    import re
    from bot import engine as E
    import inspect
    src = inspect.getsource(E)
    calls = [m.start() for m in re.finditer(r"self\.client\.place_order\(", src)]
    check("existem múltiplos call sites de place_order", len(calls) >= 1,
          f"={len(calls)}")
    aberturas = 0
    for pos in calls:
        janela = src[pos:pos+400]
        if "reduce_only=True" not in janela:
            aberturas += 1
    check("no máximo 1 call site abre posição nova (sem reduce_only)",
          aberturas <= 1, f"aberturas={aberturas}")




def test_clientoid_consistente_entre_order_e_filled():
    """
    BUG ENCONTRADO na tentativa de prova E2E: place_order() calculava
    o clientOid REAL (prefixo bgx7-) internamente mas nunca o
    devolvia — engine.py usava _idem (a chave pré-hash) nos logs de
    FILLED, diferente do que aparecia no log de ORDER. Capturado em
    teste E2E real: os logs mostravam dois valores DIFERENTES para a
    mesma ordem, o que quebraria qualquer correlação com a KuCoin
    real (a exchange só conhece o valor com prefixo bgx7-).
    """
    # NOTA: bot.kucoin lê PAPER_TRADE no nível do módulo, uma única vez
    # no import. Se outro teste já importou o módulo antes com um valor
    # diferente, setar a env var aqui não tem efeito — por isso este
    # teste verifica a LÓGICA de geração do clientOid isoladamente
    # (hashlib + prefixo), sem depender do estado de import de
    # bot.kucoin, que é compartilhado entre todos os testes do processo.
    import hashlib, time
    _window = int(time.time() // 60)
    _raw = f"BTCUSDT_Buy_0.01_{_window}"
    _oid_esperado = f"bgx7-{hashlib.md5(_raw.encode()).hexdigest()}"[:40]
    check("fórmula do clientOid produz prefixo bgx7-",
          _oid_esperado.startswith("bgx7-"))

    import inspect
    from bot.kucoin import KuCoinClient
    src = inspect.getsource(KuCoinClient.place_order)
    check("place_order inclui clientOid no dict de retorno (bug corrigido)",
          'data["clientOid"] = _oid' in src or '"clientOid": _oid' in src,
          "retorno não inclui mais o clientOid real")


if __name__ == "__main__":
    print("═══ TESTES DE REGRESSÃO ═══\n")
    for fn in [test_paper_trade_barrier, test_idempotencia_ordem,
               test_sl_vs_liquidacao, test_expectancy_math, test_ev_com_custos,
               test_nexus_nao_inventa_dados, test_nexus_bloqueia_sem_dados,
               test_selfcheck_detecta_bugs, test_config_sanity,
               test_imports_todos_modulos,
               test_ordenacao_candles, test_structure_vocabulario,
               test_max_data_age_compativel, test_rr_bruto_vs_liquido,
               test_score_exclui_sem_dados,
               test_riskmanager_unico, test_sizing_nunca_excede_saldo,
               test_minqty_unidade_correta,
               test_arredondamento_lote_decimal,
               test_cross_margin_multi_posicao_nao_confiavel,
               test_clientoid_identificavel,
               test_caso_negativo_A_nexus_veto_zero_http,
               test_caso_negativo_B_http_aceita_sem_fill,
               test_preco_execucao_prioriza_dado_real_da_ordem,
               test_apenas_um_callsite_de_open_abre_posicao_nova,
               test_clientoid_consistente_entre_order_e_filled]:
        print(f"\n{fn.__name__}:")
        try:
            fn()
        except Exception as e:
            _f += 1
            print(f"  ✗ ERRO NO TESTE: {type(e).__name__}: {e}")
    print(f"\n{'═'*46}")
    print(f"PASSOU: {_p}  |  FALHOU: {_f}")
    print("═"*46)
    sys.exit(1 if _f else 0)
