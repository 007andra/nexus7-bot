"""Startup position loader with explicit KuCoin quantity-unit semantics.

KuCoinPositionUnitAdapter normalizes ``get_positions()[].size`` to BASE_ASSET
and preserves native contracts in ``sizeContracts``. The legacy engine startup
loader predates that adapter and unconditionally multiplied ``size`` by the
contract multiplier a second time. A recovered 30 AVAX position therefore
became 3 AVAX locally and was correctly demoted by the post-load ownership
proof as divergent.

The legacy startup fallback stop geometry also assumed that a liquidation-price
buffer would always remain on the protective side of entry. At high leverage a
nearby liquidation price can violate that assumption (for example a SHORT can
produce ``liq * 0.98 <= entry``), causing Signal validation to abort the entire
startup load. The fallback below uses liquidation geometry only when it remains
strictly protective; otherwise it falls back to the ATR-derived level.

This hardening replaces only the startup reconstruction method. It performs no
exchange mutation and keeps unknown units fail-closed.
"""
from __future__ import annotations

import math


def _startup_levels(entry: float, liquidation: float, direction: str) -> tuple[float, float]:
    """Return valid local fallback SL/TP geometry without mutating exchange state."""
    atr_est = entry * 0.007
    if direction == "LONG":
        fallback_sl = entry - atr_est * 1.5
        liquidation_sl = liquidation * 1.02 if liquidation > 0 else 0.0
        # A LONG stop must remain strictly below entry. A very near liquidation
        # can make the 2% buffer cross entry, in which case it is unusable.
        if 0 < liquidation_sl < entry:
            sl = max(liquidation_sl, fallback_sl)
        else:
            sl = fallback_sl
        tp = entry + atr_est * 3.0
    else:
        fallback_sl = entry + atr_est * 1.5
        liquidation_sl = liquidation * 0.98 if liquidation > 0 else 0.0
        # A SHORT stop must remain strictly above entry. At 50x, liquidation can
        # be close enough that a 2% inward buffer lands below entry.
        if liquidation_sl > entry:
            sl = min(liquidation_sl, fallback_sl)
        else:
            sl = fallback_sl
        tp = entry - atr_est * 3.0

    if direction == "LONG" and not (sl < entry < tp):
        raise ValueError(f"invalid LONG startup geometry: sl={sl} entry={entry} tp={tp}")
    if direction == "SHORT" and not (tp < entry < sl):
        raise ValueError(f"invalid SHORT startup geometry: tp={tp} entry={entry} sl={sl}")
    return sl, tp


def install(TradingEngine, log) -> None:
    if getattr(TradingEngine, "_startup_position_unit_hardening_patched", False):
        return

    from bot.engine import Position
    from bot.strategy import Signal

    async def _load_existing_positions_unit_safe(self):
        try:
            all_pos = await self.client.get_positions()
            count = 0
            for p in all_pos or []:
                if not isinstance(p, dict):
                    continue
                try:
                    size = float(p.get("size", 0) or 0)
                except (TypeError, ValueError):
                    log.critical(
                        "[STARTUP_POSITION_UNIT] result=REJECTED reason=invalid_size"
                    )
                    continue
                if not math.isfinite(size) or size <= 0:
                    continue

                sym = str(p.get("symbol", "") or "")
                if not sym:
                    log.critical(
                        "[STARTUP_POSITION_UNIT] result=REJECTED reason=missing_symbol"
                    )
                    continue

                side = p.get("side", "Buy")
                try:
                    ep = float(p.get("entryPrice", p.get("avgPrice", 0)) or 0)
                    upnl = float(p.get("unrealisedPnl", 0) or 0)
                    liq = float(p.get("liquidationPrice", p.get("liqPrice", 0)) or 0)
                except (TypeError, ValueError):
                    log.critical(
                        "[STARTUP_POSITION_UNIT] symbol=%s result=REJECTED "
                        "reason=invalid_position_prices",
                        sym,
                    )
                    continue

                if not math.isfinite(ep) or ep <= 0:
                    log.error(
                        "⚠️ %s: entryPrice inválido (%s) no retorno da exchange — "
                        "posição NÃO carregada para evitar SL/TP zerados. Campos "
                        "recebidos: %s",
                        sym, ep, list(p.keys()),
                    )
                    continue

                direction = "LONG" if side == "Buy" else "SHORT"
                try:
                    sl, tp = _startup_levels(ep, liq, direction)
                except ValueError as exc:
                    log.critical(
                        "[STARTUP_POSITION_UNIT] symbol=%s result=REJECTED "
                        "reason=invalid_startup_geometry error=%s",
                        sym, str(exc),
                    )
                    continue

                unit = str(p.get("sizeUnit", "") or "").strip().upper()
                try:
                    if unit == "BASE_ASSET":
                        base_size = size
                        source = "explicit_base_asset"
                    elif unit in {"", "CONTRACT", "CONTRACTS"}:
                        # Backward compatibility for raw/legacy KuCoin rows that
                        # predate the adapter and therefore carry contract size.
                        base_size = float(self._contracts_to_base_qty(sym, size))
                        source = "legacy_contract_conversion"
                    else:
                        raise ValueError(f"unsupported sizeUnit={unit!r}")
                except (TypeError, ValueError) as exc:
                    log.critical(
                        "[STARTUP_POSITION_UNIT] symbol=%s result=REJECTED "
                        "reason=unit_conversion_failed size=%s sizeUnit=%s error=%s",
                        sym, size, unit or "UNSPECIFIED", str(exc),
                    )
                    continue

                if not math.isfinite(base_size) or base_size <= 0:
                    log.critical(
                        "[STARTUP_POSITION_UNIT] symbol=%s result=REJECTED "
                        "reason=invalid_base_quantity base_qty=%s",
                        sym, base_size,
                    )
                    continue

                sig = Signal(sym, direction, ep, sl, tp, 0.75, "Startup sync", 75)
                pos = Position(sig, base_size)
                pos.initial_sl = None  # Q-01: rebuilt from exchange; initial risk unknown
                pos.pnl = upnl
                try:
                    cur = float(p.get("markPrice", ep) or ep)
                except (TypeError, ValueError):
                    cur = ep
                pos.update_pnl(cur)
                self.positions[sym] = pos
                count += 1
                log.info(
                    "[STARTUP_POSITION_UNIT] symbol=%s result=LOADED "
                    "raw_size=%s sizeUnit=%s base_qty=%s source=%s "
                    "local_sl=%s local_tp=%s",
                    sym, size, unit or "UNSPECIFIED", base_size, source, sl, tp,
                )

            if count:
                log.info("✅ %s posição(ões) sincronizadas da exchange", count)
        except Exception as exc:
            log.error("_load_existing: %s", exc)

    TradingEngine._load_existing_positions = _load_existing_positions_unit_safe
    TradingEngine._startup_position_unit_hardening_patched = True
    log.warning(
        "[STARTUP_POSITION_UNIT] installed=true explicit_base_asset_passthrough=true "
        "legacy_contract_conversion=true valid_stop_geometry=true exchange_mutation=false"
    )
