"""Use KuCoin's margin-mode-correct CROSS risk before LIVE sizing.

Public contract ``maintainMargin`` remains reference metadata only. Controlled
LIVE entries obtain account-specific CROSS MMR from KuCoin's private
``/api/v2/batchGetCrossOrderLimit`` endpoint after an exact NEXUS approval but
before RiskManagerV3 sizing.

Why the placement matters: with fixed high leverage, an ATR-derived stop can be
technically valid yet sit beyond the exchange liquidation boundary. The final
liquidation guard correctly rejects that order, but if the exact CROSS MMR is
known only after sizing there is no opportunity to make the strategy geometry
compatible with the configured leverage while preserving exact NEXUS approval.

For an initially NEXUS-approved setup this module therefore:
1. refreshes exact account-specific CROSS MMR;
2. keeps already-safe geometry unchanged and lets RiskManagerV3 size it;
3. otherwise refuses the trade (F-003: TECHNICAL_STOP_OUTSIDE_LIQUIDATION_SAFE_ZONE).
   The technical stop is never compressed and leverage is never auto-reduced.

The configured leverage is never lowered. The downstream liquidation guard,
RiskManagerV3, drawdown, ownership, exposure, durable execution and native
SL/TP protections remain authoritative. Any missing/invalid CROSS risk or
unsafe geometry fails closed.
"""
from __future__ import annotations

import contextvars
import math
import time


_ENGINE = contextvars.ContextVar("nexus_cross_mmr_engine", default=None)
_SIGNAL = contextvars.ContextVar("nexus_cross_mmr_signal", default=None)

_CROSS_MMR_CACHE_TTL_S = 15.0
_EXTRA_LIQ_HEADROOM_PCT = 0.10
_MIN_RETAINED_STOP_FRACTION = 0.40


def _normalize_mmr(value):
    """Return a finite fractional maintenance-margin rate or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        mmr = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(mmr) or not (0.0 < mmr < 1.0):
        return None
    return mmr


def _positive_finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _contract_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("dataList", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _select_cross_risk(data, kucoin_symbol):
    """Select the exact CROSS risk row; never accept another symbol's MMR."""
    for row in _contract_list(data):
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol", "")) != str(kucoin_symbol):
            continue
        mmr = _normalize_mmr(row.get("mmr"))
        if mmr is None:
            return None
        return {
            "mmr": mmr,
            "imr": _normalize_mmr(row.get("imr")),
            "totalMargin": _positive_finite(row.get("totalMargin")),
            "price": _positive_finite(row.get("price")),
            "leverage": _positive_finite(row.get("leverage")),
        }
    return None


def _hard_block_result(reason: str) -> dict:
    """Well-formed legacy-score result that cannot be advisory-bypassed."""
    return {
        "total": 0,
        "tecnico": 0,
        "orderflow": 0,
        "macro": 0,
        "news_mod": 0,
        "aprovado": False,
        "hard_block": reason,
        "detalhes": {"cross_margin_risk": reason},
    }


def _geometry_from_exact_mmr(liquidation, sig, leverage: int) -> dict:
    """Return SAFE, ADJUSTED or BLOCK using already-registered exact MMR."""
    try:
        entry = float(sig.entry)
        sl = float(sig.sl)
        tp = float(sig.tp)
        direction = str(sig.direction).upper()
    except (TypeError, ValueError, AttributeError):
        return {"status": "BLOCK", "reason": "invalid_signal_geometry"}

    if direction not in {"LONG", "SHORT"} or leverage <= 0:
        return {"status": "BLOCK", "reason": "invalid_signal_geometry"}
    if not all(math.isfinite(v) and v > 0 for v in (entry, sl, tp)):
        return {"status": "BLOCK", "reason": "invalid_signal_geometry"}
    if direction == "LONG" and not (sl < entry < tp):
        return {"status": "BLOCK", "reason": "invalid_long_geometry"}
    if direction == "SHORT" and not (tp < entry < sl):
        return {"status": "BLOCK", "reason": "invalid_short_geometry"}

    analysis = liquidation.analyze(
        entry=entry,
        stop=sl,
        leverage=int(leverage),
        is_long=(direction == "LONG"),
        symbol=str(sig.symbol),
        n_open_positions=1,
    )
    original_stop_pct = abs(entry - sl) / entry * 100.0
    raw_rr = abs(tp - entry) / abs(entry - sl)

    if analysis.stop_effective:
        return {
            "status": "SAFE",
            "reason": "already_liquidation_compatible",
            "sl": sl,
            "tp": tp,
            "rr": raw_rr,
            "original_stop_pct": original_stop_pct,
            "final_stop_pct": original_stop_pct,
            "safe_stop_pct": float(analysis.max_safe_stop_pct),
            "retained_fraction": 1.0,
            "liq_move_pct": float(analysis.liq_move_pct),
        }

    safe_stop_pct = max(
        0.0,
        float(analysis.max_safe_stop_pct) - _EXTRA_LIQ_HEADROOM_PCT,
    )
    if not math.isfinite(safe_stop_pct) or safe_stop_pct <= 0:
        return {
            "status": "BLOCK",
            "reason": "no_safe_stop_capacity",
            "original_stop_pct": original_stop_pct,
            "safe_stop_pct": safe_stop_pct,
        }

    retained = safe_stop_pct / original_stop_pct if original_stop_pct > 0 else 0.0
    if retained < _MIN_RETAINED_STOP_FRACTION:
        return {
            "status": "BLOCK",
            "reason": "required_compression_too_large",
            "original_stop_pct": original_stop_pct,
            "safe_stop_pct": safe_stop_pct,
            "retained_fraction": retained,
            "liq_move_pct": float(analysis.liq_move_pct),
        }

    new_risk = entry * safe_stop_pct / 100.0
    if direction == "LONG":
        new_sl = entry - new_risk
        new_tp = entry + new_risk * raw_rr
    else:
        new_sl = entry + new_risk
        new_tp = entry - new_risk * raw_rr

    if not all(math.isfinite(v) and v > 0 for v in (new_sl, new_tp)):
        return {"status": "BLOCK", "reason": "invalid_adjusted_geometry"}

    return {
        "status": "ADJUSTED",
        "reason": "compressed_for_configured_leverage",
        "sl": new_sl,
        "tp": new_tp,
        "rr": raw_rr,
        "original_stop_pct": original_stop_pct,
        "final_stop_pct": safe_stop_pct,
        "safe_stop_pct": safe_stop_pct,
        "retained_fraction": retained,
        "liq_move_pct": float(analysis.liq_move_pct),
    }


def install(KuCoinClient, TradingEngine, scoring, liquidation, log) -> None:
    if getattr(KuCoinClient, "_official_contract_mmr_installed", False):
        return

    original_load = KuCoinClient.load_instruments
    original_open = TradingEngine._open
    original_calculate = scoring.calculate
    original_nexus_validate = getattr(TradingEngine, "_nexus_validate", None)

    async def load_instruments_with_contract_risk_metadata(self):
        instruments = await original_load(self)
        self._cross_mmr_symbols = set()
        self._cross_mmr_cache = {}

        try:
            raw = await self._get("/api/v1/contracts/active")
            contracts = _contract_list(raw)
        except Exception as exc:
            log.warning(
                "[KUCOIN_CONTRACT_RISK] public_reference=UNAVAILABLE type=%s "
                "cross_mmr_ready=false execution_effect=BLOCK_NEW_LIVE_ENTRIES",
                type(exc).__name__,
            )
            return instruments

        by_symbol = {
            str(c.get("symbol", "")): c
            for c in contracts
            if isinstance(c, dict) and c.get("symbol")
        }
        reference_count = 0
        for standard_symbol, info in (instruments or {}).items():
            if not isinstance(info, dict):
                continue
            kucoin_symbol = str(info.get("kucoinSymbol", ""))
            contract = by_symbol.get(kucoin_symbol)
            if not contract:
                continue
            public_mmr = _normalize_mmr(contract.get("maintainMargin"))
            if public_mmr is not None:
                info["contractMaintainMarginReference"] = public_mmr
                reference_count += 1
            for source_key, dest_key in (
                ("initialMargin", "initialMarginReference"),
                ("minRiskLimit", "minRiskLimit"),
                ("maxRiskLimit", "maxRiskLimit"),
                ("riskStep", "riskStep"),
            ):
                value = _positive_finite(contract.get(source_key))
                if value is not None:
                    info[dest_key] = value

        log.warning(
            "[KUCOIN_CONTRACT_RISK] public_reference=%d/%d "
            "execution_authority=CROSS_PRIVATE_RISK_ENDPOINT "
            "endpoint=/api/v2/batchGetCrossOrderLimit leverage_unchanged=true",
            reference_count, len(instruments or {}),
        )
        return instruments

    async def _refresh_cross_risk(engine, symbol: str, stage: str):
        from bot.config import cfg

        info = (getattr(engine, "instruments", {}) or {}).get(symbol, {})
        kucoin_symbol = str(info.get("kucoinSymbol", "")) if isinstance(info, dict) else ""
        if not kucoin_symbol:
            raise ValueError("missing_kucoin_symbol")

        exchange_client = getattr(engine, "client", None)
        if exchange_client is None:
            raise ValueError("missing_exchange_client")

        account = await exchange_client._get(
            "/api/v1/account-overview", {"currency": "USDT"}, auth=True
        )
        total_margin = None
        if isinstance(account, dict):
            for key in ("marginBalance", "accountEquity", "equity"):
                total_margin = _positive_finite(account.get(key))
                if total_margin is not None:
                    break
        if total_margin is None:
            raise ValueError("invalid_total_margin")

        response = await exchange_client._get(
            "/api/v2/batchGetCrossOrderLimit",
            {
                "symbol": kucoin_symbol,
                "totalMargin": f"{total_margin:.8f}",
                "leverage": str(int(cfg.LEVERAGE)),
            },
            auth=True,
        )
        cross = _select_cross_risk(response, kucoin_symbol)
        if cross is None:
            raise ValueError("cross_risk_row_unavailable")

        returned_lev = cross.get("leverage")
        if returned_lev is not None and abs(returned_lev - float(cfg.LEVERAGE)) > 1e-9:
            raise ValueError("cross_risk_leverage_mismatch")

        liquidation.set_mmr_from_api(
            symbol, cross["mmr"], source="kucoin_cross_order_limit"
        )
        if not hasattr(exchange_client, "_cross_mmr_symbols"):
            exchange_client._cross_mmr_symbols = set()
        if not hasattr(exchange_client, "_cross_mmr_cache"):
            exchange_client._cross_mmr_cache = {}
        exchange_client._cross_mmr_symbols.add(symbol)
        exchange_client._cross_mmr_cache[symbol] = {
            "mmr": cross["mmr"],
            "leverage": float(cfg.LEVERAGE),
            "total_margin": total_margin,
            "ts": time.monotonic(),
        }
        log.warning(
            "[KUCOIN_CROSS_RISK] symbol=%s kucoin_symbol=%s result=PASS "
            "mmr=%.6f total_margin=%.4f leverage=%sx source=KuCoin_private "
            "stage=%s execution_effect=NONE",
            symbol, kucoin_symbol, cross["mmr"], total_margin,
            int(cfg.LEVERAGE), stage,
        )
        return cross

    def _cached_cross_risk(engine, symbol: str):
        from bot.config import cfg

        client = getattr(engine, "client", None)
        cache = getattr(client, "_cross_mmr_cache", {}) if client is not None else {}
        item = cache.get(symbol) if isinstance(cache, dict) else None
        if not isinstance(item, dict):
            return None
        try:
            age = time.monotonic() - float(item.get("ts", 0.0))
            leverage = float(item.get("leverage"))
            mmr = _normalize_mmr(item.get("mmr"))
        except (TypeError, ValueError):
            return None
        if age < 0 or age > _CROSS_MMR_CACHE_TTL_S:
            return None
        if abs(leverage - float(cfg.LEVERAGE)) > 1e-9 or mmr is None:
            return None
        registered = symbol in getattr(client, "_cross_mmr_symbols", set())
        if not registered:
            return None
        liquidation.set_mmr_from_api(
            symbol, mmr, source="kucoin_cross_order_limit_cached"
        )
        return {"mmr": mmr, "age": age}

    async def _open_with_cross_context(self, sig, *args, **kwargs):
        if getattr(self, "paper_trade", True) or not bool(
            getattr(getattr(self, "pilot", None), "enabled", False)
        ):
            return await original_open(self, sig, *args, **kwargs)

        token_engine = _ENGINE.set(self)
        token_signal = _SIGNAL.set(sig)
        try:
            return await original_open(self, sig, *args, **kwargs)
        finally:
            _SIGNAL.reset(token_signal)
            _ENGINE.reset(token_engine)

    async def _nexus_validate_with_exact_cross_geometry(self, sig, *args, **kwargs):
        initial = await original_nexus_validate(self, sig, *args, **kwargs)
        if getattr(self, "paper_trade", True) or not bool(
            getattr(getattr(self, "pilot", None), "enabled", False)
        ):
            return initial

        from bot.config import cfg
        from bot.nexus_types import NexusDecision, decision_validation_error

        validation_error = decision_validation_error(
            initial, sig.symbol, sig.direction, sig.entry, sig.sl, sig.tp
        )
        if validation_error is not None or initial.execution_allowed is not True:
            return initial

        if len(getattr(self, "positions", {}) or {}) >= 1:
            if bool(getattr(type(self), "_cross_portfolio_stress_capable", False)):
                setattr(sig, "_cross_multi_position_requires_stress", True)
                log.warning(
                    "[KUCOIN_CROSS_GEOMETRY] symbol=%s result=DEFER "
                    "reason=cross_multi_position_requires_final_qty_stress "
                    "stage=POST_NEXUS_PRE_SIZING final_authority=CROSS_PORTFOLIO_STRESS "
                    "execution_effect=NONE",
                    sig.symbol,
                )
                return initial
            log.warning(
                "[KUCOIN_CROSS_GEOMETRY] symbol=%s result=BLOCK "
                "reason=cross_multi_position_liquidation_unmodeled "
                "stage=POST_NEXUS_PRE_SIZING execution_effect=BLOCK_NEW_LIVE_ENTRY",
                sig.symbol,
            )
            return NexusDecision.wait(
                sig.symbol, "cross_multi_position_liquidation_unmodeled"
            )

        try:
            await _refresh_cross_risk(self, sig.symbol, "POST_NEXUS_PRE_SIZING")
        except Exception as exc:
            log.warning(
                "[KUCOIN_CROSS_RISK] symbol=%s result=BLOCK type=%s "
                "reason=fresh_cross_margin_mmr_unavailable "
                "stage=POST_NEXUS_PRE_SIZING execution_effect=BLOCK_NEW_LIVE_ENTRY",
                sig.symbol, type(exc).__name__,
            )
            return NexusDecision.wait(sig.symbol, "official_cross_mmr_unavailable")

        geometry = _geometry_from_exact_mmr(liquidation, sig, int(cfg.LEVERAGE))
        if geometry.get("status") == "BLOCK":
            log.warning(
                "[KUCOIN_CROSS_GEOMETRY] symbol=%s direction=%s result=BLOCK "
                "reason=%s leverage=%sx original_stop=%.3f%% safe_stop=%.3f%% "
                "retained=%.1f%% execution_effect=BLOCK_NEW_LIVE_ENTRY",
                sig.symbol, sig.direction, geometry.get("reason"),
                int(cfg.LEVERAGE), float(geometry.get("original_stop_pct", 0.0)),
                float(geometry.get("safe_stop_pct", 0.0)),
                float(geometry.get("retained_fraction", 0.0)) * 100.0,
            )
            return NexusDecision.wait(
                sig.symbol, str(geometry.get("reason", "unsafe_geometry"))
            )

        if geometry.get("status") == "SAFE":
            log.info(
                "[KUCOIN_CROSS_GEOMETRY] symbol=%s direction=%s result=SAFE "
                "leverage=%sx stop=%.3f%% liq=%.3f%% exact_mmr=true "
                "nexus_recheck=false execution_effect=NONE",
                sig.symbol, sig.direction, int(cfg.LEVERAGE),
                float(geometry.get("final_stop_pct", 0.0)),
                float(geometry.get("liq_move_pct", 0.0)),
            )
            return initial

        # F-003: the technical stop is never compressed to fit the liquidation
        # buffer at the configured leverage (and leverage is never lowered
        # automatically). A stop outside the safe zone is NO TRADE.
        from bot.risk_budget import TECHNICAL_STOP_OUTSIDE_LIQUIDATION_SAFE_ZONE
        log.warning(
            "[KUCOIN_CROSS_GEOMETRY] symbol=%s direction=%s result=BLOCK reason=%s "
            "leverage=%sx technical_stop=%.3f%% safe_stop=%.3f%% liq=%.3f%% "
            "stop_compression=DISABLED execution_effect=BLOCK_NEW_LIVE_ENTRY",
            sig.symbol, sig.direction, TECHNICAL_STOP_OUTSIDE_LIQUIDATION_SAFE_ZONE,
            int(cfg.LEVERAGE), float(geometry.get("original_stop_pct", 0.0)),
            float(geometry.get("safe_stop_pct", 0.0)),
            float(geometry.get("liq_move_pct", 0.0)),
        )
        return NexusDecision.wait(sig.symbol, TECHNICAL_STOP_OUTSIDE_LIQUIDATION_SAFE_ZONE)

    async def _calculate_with_fresh_cross_mmr(
        symbol, direction, closes, highs, lows, volumes, client=None
    ):
        engine = _ENGINE.get()
        sig = _SIGNAL.get()

        if engine is None or sig is None:
            return await original_calculate(
                symbol, direction, closes, highs, lows, volumes, client
            )
        if str(getattr(sig, "symbol", "")) != str(symbol):
            return _hard_block_result("cross_mmr_signal_symbol_mismatch")
        if str(getattr(sig, "direction", "")).upper() != str(direction).upper():
            return _hard_block_result("cross_mmr_signal_side_mismatch")

        cached = _cached_cross_risk(engine, symbol)
        if cached is not None:
            log.info(
                "[KUCOIN_CROSS_RISK] symbol=%s result=PASS source=pre_sizing_cache "
                "mmr=%.6f age=%.2fs stage=POST_NEXUS_PRETRADE "
                "execution_effect=NONE",
                symbol, cached["mmr"], cached["age"],
            )
        else:
            try:
                await _refresh_cross_risk(engine, symbol, "POST_NEXUS_PRETRADE")
            except Exception as exc:
                log.warning(
                    "[KUCOIN_CROSS_RISK] symbol=%s result=BLOCK type=%s "
                    "reason=fresh_cross_margin_mmr_unavailable "
                    "stage=POST_NEXUS_PRETRADE execution_effect=BLOCK_NEW_LIVE_ENTRY",
                    symbol, type(exc).__name__,
                )
                return _hard_block_result("official_cross_mmr_unavailable")

        exchange_client = getattr(engine, "client", None)
        _, official = liquidation.get_mmr(symbol)
        registered = symbol in getattr(exchange_client, "_cross_mmr_symbols", set())
        if not official or not registered:
            log.warning(
                "[KUCOIN_CROSS_RISK] symbol=%s result=BLOCK "
                "reason=cross_mmr_registration_failed stage=POST_NEXUS_PRETRADE "
                "execution_effect=BLOCK_NEW_LIVE_ENTRY",
                symbol,
            )
            return _hard_block_result("official_cross_mmr_unavailable")

        return await original_calculate(
            symbol, direction, closes, highs, lows, volumes, client
        )

    KuCoinClient.load_instruments = load_instruments_with_contract_risk_metadata
    TradingEngine._open = _open_with_cross_context
    if original_nexus_validate is not None:
        TradingEngine._nexus_validate = _nexus_validate_with_exact_cross_geometry
    scoring.calculate = _calculate_with_fresh_cross_mmr
    KuCoinClient._official_contract_mmr_installed = True
    log.warning(
        "[KUCOIN_CONTRACT_RISK] installed: public maintainMargin reference-only; "
        "fresh account-specific CROSS mmr is required after exact NEXUS approval "
        "and before LIVE sizing; unsafe 50x geometry may be compressed with R:R "
        "preserved then must pass a second exact NEXUS review; CROSS cache ttl=%.0fs; "
        "leverage/score thresholds/execution protections unchanged",
        _CROSS_MMR_CACHE_TTL_S,
    )
