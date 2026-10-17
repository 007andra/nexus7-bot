"""Minimal fail-closed startup entrypoint for BGX runtime hardenings.

Installation ordering lives in ``bot.runtime_bootstrap`` so the runtime stack is
explicit and testable while hardenings are progressively migrated into core.

``main_hardened.py`` is also a real production entrypoint and imports
``bot.service_readiness`` for Railway's infrastructure readiness endpoint. This
reference is documented here because the self-check scans root startup
entrypoints, including this module, when determining whether a bot module is
actually orphaned. Keeping the production entrypoint dependency visible here
prevents a false dead-code warning without importing or executing readiness
logic during interpreter startup.
"""
import builtins
import sys

builtins._nexus_sitecustomize_status = "installing"

try:
    # Runtime-truth only: websockets==12.0 protocols are async iterables but
    # don't expose __anext__ directly. The truth WS proxy delegates __anext__
    # to the wrapped protocol, so install the compatibility shim before the
    # runtime bootstrap creates any telemetry-enabled transport wrappers.
    # The shim is a strict no-op unless BGX_RUNTIME_TRUTH_ENABLED=true.
    from bot.runtime_truth_ws_compat import install as _install_runtime_truth_ws_compat
    _install_runtime_truth_ws_compat()

    # Preserve every replay-relevant kline/control frame while excluding the
    # high-frequency ticker stream from durable truth telemetry. This wraps
    # only the telemetry capture function; the KuCoin WS frame delivered to
    # production parsing/cache code is unchanged. It is also a no-op while
    # BGX_RUNTIME_TRUTH_ENABLED=false.
    from bot.runtime_truth_ws_filter import install as _install_runtime_truth_ws_filter
    _install_runtime_truth_ws_filter()

    from bot.runtime_bootstrap import install as _install_runtime_bootstrap

    _install_runtime_bootstrap()
    builtins._nexus_sitecustomize_status = "ok"
except Exception as _sitecustomize_error:
    builtins._nexus_sitecustomize_status = "failed"
    print(
        "CRITICAL: NEXUS sitecustomize hardening installation failed: "
        f"{type(_sitecustomize_error).__name__}: {_sitecustomize_error}",
        file=sys.stderr,
    )
