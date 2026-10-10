from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_sitecustomize_is_minimal_delegate():
    text = (ROOT / "sitecustomize.py").read_text(encoding="utf-8")
    assert "bot.runtime_bootstrap" in text
    assert "_install_runtime_bootstrap()" in text
    assert "TradingEngine._nexus_validate" not in text
    assert "Analyzer.analyze_mtf" not in text
    assert "_np._execute" not in text


def test_runtime_bootstrap_preserves_safety_installers():
    text = (ROOT / "bot" / "runtime_bootstrap.py").read_text(encoding="utf-8")
    for marker in (
        "_validation_safety_lock.install(_log)",
        "_prelive_protection_failclosed.install(TradingEngine, _kucoin, _log)",
        "_binance_protection_failclosed.install(TradingEngine, _log)",
        "_pilot_external_position_guard.install(",
        'exchange_name="kucoin"',
        'exchange_name="binance"',
        "_instrument_readiness_guard.install(_log)",
        "_news_context_hardening.install(_log)",
        "_stagnation_time_hardening.install(TradingEngine, _log)",
    ):
        assert marker in text


def test_bootstrap_has_no_direct_exchange_mutation_or_release_enablement():
    text = (ROOT / "bot" / "runtime_bootstrap.py").read_text(encoding="utf-8")
    for marker in (
        "PILOT_RELEASE_APPROVED=",
        "LIVE_TRADING_CONFIRMED=",
        "PAPER_TRADE=false",
        "place_order(",
        "cancel_order(",
        "set_leverage(",
    ):
        assert marker not in text
