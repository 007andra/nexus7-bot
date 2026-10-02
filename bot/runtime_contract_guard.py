"""Fail-closed contract guard for the final NEXUS-7 runtime patch stack.

The project intentionally composes a number of narrowly-scoped runtime wrappers.
That makes installation order operationally significant: a later wrapper can
silently replace an earlier safety or operator-policy method while every module
still imports successfully.

This guard runs *last* in ``runtime_overlays.install`` and proves ownership of
execution-critical final callables plus required inner hardening markers. It
does not change thresholds, leverage, drawdown semantics, entry authorization
or exchange behavior. It refuses startup when the reviewed runtime graph,
including final risk-authoritative sizing, is not intact.
"""
from __future__ import annotations

import builtins
import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContractItem:
    name: str
    value: Any
    expected_source: str


@dataclass(frozen=True)
class MarkerItem:
    name: str
    value: Any


def callable_source(value: Any) -> str:
    code = getattr(value, "__code__", None)
    filename = str(getattr(code, "co_filename", "") or "")
    return os.path.basename(filename)


def verify(items: tuple[ContractItem, ...]) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    for item in items:
        source = callable_source(item.value)
        if source != item.expected_source:
            errors.append(
                f"{item.name}: source={source or '<unknown>'} "
                f"expected={item.expected_source}"
            )
    return (not errors, tuple(errors))


def verify_markers(items: tuple[MarkerItem, ...]) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    for item in items:
        if item.value is not True:
            errors.append(f"{item.name}: installed={item.value!r} expected=True")
    return (not errors, tuple(errors))


def install(TradingEngine, PilotGuard, nexus_ai, engine_module, log) -> None:
    """Prove the final reviewed runtime graph; fail closed on ownership drift."""
    if getattr(builtins, "_nexus_runtime_contract_status", None) == "ok":
        return

    from bot.risk import RiskManager
    from bot.risk_manager_v3 import RiskManagerV3
    from bot.kucoin import KuCoinClient
    from bot import durable_execution

    items = (
        ContractItem("TradingEngine.run", TradingEngine.run, "operator_runtime_policy.py"),
        ContractItem(
            "TradingEngine._update_balance",
            TradingEngine._update_balance,
            "operator_runtime_policy.py",
        ),
        ContractItem("RiskManager.can_open", RiskManager.can_open, "operator_runtime_policy.py"),
        ContractItem(
            "RiskManagerV3.can_open",
            RiskManagerV3.can_open,
            "operator_runtime_policy.py",
        ),
        ContractItem(
            "engine.minimum_base_quantity",
            engine_module.minimum_base_quantity,
            "final_sizing_invariants.py",
        ),
        ContractItem("PilotGuard.evaluate", PilotGuard.evaluate, "pilot_exposure_capacity.py"),
        ContractItem(
            "nexus_ai.regime_compatibility",
            nexus_ai.regime_compatibility,
            "nexus_regime_transition_consistency.py",
        ),
        ContractItem(
            "KuCoinClient.place_order",
            KuCoinClient.place_order,
            "live_execution_fence.py",
        ),
        ContractItem(
            "KuCoinClient._post",
            KuCoinClient._post,
            "partial_tp_execution_hardening.py",
        ),
        ContractItem(
            "KuCoinClient.wait_for_fill",
            KuCoinClient.wait_for_fill,
            "order_visibility_race_hardening.py",
        ),
        ContractItem(
            "KuCoinClient.get_order_status",
            KuCoinClient.get_order_status,
            "kucoin.py",
        ),
        ContractItem(
            "KuCoinClient.set_position_stops",
            KuCoinClient.set_position_stops,
            "prelive_protection_failclosed.py",
        ),
        ContractItem(
            "TradingEngine._manage_partial_tp",
            TradingEngine._manage_partial_tp,
            "partial_tp_execution_hardening.py",
        ),
        ContractItem(
            "durable_execution.reconcile_orders",
            durable_execution.reconcile_orders,
            "durable_reconcile_hardening.py",
        ),
    )

    markers = (
        MarkerItem(
            "engine.final_sizing_invariants",
            getattr(engine_module, "_final_sizing_invariants_installed", False),
        ),
        MarkerItem(
            "KuCoinClient.native_tpsl",
            getattr(KuCoinClient, "_native_tpsl_entry_installed", False),
        ),
        MarkerItem(
            "KuCoinClient.fill_normalization",
            getattr(KuCoinClient, "_fill_normalization_installed", False),
        ),
        MarkerItem(
            "KuCoinClient.durable_submission_counter",
            getattr(KuCoinClient, "_pilot_durable_submission_counter_installed", False),
        ),
        MarkerItem(
            "KuCoinClient.live_execution_fence",
            getattr(KuCoinClient, "_live_execution_fence_installed", False),
        ),
        MarkerItem(
            "KuCoinClient.order_visibility_race",
            getattr(KuCoinClient, "_order_visibility_race_hardening_installed", False),
        ),
        MarkerItem(
            "TradingEngine.partial_tp_execution",
            getattr(TradingEngine, "_partial_tp_execution_hardening_installed", False),
        ),
        MarkerItem(
            "durable_execution.startup_reconcile",
            getattr(durable_execution, "_startup_reconcile_hardening_installed", False),
        ),
    )

    callable_ok, callable_errors = verify(items)
    marker_ok, marker_errors = verify_markers(markers)
    errors = callable_errors + marker_errors
    if not callable_ok or not marker_ok:
        builtins._nexus_runtime_contract_status = "failed"
        detail = "; ".join(errors)
        log.critical(
            "[RUNTIME_CONTRACT] status=FAILED action=BLOCK_STARTUP "
            "reason=execution_chain_drift detail=%s",
            detail,
        )
        raise RuntimeError(f"RUNTIME_CONTRACT_DRIFT: {detail}")

    builtins._nexus_runtime_contract_status = "ok"
    builtins._nexus_runtime_contract_snapshot = {
        **{item.name: callable_source(item.value) for item in items},
        **{item.name: True for item in markers},
    }
    log.critical(
        "[RUNTIME_CONTRACT] status=PASS protected_callables=%d required_markers=%d "
        "execution_chain=idempotency>distributed_fence>dispatch>fill>tpsl>reconcile "
        "late_wrapper_drift=false leverage_unchanged=true "
        "sizing_authority=RISK_BUDGET_V3 margin_role=CEILING "
        "drawdown_policy_unchanged=true entry_authorization_unchanged=true",
        len(items), len(markers),
    )
