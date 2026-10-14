"""Single reproducible pre-release proof-pack entry point."""
import subprocess
import sys

MODULES = (
    "tests.test_final_evidence_closure",
    "tests.test_release_pilot_postgres",
    "tests.test_execution_ownership",
    "tests.test_critical_state",
    "tests.test_financial_state",
    "tests.test_runtime_readiness",
    "tests.test_durable_execution_restart",
    "tests.test_durable_reconcile_hardening",
    "tests.test_restart_opening_order_lineage",
    "tests.test_protection_readiness_authority",  # BGX-MISSED-001: includes explicit XRP positive/negative/race proofs
    "tests.test_release_source_regression",
    "tests.test_regression",
    # Production venue (Binance USD-M) LIVE path; replaces the KuCoin adapter
    # proofs removed with the adapter.
    "tests.test_binance_live_ai_gate",
    "tests.test_binance_live_pilot_limits",
    "tests.test_binance_live_dispatch_safety",
    "tests.test_binance_paper_mutations",
    "tests.test_binance_restart_ownership_continuity",
)

def main() -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "tests.run_offline", *MODULES],
        check=False,
    )
    if completed.returncode != 0:
        print("RELEASE_PROOF=FAIL", file=sys.stderr)
        return completed.returncode or 1
    print("RELEASE_PROOF=PASS")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
