"""Fail-closed symbol viability gate for NEXUS-7.

This hardening replaces the legacy core fallback that marked every configured
symbol viable after an exception. A symbol is now usable only when instrument
metadata, a positive market price, and minimum-lot affordability are proven.
No exchange mutation is performed here.
"""
from __future__ import annotations


async def _filter_viable_symbols_fail_closed(self) -> bool:
    from bot.config import cfg
    from bot.logger import log

    try:
        if not self.instruments:
            log.warning(
                "⛔ INSTRUMENTS_UNAVAILABLE: self.instruments vazio — "
                "viability fails closed"
            )
            self.viable_symbols = []
            return False

        tickers = await self.client.get_all_tickers()
        if not tickers:
            log.warning(
                "⛔ TICKERS_UNAVAILABLE: get_all_tickers() vazio — "
                "usando somente preços comprovados no cache WS"
            )
            tickers = []

        price_map = {}
        for ticker in tickers:
            if not isinstance(ticker, dict):
                continue
            symbol = ticker.get("symbol")
            try:
                price = float(ticker.get("lastPrice", 0) or 0)
            except (TypeError, ValueError):
                continue
            if symbol and price > 0:
                price_map[symbol] = price

        # Observability only. This override replaces the legacy method that
        # hosted the minimum-order feasibility matrix, which therefore never
        # ran in production. It never changes viable_symbols, risk or sizing.
        try:
            from bot import min_order_feasibility_matrix as _feasibility_matrix
            _feasibility_matrix.log_once(self, dict(price_map), log)
        except Exception as matrix_exc:
            log.warning(
                "[MIN_ORDER_FEASIBILITY_MATRIX] result=DEFER reason=%s "
                "observability_only=true decision_effect=NONE execution_effect=NONE",
                type(matrix_exc).__name__,
            )

        balance = float(getattr(getattr(self, "risk", None), "balance", 0) or 0)
        buying_power = balance * float(cfg.LEVERAGE)
        if buying_power <= 0:
            log.warning(
                "⛔ INSUFFICIENT_BUYING_POWER: balance/leverage não comprovam "
                "capacidade de abrir lote mínimo"
            )
            self.viable_symbols = []
            return False

        viable = []
        rejected = []
        for sym in cfg.SYMBOLS:
            info = self.instruments.get(sym)
            if not isinstance(info, dict) or not info:
                rejected.append(f"{sym}(sem instrumento)")
                continue

            price = price_map.get(sym, 0.0)
            if price <= 0:
                try:
                    cached = self.client.get_cached_ticker(sym) or {}
                    price = float(cached.get("lastPrice", 0) or 0)
                except (TypeError, ValueError, AttributeError):
                    price = 0.0
            if price <= 0:
                rejected.append(f"{sym}(sem preço)")
                continue

            try:
                lot_size = float(info.get("minQty", 0) or 0)
                multiplier = float(info.get("multiplier", 0) or 0)
            except (TypeError, ValueError):
                rejected.append(f"{sym}(metadata inválida)")
                continue
            if lot_size <= 0 or multiplier <= 0:
                rejected.append(f"{sym}(metadata inválida)")
                continue

            min_cost = lot_size * multiplier * price
            if buying_power >= min_cost * 1.1:
                viable.append(sym)
            else:
                rejected.append(f"{sym}(min ${min_cost:.2f})")

        self.viable_symbols = viable
        if viable:
            log.info(
                "✅ %s/%s pares viáveis (fail-closed): %s",
                len(viable), len(cfg.SYMBOLS), ", ".join(viable),
            )
        else:
            log.error(
                "⛔ ZERO_VIABLE_SYMBOLS: nenhuma viabilidade comprovada; "
                "novas entradas bloqueadas"
            )
        if rejected:
            log.info("⛔ Rejeitados: %s", ", ".join(rejected))
        return bool(viable)
    except Exception as exc:
        self.viable_symbols = []
        log.error(
            "[VIABILITY_FAIL_CLOSED] result=BLOCK error=%s detail=%s",
            type(exc).__name__, str(exc)[:180],
        )
        return False


def install(TradingEngine, log) -> None:
    if getattr(TradingEngine, "_viability_fail_closed_installed", False):
        return
    TradingEngine._filter_viable_symbols = _filter_viable_symbols_fail_closed
    TradingEngine._viability_fail_closed_installed = True
    log.warning(
        "[VIABILITY_FAIL_CLOSED] installed: exceptions/invalid metadata/unknown "
        "prices can never authorize symbols"
    )
