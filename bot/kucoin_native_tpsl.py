"""KuCoin Futures native TP/SL hardening for protected LIVE entries.

Protected opening orders use KuCoin's native ``POST /api/v1/st-orders``
endpoint. Before dispatch, the symbol's margin mode is read and, when needed,
changed to CROSS using KuCoin's documented margin-mode endpoint. Unknown or
unverifiable mode fails closed before any entry order is sent.

Existing AI, risk, EV/RR, pilot, durable-submission and exposure gates remain
unchanged. Leverage is always taken from ``cfg.LEVERAGE``.
"""
from __future__ import annotations

from bot.config import cfg

TPSL_ENDPOINT = "/api/v1/st-orders"
GET_MARGIN_MODE_ENDPOINT = "/api/v2/position/getMarginMode"
CHANGE_MARGIN_MODE_ENDPOINT = "/api/v2/position/changeMarginMode"
CROSS = "CROSS"


def _trigger_prices(side: str, sl: float, tp: float) -> tuple[float, float]:
    """Return (trigger_up, trigger_down) for a one-way futures entry."""
    side_l = str(side or "").strip().lower()
    if side_l == "buy":
        return float(tp or 0), float(sl or 0)
    if side_l == "sell":
        return float(sl or 0), float(tp or 0)
    raise ValueError(f"unsupported KuCoin order side: {side}")


async def _ensure_cross_margin(self, kucoin_module, symbol: str, log) -> bool:
    """Verify/switch this symbol to CROSS before a protected LIVE entry.

    Switching is attempted only after the normal NEXUS execution gates have
    reached ``place_order``. A failed read, failed switch, or failed verify
    blocks the entry. No order is dispatched while margin mode is uncertain.
    """
    kc_symbol = kucoin_module.to_kucoin(symbol)
    try:
        current = await self._get(
            GET_MARGIN_MODE_ENDPOINT,
            {"symbol": kc_symbol},
            auth=True,
        )
    except Exception as exc:
        log.error(
            "[KUCOIN_MARGIN_MODE] BLOCK symbol=%s reason=query_failed error=%s",
            symbol, exc,
        )
        return False

    mode = str((current or {}).get("marginMode", "") or "").upper()
    if mode == CROSS:
        return True
    if not mode:
        log.error(
            "[KUCOIN_MARGIN_MODE] BLOCK symbol=%s reason=mode_unconfirmed",
            symbol,
        )
        return False

    log.warning(
        "[KUCOIN_MARGIN_MODE] symbol=%s current=%s requested=CROSS action=switch",
        symbol, mode,
    )
    try:
        changed = await self._post(
            CHANGE_MARGIN_MODE_ENDPOINT,
            {"symbol": kc_symbol, "marginMode": CROSS},
            single_attempt=True,
        )
    except Exception as exc:
        log.error(
            "[KUCOIN_MARGIN_MODE] BLOCK symbol=%s reason=switch_failed error=%s",
            symbol, exc,
        )
        return False

    if not isinstance(changed, dict) or str(changed.get("marginMode", "") or "").upper() != CROSS:
        log.error(
            "[KUCOIN_MARGIN_MODE] BLOCK symbol=%s reason=switch_not_confirmed response=%s",
            symbol, str(changed)[:160],
        )
        return False

    # Independent read-back prevents trusting a mutation response blindly.
    try:
        verify = await self._get(
            GET_MARGIN_MODE_ENDPOINT,
            {"symbol": kc_symbol},
            auth=True,
        )
    except Exception as exc:
        log.error(
            "[KUCOIN_MARGIN_MODE] BLOCK symbol=%s reason=verify_failed error=%s",
            symbol, exc,
        )
        return False

    verified_mode = str((verify or {}).get("marginMode", "") or "").upper()
    if verified_mode != CROSS:
        log.error(
            "[KUCOIN_MARGIN_MODE] BLOCK symbol=%s reason=verify_mismatch mode=%s",
            symbol, verified_mode or "UNKNOWN",
        )
        return False

    log.warning(
        "[KUCOIN_MARGIN_MODE] symbol=%s result=CROSS_CONFIRMED execution_effect=CONFIG_ONLY",
        symbol,
    )
    return True


def _protection_equivalent(expected: dict, observed: dict) -> bool:
    """Require exchange readback to match the protection-bearing entry intent."""
    if not isinstance(observed, dict) or not observed:
        return False
    fields = ("symbol", "side", "size", "triggerStopUpPrice", "triggerStopDownPrice")
    for field in fields:
        if field in expected:
            if str(observed.get(field, "")) != str(expected[field]):
                return False
    if bool(observed.get("reduceOnly", False)) is not bool(expected.get("reduceOnly", False)):
        return False
    return True


def install(KuCoinClient, kucoin_module, log) -> None:
    if getattr(KuCoinClient, "_native_tpsl_entry_installed", False):
        return

    original = KuCoinClient.place_order

    async def place_order_with_native_tpsl(
        self,
        symbol: str,
        side: str,
        qty: float,
        sl: float = 0,
        tp: float = 0,
        instruments: dict = None,
        reduce_only: bool = False,
        idem_key: str = None,
        single_submission: bool = False,
    ) -> dict:
        if (
            kucoin_module.PAPER_TRADE
            or reduce_only
            or not (float(sl or 0) > 0 or float(tp or 0) > 0)
        ):
            return await original(
                self,
                symbol,
                side,
                qty,
                sl=sl,
                tp=tp,
                instruments=instruments,
                reduce_only=reduce_only,
                idem_key=idem_key,
                single_submission=single_submission,
            )

        if not kucoin_module.API_KEY:
            log.warning("place_order TPSL: KUCOIN_API_KEY não configurado")
            return {}

        # INV-LIVE-READINESS-001: canonical new-entry readiness before any
        # external side effect (margin-mode switch included). The transport
        # boundary re-checks; this replaces the gate of the bypassed original.
        from bot.runtime_readiness import EntryReadinessRefused, assert_entry_dispatch_ready
        try:
            gate_oid = self.build_client_oid(symbol, side, qty, idem_key)
        except Exception:
            gate_oid = ""
        try:
            assert_entry_dispatch_ready(
                self, stage="native_tpsl", symbol=symbol, side=side, client_oid=gate_oid,
            )
        except EntryReadinessRefused:
            try:
                from bot.pilot_submission_counter import _fail_predispatch
                await _fail_predispatch(
                    self, symbol, side, qty, idem_key, "PRE_DISPATCH_READINESS_DENIED"
                )
            except Exception as exc:
                log.critical(
                    "[KUCOIN_NATIVE_TPSL] predispatch_abort_record_failed symbol=%s error=%s",
                    symbol, type(exc).__name__,
                )
            raise

        # Fix for production 330005: the body alone is insufficient when a
        # symbol is still selected as ISOLATED in the account configuration.
        if not await _ensure_cross_margin(self, kucoin_module, symbol, log):
            return {}

        try:
            contracts = self._round_qty(qty, symbol)
            client_oid = self.build_client_oid(
                symbol, side, qty, idem_key, contracts=contracts
            )
            trigger_up, trigger_down = _trigger_prices(side, sl, tp)
        except Exception as exc:
            log.error("[KUCOIN_NATIVE_TPSL] build failed symbol=%s error=%s", symbol, exc)
            return {}

        leverage = int(cfg.LEVERAGE)
        if leverage <= 0:
            log.error("[KUCOIN_NATIVE_TPSL] blocked symbol=%s reason=invalid_leverage", symbol)
            return {}

        body = {
            "clientOid": client_oid,
            "symbol": kucoin_module.to_kucoin(symbol),
            "side": str(side).lower(),
            "type": "market",
            "size": int(contracts),
            "leverage": leverage,
            "reduceOnly": False,
            "marginMode": CROSS,
            "positionSide": "BOTH",
            "stopPriceType": "TP",
        }
        if trigger_up > 0:
            body["triggerStopUpPrice"] = self._round_price(trigger_up, symbol)
        if trigger_down > 0:
            body["triggerStopDownPrice"] = self._round_price(trigger_down, symbol)

        if "triggerStopUpPrice" not in body and "triggerStopDownPrice" not in body:
            log.error("[KUCOIN_NATIVE_TPSL] blocked symbol=%s reason=no_valid_trigger", symbol)
            return {}

        post_options = {"single_attempt": True} if single_submission else {}
        data = await self._post(TPSL_ENDPOINT, body, **post_options)
        if not isinstance(data, dict):
            return {}

        order_id = data.get("orderId", "")
        if not order_id and data.get("_ambiguous"):
            for attempt in range(3):
                recovered = await self.get_order_by_client_oid(client_oid)
                if recovered:
                    data = recovered
                    order_id = recovered.get("orderId", "")
                    log.warning(
                        "♻️ [TPSL ORDER RECOVERED] clientOid=%s orderId=%s",
                        client_oid, order_id,
                    )
                    break
                if attempt < 2:
                    import asyncio
                    await asyncio.sleep(0.25 * (attempt + 1))

        if not order_id:
            log.error(
                "[KUCOIN_NATIVE_TPSL] submission unresolved symbol=%s clientOid=%s",
                symbol, client_oid,
            )
            return data

        data["clientOid"] = client_oid
        data["native_tpsl"] = True
        data["protection_endpoint"] = TPSL_ENDPOINT

        # A successful mutation response is not proof that protection exists.
        # Read back by the deterministic clientOid and require semantic
        # equivalence before declaring the entry protected.
        verified = False
        observed = {}
        for attempt in range(3):
            try:
                observed = await self.get_order_by_client_oid(client_oid) or {}
            except Exception:
                observed = {}
            if _protection_equivalent(body, observed):
                verified = True
                break
            if attempt < 2:
                import asyncio
                await asyncio.sleep(0.25 * (attempt + 1))
        data["protection_verified"] = verified
        if not verified:
            data["protection_not_verified"] = True
            self.entries_paused = True
            log.critical(
                "[KUCOIN_NATIVE_TPSL] PROTECTION_NOT_VERIFIED symbol=%s clientOid=%s "
                "execution_effect=BLOCK_NEW_ENTRIES",
                symbol, client_oid,
            )

        log.info(
            "📤 [ORDER+TPSL] clientOid=%s orderId=%s symbol=%s side=%s qty=%s "
            "leverage=%sx marginMode=CROSS SL=%s TP=%s endpoint=%s",
            client_oid, order_id, symbol, side, contracts, leverage,
            body.get("triggerStopDownPrice") if str(side).lower() == "buy" else body.get("triggerStopUpPrice"),
            body.get("triggerStopUpPrice") if str(side).lower() == "buy" else body.get("triggerStopDownPrice"),
            TPSL_ENDPOINT,
        )
        return data

    KuCoinClient.place_order = place_order_with_native_tpsl
    KuCoinClient._native_tpsl_entry_installed = True
    log.warning(
        "[KUCOIN_NATIVE_TPSL] installed: protected opening orders use "
        "POST /api/v1/st-orders with verified CROSS margin mode"
    )
