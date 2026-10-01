"""
NEXUS-7 — INTEGRITY GUARD (Fase 3, P0)

Barreira única e explícita contra novas entradas quando o estado da
exchange não pode ser confirmado.

PRINCÍPIO: EXCHANGE = SOURCE OF TRUTH.
Qualquer divergência entre estado local e exchange bloqueia NOVAS
ENTRADAS — nunca abandona o gerenciamento de posições existentes.

Posições abertas manualmente/externamente na exchange são classificadas
explicitamente como EXTERNAL_POSITION_PROTECTED ou
EXTERNAL_POSITION_UNPROTECTED. Elas NÃO são tratadas como
STATE_DIVERGENCE. Ambas continuam bloqueando novas entradas por padrão
porque não são gerenciadas pelo NEXUS-7; a proteção por stop melhora a
classificação, mas não autoriza empilhar risco sem uma política explícita
de coexistência e validação de exposição/margem.

FAIL-CLOSED: na dúvida, bloqueia. A ausência de informação nunca é
tratada como "está tudo bem".

Uso:
    guard = IntegrityGuard()
    await guard.assess(client, engine)      # avalia e registra
    if not guard.can_open_new():
        return                              # bloqueado, com motivo
"""
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List

from bot.conditional_stop_protection import conditional_stop_confirmed, inline_stop_confirmed
from bot.logger import log


class Severity(str, Enum):
    OK       = "OK"
    DEGRADED = "DEGRADED"   # opera, mas com ressalva
    BLOCKED  = "BLOCKED"    # não abre novas posições


TTL = {
    "balance":    float(os.environ.get("TTL_BALANCE",    "120")),
    "positions":  float(os.environ.get("TTL_POSITIONS",  "120")),
    "price":      float(os.environ.get("TTL_PRICE",       "60")),
    "candles":    float(os.environ.get("TTL_CANDLES",   "1800")),
    "instruments":float(os.environ.get("TTL_INSTRUMENTS","86400")),
    "clock":      float(os.environ.get("TTL_CLOCK",     "3600")),
}

MAX_CLOCK_SKEW_MS = float(os.environ.get("MAX_CLOCK_SKEW_MS", "5000"))


@dataclass
class IntegrityIssue:
    code:     str
    severity: Severity
    detail:   str
    ts:       float = field(default_factory=time.time)


@dataclass
class IntegrityState:
    """Snapshot da última avaliação."""
    severity:      Severity = Severity.OK
    issues:        List[IntegrityIssue] = field(default_factory=list)
    checked_at:    float = 0.0
    exchange_known: bool = False

    @property
    def blocked(self) -> bool:
        return self.severity == Severity.BLOCKED

    def codes(self) -> List[str]:
        return [i.code for i in self.issues]


class IntegrityGuard:
    """Avalia integridade e decide se novas entradas são permitidas."""

    def __init__(self):
        self.state = IntegrityState()
        self._last_block_log = 0.0
        self._consec_failures = 0

    async def assess(self, client, engine) -> IntegrityState:
        issues: List[IntegrityIssue] = []
        exchange_known = True

        def add(code, sev, detail):
            issues.append(IntegrityIssue(code, sev, detail))

        try:
            bal = await client.get_balance()
            if bal is None or bal < 0:
                add("BALANCE_UNCONFIRMED", Severity.BLOCKED,
                    f"saldo não confirmado (retorno={bal})")
                exchange_known = False
            elif bal == 0:
                add("BALANCE_ZERO", Severity.BLOCKED, "saldo zero")
        except Exception as e:
            add("REST_UNAVAILABLE", Severity.BLOCKED,
                f"REST indisponível: {type(e).__name__}: {e}")
            exchange_known = False

        ex_positions = None
        try:
            ex_positions = await client.get_positions()
            if ex_positions is None:
                add("POSITIONS_UNCONFIRMED", Severity.BLOCKED,
                    "get_positions retornou None")
                exchange_known = False
        except Exception as e:
            add("POSITIONS_UNCONFIRMED", Severity.BLOCKED,
                f"posições não confirmadas: {type(e).__name__}: {e}")
            exchange_known = False

        # KuCoin can keep a manually-created stop as a separate conditional
        # order rather than embedding it in the position payload. Resolve that
        # protection through a READ-ONLY /stopOrders query. Failure to read or
        # ambiguous/partial coverage remains unprotected (fail closed).
        protection: dict[str, tuple[bool, str]] = {}
        if ex_positions:
            for position in ex_positions:
                try:
                    if abs(float(position.get("size", 0) or 0)) <= 0:
                        continue
                    sym = str(position.get("symbol", "") or "")
                    if not sym:
                        continue
                    if inline_stop_confirmed(position):
                        protection[sym] = (True, "inline_stop")
                        continue
                    protected, evidence = await conditional_stop_confirmed(client, position)
                    protection[sym] = (protected, evidence)
                    log.info(
                        "[CONDITIONAL_STOP_READONLY] symbol=%s confirmed=%s "
                        "evidence=%s action=read_only execution_effect=NONE",
                        sym, str(bool(protected)).lower(), evidence,
                    )
                except Exception as e:
                    sym = str(position.get("symbol", "?") or "?") if isinstance(position, dict) else "?"
                    protection[sym] = (False, "validation_exception")
                    log.warning(
                        "[CONDITIONAL_STOP_READONLY] symbol=%s confirmed=false "
                        "evidence=validation_exception error=%s action=read_only "
                        "execution_effect=NONE",
                        sym, type(e).__name__,
                    )

        def has_confirmed_stop(position: dict) -> bool:
            if self._position_has_confirmed_stop(position):
                return True
            sym = str(position.get("symbol", "") or "") if isinstance(position, dict) else ""
            return bool(protection.get(sym, (False, ""))[0])

        if ex_positions is not None:
            div = self._reconcile(engine, ex_positions)
            for d in div:
                add("STATE_DIVERGENCE", Severity.BLOCKED, d)

            try:
                external_positions = self._external_position_map_strict(engine, ex_positions)
            except Exception as exc:
                # Unreadable external exposure must never be reported as
                # "no external positions" (previously a silent {} fail-open).
                external_positions = {}
                add(
                    "EXTERNAL_POSITIONS_UNREADABLE",
                    Severity.BLOCKED,
                    f"posições externas ilegíveis ({type(exc).__name__}); "
                    "exposição externa não verificável",
                )
            for sym, position in external_positions.items():
                if has_confirmed_stop(position):
                    evidence = protection.get(sym, (True, "inline_stop"))[1]
                    add(
                        "EXTERNAL_POSITION_PROTECTED",
                        Severity.BLOCKED,
                        f"{sym}: posição externa protegida por stop confirmado "
                        f"({evidence}), mas não gerenciada pelo NEXUS-7; exposição continua ativa",
                    )
                else:
                    add(
                        "EXTERNAL_POSITION_UNPROTECTED",
                        Severity.BLOCKED,
                        f"{sym}: posição externa sem stop confirmado e não gerenciada pelo NEXUS-7",
                    )

        if ex_positions:
            for p in ex_positions:
                try:
                    if abs(float(p.get("size", 0) or 0)) <= 0:
                        continue
                    if not has_confirmed_stop(p):
                        add("POSITION_WITHOUT_STOP", Severity.BLOCKED,
                            f"{p.get('symbol')} aberta SEM stop confirmado "
                            f"na exchange")
                except Exception as e:
                    add("POSITION_UNREADABLE", Severity.BLOCKED, str(e))

        try:
            inst = client.get_instruments()
            if not inst:
                add("INSTRUMENTS_MISSING", Severity.BLOCKED,
                    "nenhum instrumento carregado")
                exchange_known = False
        except Exception as e:
            add("INSTRUMENTS_MISSING", Severity.BLOCKED, str(e))
            exchange_known = False

        skew = getattr(client, "_time_offset_ms", None)
        if skew is None:
            add("CLOCK_UNSYNCED", Severity.DEGRADED,
                "offset de relógio desconhecido")
        elif abs(skew) > MAX_CLOCK_SKEW_MS:
            add("CLOCK_SKEW", Severity.BLOCKED,
                f"desvio de relógio {skew:.0f}ms > {MAX_CLOCK_SKEW_MS:.0f}ms")

        try:
            last_ws = getattr(client, "_last_ws_update", 0) or 0
            if last_ws:
                age = time.time() - last_ws
                if age > TTL["candles"]:
                    add("MARKET_DATA_STALE", Severity.BLOCKED,
                        f"último dado de mercado há {age/60:.1f}min")
                elif age > TTL["price"] * 5:
                    add("WS_LAGGING", Severity.DEGRADED,
                        f"WS atrasado {age:.0f}s")
            else:
                add("WS_NEVER_CONNECTED", Severity.DEGRADED,
                    "WebSocket nunca entregou dados")
        except Exception as e:
            add("WS_UNKNOWN", Severity.DEGRADED, str(e))

        try:
            risk = getattr(engine, "risk", None)
            if risk is None or not getattr(risk, "_ready", False):
                add("RISK_ENGINE_UNAVAILABLE", Severity.BLOCKED,
                    "Risk Engine não inicializado")
        except Exception as e:
            add("RISK_ENGINE_UNAVAILABLE", Severity.BLOCKED, str(e))

        # RATE_LIMITED must represent a current exchange condition, not a
        # lifetime process counter. BinanceClient exposes a rolling window and
        # shared cooldown; retain the historical counter only as a fallback for
        # older adapters.
        rate_status = None
        try:
            status_reader = getattr(client, "rate_limit_status", None)
            if callable(status_reader):
                rate_status = status_reader()
        except Exception:
            rate_status = None

        if isinstance(rate_status, dict):
            n429 = int(rate_status.get("recent_hits", 0) or 0)
            window_s = float(rate_status.get("window_seconds", 0) or 0)
            detail = (
                f"{n429} respostas 429 nos últimos {window_s:.0f}s"
                if window_s > 0
                else f"{n429} respostas 429 recentes"
            )
        else:
            n429 = int(getattr(client, "_rate_limit_hits", 0) or 0)
            detail = f"{n429} respostas 429 recentes"

        if n429 >= int(os.environ.get("RATE_LIMIT_BLOCK_AFTER", "5")):
            add("RATE_LIMITED", Severity.BLOCKED, detail)

        if any(i.severity == Severity.BLOCKED for i in issues):
            sev = Severity.BLOCKED
        elif any(i.severity == Severity.DEGRADED for i in issues):
            sev = Severity.DEGRADED
        else:
            sev = Severity.OK

        self.state = IntegrityState(
            severity=sev, issues=issues,
            checked_at=time.time(), exchange_known=exchange_known,
        )
        self._log_state()
        return self.state

    def _exchange_position_map(self, ex_positions: list) -> dict:
        """Normaliza apenas posições efetivamente abertas na exchange."""
        ex = {}
        for p in ex_positions:
            sym = p.get("symbol")
            sz = abs(float(p.get("size", 0) or 0))
            if sym and sz > 0:
                ex[sym] = p
        return ex

    @staticmethod
    def _position_has_confirmed_stop(position: dict) -> bool:
        """True quando o payload da posição traz stopLoss explícito positivo."""
        return inline_stop_confirmed(position)

    def _external_position_map_strict(self, engine, ex_positions: list) -> dict:
        """Mapeia posições externas; propaga erro de leitura (fail-closed no assess)."""
        ex = self._exchange_position_map(ex_positions)
        local = dict(getattr(engine, "positions", {}) or {})
        return {sym: ex[sym] for sym in sorted(ex) if sym not in local}

    def _external_position_map(self, engine, ex_positions: list) -> dict:
        """Compatibilidade: versão tolerante usada só para listagem de símbolos."""
        try:
            return self._external_position_map_strict(engine, ex_positions)
        except Exception:
            return {}

    def _external_positions(self, engine, ex_positions: list) -> List[str]:
        """Compatibilidade: retorna apenas os símbolos das posições externas."""
        return list(self._external_position_map(engine, ex_positions))

    def _reconcile(self, engine, ex_positions: list) -> List[str]:
        """
        Compara estado local gerenciado pelo NEXUS-7 com a exchange.
        Posição apenas na exchange é externa, não STATE_DIVERGENCE.
        """
        div = []
        try:
            ex = self._exchange_position_map(ex_positions)
            local = dict(getattr(engine, "positions", {}) or {})

            for sym in local:
                if sym not in ex:
                    div.append(
                        f"{sym}: registrada localmente mas INEXISTENTE na "
                        f"exchange (posição fantasma)"
                    )

            _tol_qty   = float(os.environ.get("RECON_QTY_TOL",   "0.02"))
            _tol_price = float(os.environ.get("RECON_PRICE_TOL", "0.01"))
            for sym in set(local) & set(ex):
                lp, xp = local[sym], ex[sym]
                lq = abs(float(getattr(lp, "qty", 0) or 0))
                xq_contratos = abs(float(xp.get("size", 0) or 0))
                try:
                    xq = (
                        xq_contratos
                        if xp.get("sizeUnit") == "BASE_ASSET"
                        else engine._contracts_to_base_qty(sym, xq_contratos)
                    )
                except Exception:
                    div.append(
                        f"{sym}: multiplier indisponível — impossível "
                        f"comparar qty local ({lq}) com exchange "
                        f"({xq_contratos} contratos)"
                    )
                    continue

                if lq > 0 and xq > 0 and abs(lq - xq) / max(lq, xq) > _tol_qty:
                    div.append(
                        f"{sym}: qty local {lq} ≠ exchange {xq} "
                        f"(size={xq_contratos}, unit={xp.get('sizeUnit', 'CONTRACTS')})"
                    )

                _side_ex = "LONG" if xp.get("side", "Buy") == "Buy" else "SHORT"
                _side_local = getattr(lp, "direction", "")
                if _side_local and _side_ex != _side_local:
                    div.append(
                        f"{sym}: SIDE divergente — local {_side_local}, "
                        f"exchange {_side_ex}"
                    )

                le = float(getattr(lp, "entry", 0) or 0)
                xe = float(xp.get("entryPrice", 0) or 0)
                if le > 0 and xe > 0 and abs(le - xe) / xe > _tol_price:
                    div.append(
                        f"{sym}: entry local {le:.6f} ≠ exchange {xe:.6f}"
                    )
        except Exception as e:
            div.append(f"falha ao reconciliar: {type(e).__name__}: {e}")
        return div

    def can_open_new(self) -> bool:
        if self.state.checked_at <= 0:
            return False
        idade = time.time() - self.state.checked_at
        if idade > float(os.environ.get("INTEGRITY_MAX_AGE", "300")):
            return False
        return not self.state.blocked

    def block_reason(self) -> str:
        if self.state.checked_at <= 0:
            return "integridade nunca avaliada (fail-closed)"
        idade = time.time() - self.state.checked_at
        if idade > float(os.environ.get("INTEGRITY_MAX_AGE", "300")):
            return f"avaliação de integridade obsoleta ({idade:.0f}s)"
        blk = [i for i in self.state.issues if i.severity == Severity.BLOCKED]
        if not blk:
            return ""
        return " | ".join(f"{i.code}: {i.detail}" for i in blk[:4])

    def _log_state(self):
        s = self.state
        if s.severity == Severity.BLOCKED:
            now = time.time()
            if now - self._last_block_log > 60:
                self._last_block_log = now
                log.error(
                    f"🚫 INTEGRIDADE: NOVAS ENTRADAS BLOQUEADAS — "
                    f"{self.block_reason()}"
                )
        elif s.severity == Severity.DEGRADED:
            log.debug(f"⚠️ Integridade degradada: {', '.join(s.codes())}")

    def to_dict(self) -> dict:
        s = self.state
        return {
            "severity":        s.severity.value,
            "can_open_new":    self.can_open_new(),
            "block_reason":    self.block_reason(),
            "exchange_known":  s.exchange_known,
            "checked_at":      s.checked_at,
            "age_seconds":     round(time.time() - s.checked_at, 1) if s.checked_at else None,
            "issues": [
                {"code": i.code, "severity": i.severity.value, "detail": i.detail}
                for i in s.issues
            ],
        }
