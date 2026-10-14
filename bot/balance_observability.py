"""Disambiguate exchange balance from PAPER and SHADOW read-only modes.

Observability-only hardening. In PAPER or validation-held SHADOW mode the
exchange available balance may be queried for connectivity/account
visibility, but it must never be presented as an actively executable LIVE
balance.
"""
from __future__ import annotations


def exchange_metadata(balance, paper_trade: bool, shadow_readonly: bool = False) -> dict:
    read_only = bool(paper_trade or shadow_readonly)
    mode = "PAPER_READ_ONLY" if paper_trade else (
        "SHADOW_READ_ONLY" if shadow_readonly else "LIVE"
    )
    return {
        "available_usdt": (
            round(float(balance), 4) if balance is not None else None
        ),
        "source": _exchange_source(),
        "read_only": read_only,
        "mode": mode,
        "used_for_paper_sizing": False,
    }


def _exchange_source() -> str:
    from bot import exchange

    return f"{exchange.EXCHANGE_NAME.capitalize()} {exchange.EXCHANGE_PRODUCT}"


def paper_metadata(balance) -> dict:
    return {
        "balance": round(float(balance or 0.0), 4),
        "source": "isolated_paper_wallet",
        "used_for_paper_sizing": True,
    }


def install(log) -> None:
    from bot.engine import TradingEngine
    from bot.exchange import PAPER_TRADE

    original_status = getattr(TradingEngine, "get_status", None)
    if original_status is not None and not getattr(
        TradingEngine, "_balance_observability_status_patched", False
    ):
        def status_with_balance_sources(self, *args, **kwargs):
            out = original_status(self, *args, **kwargs)
            if not isinstance(out, dict):
                return out
            result = dict(out)
            paper_trade = bool(getattr(self, "paper_trade", PAPER_TRADE))
            shadow_readonly = bool(
                getattr(self.client, "_shadow_readonly_active", False)
                or getattr(self, "_validation_safety_lock_active", False)
            )
            exchange_balance = getattr(self.client, "_last_exchange_balance", None)
            result["exchange_balance"] = exchange_metadata(
                exchange_balance,
                paper_trade,
                shadow_readonly,
            )
            if paper_trade:
                result["paper_balance_source"] = paper_metadata(
                    getattr(
                        self,
                        "_paper_balance",
                        getattr(self.risk, "balance", 0.0),
                    )
                )
            return result

        TradingEngine.get_status = status_with_balance_sources
        TradingEngine._balance_observability_status_patched = True

    log.info(
        "[BALANCE_OBSERVABILITY] installed: exchange/PAPER/SHADOW balance modes "
        "are explicit; sizing/risk behavior unchanged"
    )
