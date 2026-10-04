"""Fail-closed gate for the controlled real-money pilot."""
import os
import time
import threading
from dataclasses import dataclass, field
from typing import List

from bot.logger import log
from bot.financial_state import FinancialStateInvalid, validate_financial_state
from bot.market_data_health import MarketDataHealth
from bot.private_stream_health import PrivateStreamHealth

PILOT_ENABLED = os.environ.get("REAL_TRADING_PILOT", "").strip().lower() == "true"
PILOT_RELEASE_TOKEN = "I_APPROVE_TWO_LIVE_PILOT_ORDERS"


def _paper_trade_enabled() -> bool:
    return os.environ.get("PAPER_TRADE", "true").strip().lower() == "true"


def _release_approved() -> bool:
    return os.environ.get("PILOT_RELEASE_APPROVED", "").strip() == PILOT_RELEASE_TOKEN


# Controlled pilot limits are explicit runtime configuration. Invalid values
# fail closed at import time rather than silently widening LIVE authority.
PILOT_MAX_CONCURRENT_POSITIONS = int(os.environ.get("PILOT_MAX_CONCURRENT_POSITIONS", "2"))
MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION = int(os.environ.get("PILOT_MAX_NEW_ORDER_SUBMISSIONS", "2"))
if PILOT_MAX_CONCURRENT_POSITIONS < 1:
    raise ValueError("PILOT_MAX_CONCURRENT_POSITIONS must be >= 1")
if MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION < 1:
    raise ValueError("PILOT_MAX_NEW_ORDER_SUBMISSIONS must be >= 1")
PILOT_MAX_NEW_POSITIONS_SESSION = MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION
PILOT_MAX_MARKET_DATA_AGE_S = float(os.environ.get("PILOT_MAX_MARKET_DATA_AGE_S", "120"))


_MARKET_CHECK_LOG_INTERVAL_S = 60.0
_market_check_log_state = {"key": None, "at": 0.0}


def market_data_blockers(client) -> List[str]:
    """Gate 11: public market-data freshness from the canonical authority.

    Binance clients own a ``MarketDataHealth`` (monotonic, written only by the
    public websocket handler after a validated event). Legacy clients (KuCoin,
    test doubles) keep the historical ``_last_ws_update`` contract unchanged.
    Both paths fail closed: never received -> BLOCK, older than the limit ->
    BLOCK. The limit is ``PILOT_MAX_MARKET_DATA_AGE_S`` (unchanged, 120 s).
    """
    limit = PILOT_MAX_MARKET_DATA_AGE_S
    health = getattr(client, "market_data_health", None)
    if isinstance(health, MarketDataHealth):
        ok, reason, age = health.check(limit)
        _log_market_check(client, health, ok, reason, age)
        if ok:
            return []
        if reason == "no_market_data":
            return ["11_MARKET_DATA: nenhum dado de mercado recebido (reason=no_market_data)"]
        if reason == "stale_market_data":
            return [
                f"11_MARKET_DATA: dado com {age:.0f}s "
                f"(máx {limit:.0f}s) reason=stale_market_data"
            ]
        return [f"11_MARKET_DATA: frescor inválido reason={reason}"]

    last_ws = float(getattr(client, "_last_ws_update", 0) or 0)
    if last_ws <= 0:
        return ["11_MARKET_DATA: nenhum dado de mercado recebido"]
    age = time.time() - last_ws
    if age > limit:
        return [f"11_MARKET_DATA: dado com {age:.0f}s (máx {limit:.0f}s)"]
    return []


def private_stream_blockers(client) -> List[str]:
    """Gate 14 (Binance): live private user-data stream must be event-capable.

    Reads the canonical ``client.private_stream_health``. Clients without it
    (KuCoin/test doubles) keep the historical registry-only check above.
    """
    health = getattr(client, "private_stream_health", None)
    if not isinstance(health, PrivateStreamHealth):
        return []
    ok, reason = health.check()
    if ok:
        return []
    return [f"14_WS: stream privado não apto (reason={reason})"]


def _log_market_check(client, health, ok, reason, age) -> None:
    result = "PASS" if ok else "BLOCK"
    key = (result, reason, health.instance_id)
    now = time.monotonic()
    state = _market_check_log_state
    if key == state["key"] and now - state["at"] < _MARKET_CHECK_LOG_INTERVAL_S:
        return
    state["key"], state["at"] = key, now
    log.info(
        "[PILOT_MARKET_DATA_CHECK] client_type=%s.%s client_instance=%s "
        "last_ws_update=%.3f age_s=%s limit_s=%.0f result=%s reason=%s",
        type(client).__module__, type(client).__qualname__, health.instance_id,
        health.last_update_wall, "NA" if age is None else f"{age:.1f}",
        PILOT_MAX_MARKET_DATA_AGE_S, result, reason,
    )


@dataclass
class PilotState:
    new_order_submissions_this_session: int = 0
    positions_opened_this_session: int = 0
    first_order_ts: float = 0.0
    blocked_reasons: List[str] = field(default_factory=list)


def _venue_credential_blockers() -> List[str]:
    """Presence/parse check of the ACTIVE venue's credentials; never logs values.

    Previously this always read KuCoin key/secret/passphrase, which on
    EXCHANGE=binance either blocked every entry or passed on unrelated
    leftover KuCoin secrets without proving Binance signing material.
    """
    from bot import exchange

    if exchange.is_binance():
        from bot import binance

        try:
            binance._assert_signing_credentials_available()
        except Exception as exc:  # noqa: BLE001 - any failure is a fail-closed blocker
            code = str(exc) if str(exc).startswith("BINANCE_") else type(exc).__name__
            return [f"1_AUTH: credenciais Binance indisponíveis ({code})"]
        return []

    from bot.kucoin import API_KEY, API_SECRET, API_PASSPHRASE

    if not (API_KEY and API_SECRET and API_PASSPHRASE):
        return ["1_AUTH: credenciais KuCoin ausentes"]
    return []


def _drawdown_hard_gate_blocks(engine) -> tuple[bool, str]:
    """Return whether legacy gate 9B must still block this candidate.

    Recovery is allowed to bridge only the legacy drawdown-threshold flag, and
    only when the immediately preceding fresh pre-dispatch check minted a
    one-shot durable episode token. The token is consumed here so it cannot be
    reused by a later candidate. All mismatches fail closed.
    """
    if not bool(getattr(engine, "_drawdown_hard_gate_active", False)):
        return False, "hard_gate_inactive"

    episode = getattr(engine, "_drawdown_recovery_predispatch_episode", None)
    if not episode:
        return True, "missing_durable_recovery_token"

    # One-shot binding: consume before validation so exceptions/mismatches
    # cannot leave a reusable authorization behind.
    engine._drawdown_recovery_predispatch_episode = None

    risk = getattr(engine, "risk", None)
    legacy = getattr(risk, "_legacy", risk)
    candidates = [getattr(legacy, "drawdown", None)]
    v3 = getattr(risk, "_v3", None)
    if v3 is not None and getattr(v3, "confirmed", False):
        candidates.append(getattr(v3, "drawdown", None))
    try:
        values = [float(v) for v in candidates if v is not None]
    except (TypeError, ValueError):
        return True, "drawdown_unreadable"
    if not values or any(v != v or v < 0 or v == float("inf") for v in values):
        return True, "drawdown_unreadable"

    try:
        from bot.drawdown_recovery import threshold_decision
        allowed, reason, recovery = threshold_decision(max(values))
    except Exception as exc:
        return True, f"recovery_recheck_{type(exc).__name__}"

    if not (allowed and reason == "recovery_threshold_exception"):
        return True, f"recovery_recheck_{reason}"
    if str(getattr(recovery, "episode_id", "")) != str(episode):
        return True, "recovery_episode_mismatch"
    return False, f"recovery_episode={episode}"


class PilotGuard:
    """Additional fail-closed gates for real pilot execution."""

    def __init__(self):
        self.state = PilotState()
        self._submission_lock = threading.Lock()
        self._last_block_log = 0.0
        self._last_block_key = ""

    @property
    def enabled(self) -> bool:
        return PILOT_ENABLED and not _paper_trade_enabled()

    def reserve_submission(self, symbol: str) -> bool:
        """Atomically reserve a configured session submission slot before dispatch."""
        if not self.enabled:
            return True
        with self._submission_lock:
            if self.state.new_order_submissions_this_session >= MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION:
                log.warning(f"[PILOT] {symbol} submission cap reached ({MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION})")
                return False
            self.state.new_order_submissions_this_session += 1
            self.state.first_order_ts = time.time()
            reserved = self.state.new_order_submissions_this_session
        log.critical(
            f"[PILOT] symbol={symbol} submission_reserved={reserved}/"
            f"{MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION} session"
        )
        return True

    def register_position_opened(self, symbol: str):
        if not self.enabled:
            return
        self.state.positions_opened_this_session += 1
        if self.state.first_order_ts == 0.0:
            self.state.first_order_ts = time.time()
        log.critical(
            f"🚁 [PILOT] posição aberta em {symbol} — "
            f"{self.state.positions_opened_this_session}/"
            f"{PILOT_MAX_NEW_POSITIONS_SESSION} desta sessão."
        )

    def evaluate(self, engine, client, symbol: str, ai_decision=None) -> List[str]:
        """Return pilot blockers; an empty list means the pilot gate passes."""
        r: List[str] = []
        try:
            r.extend(_venue_credential_blockers())

            if os.environ.get("PILOT_ACCOUNT_CONFIRMED", "").strip().lower() != "true":
                r.append(
                    "2_ACCOUNT: conta real não confirmada — defina "
                    "PILOT_ACCOUNT_CONFIRMED=true após verificar que as credenciais "
                    "pertencem à conta pretendida"
                )

            if not _release_approved():
                r.append(
                    "2B_RELEASE: autorização explícita do piloto ausente; "
                    "PILOT_RELEASE_APPROVED deve corresponder ao token de release"
                )

            bal = float(getattr(engine.risk, "balance", 0) or 0)
            if bal <= 0:
                r.append(f"3_BALANCE: saldo Futures USDT = {bal}")

            try:
                risk_state = getattr(engine, "risk", None)
                peak = float(getattr(risk_state, "peak_balance", 0) or 0)
                drawdown = float(getattr(risk_state, "drawdown", 0) or 0)
                available_margin = float(getattr(risk_state, "available_margin", None) or getattr(risk_state, "available_balance", None) or bal)
                validate_financial_state(equity=bal, available_margin=available_margin, hwm=peak, drawdown=drawdown)
            except (FinancialStateInvalid, TypeError, ValueError) as exc:
                log.critical("[FINANCIAL_STATE_INVALID] symbol=%s evidence=%r execution_effect=BLOCK_NEW_ENTRIES reconciliation_required=true", symbol, str(exc))
                r.append(f"3B_FINANCIAL_STATE_INVALID: {exc}")

            if not getattr(engine, "viable_symbols", None):
                r.append("4_VIABLE: viable_symbols vazio")

            inst = getattr(engine, "instruments", None) or {}
            if not inst:
                r.append("5_INSTRUMENTS: metadata não carregada")
            elif symbol and symbol not in inst:
                r.append(f"5_INSTRUMENTS: {symbol} ausente na metadata")

            ig = getattr(engine, "integrity", None)
            if ig is not None:
                codes = ig.state.codes() if hasattr(ig, "state") else []
                if "STATE_DIVERGENCE" in codes:
                    r.append(f"6_DIVERGENCE: {ig.block_reason()[:120]}")
            else:
                r.append("6_DIVERGENCE: IntegrityGuard indisponível")

            unprot = set(getattr(engine, "_unprotected_symbols", set()) or set())
            if unprot:
                r.append(f"7_8_UNPROTECTED: {sorted(unprot)}")

            risk = getattr(engine, "risk", None)
            if risk is None or not getattr(risk, "_ready", False):
                r.append("9_RISK: RiskManager não inicializado")
            drawdown_blocks, drawdown_reason = _drawdown_hard_gate_blocks(engine)
            if drawdown_blocks:
                r.append("9B_DRAWDOWN: HARD_GATE ativo; novas entradas bloqueadas")
            elif drawdown_reason.startswith("recovery_episode="):
                log.critical(
                    "[PILOT_DRAWDOWN_RECOVERY_BRIDGE] symbol=%s result=PASS %s "
                    "scope=9B_DRAWDOWN_ONLY durable_token_consumed=true "
                    "other_gates_unchanged=true",
                    symbol, drawdown_reason,
                )

            if ai_decision is None:
                r.append("10_AI: nenhuma decisão do NEXUS AI recebida")
            elif getattr(ai_decision, "execution_allowed", None) is not True:
                r.append("10_AI: NEXUS AI não aprovou a entrada")

            r.extend(market_data_blockers(client))

            if symbol and symbol in inst:
                meta = inst[symbol]
                missing = [k for k in ("minQty", "multiplier") if not meta.get(k)]
                if missing:
                    r.append(f"12_QTY_RULES: metadata incompleta {missing}")

            reg = getattr(engine, "orders", None)
            if reg is not None:
                try:
                    for mo in reg.pending_orders():
                        r.append(
                            f"13_AMBIGUOUS: ordem pendente {mo.client_oid[:12]} "
                            f"em {mo.symbol} (estado {mo.state.value})"
                        )
                        break
                except Exception as exc:
                    r.append(f"13_AMBIGUOUS: falha ao consultar registry: {exc}")

            if getattr(client, "_order_registry", None) is None:
                r.append("14_WS: WS privado de ordens não inicializado")
            r.extend(private_stream_blockers(client))

            n_pos = len(getattr(engine, "positions", {}) or {})
            if n_pos >= PILOT_MAX_CONCURRENT_POSITIONS:
                r.append(
                    f"PILOT_CONCURRENT: {n_pos} posição(ões) aberta(s), "
                    f"máx {PILOT_MAX_CONCURRENT_POSITIONS} no piloto"
                )
            if self.state.new_order_submissions_this_session >= MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION:
                r.append(
                    f"PILOT_SESSION: {self.state.new_order_submissions_this_session}/"
                    f"{PILOT_MAX_NEW_POSITIONS_SESSION} ordens já abertas nesta sessão"
                )
        except Exception as exc:
            r.append(f"PILOT_EVAL_ERROR: {type(exc).__name__}: {exc}")

        self.state.blocked_reasons = r
        return r

    def can_open_pilot(self, engine, client, symbol: str, ai_decision=None) -> bool:
        if not self.enabled:
            self.state.blocked_reasons = []
            return True
        motivos = self.evaluate(engine, client, symbol, ai_decision)
        if motivos:
            self._log_block(symbol, motivos)
            return False
        return True

    def _log_block(self, symbol: str, motivos: List[str]):
        key = f"{symbol}|{'|'.join(sorted(motivos))}"
        now = time.time()
        if key != self._last_block_key or now - self._last_block_log >= 60.0:
            self._last_block_key = key
            self._last_block_log = now
            log.warning(
                f"🚁 [PILOT] {symbol} BLOQUEADO — {len(motivos)} "
                f"pré-condição(ões) não satisfeita(s): " + " | ".join(motivos[:5])
            )

    def status(self, engine=None, client=None) -> dict:
        return {
            "pilot_configured": PILOT_ENABLED,
            "pilot_enabled": self.enabled,
            "paper_trade": _paper_trade_enabled(),
            "release_approved": _release_approved(),
            "max_concurrent_positions": PILOT_MAX_CONCURRENT_POSITIONS,
            "max_new_order_submissions_session": MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION,
            "new_order_submissions_this_session": self.state.new_order_submissions_this_session,
            "positions_opened_this_session": self.state.positions_opened_this_session,
            "blocked_reasons": list(self.state.blocked_reasons),
        }
