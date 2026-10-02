from types import SimpleNamespace

from bot.nexus_types import ModelOutput
import bot.market_language_runtime as runtime


class _Log:
    def warning(self, *args, **kwargs):
        return None


def _base_run(closes, highs, lows, volumes, funding=None, oi_delta=None, ls_ratio=None):
    return [ModelOutput(name="BASE")]


def test_market_language_runtime_appends_model_h_and_is_idempotent():
    nexus_ai = SimpleNamespace(run_ensemble=_base_run)
    nexus_models = SimpleNamespace(run_ensemble=_base_run)
    original_model = runtime.model_market_language
    runtime.model_market_language = lambda *a, **k: ModelOutput(
        name="MARKET_LANGUAGE", available=False
    )
    try:
        runtime.install(nexus_ai, nexus_models, _Log())
        runtime.install(nexus_ai, nexus_models, _Log())
        result = nexus_ai.run_ensemble(
            [1.0] * 64, [1.1] * 64, [0.9] * 64, [100.0] * 64
        )
    finally:
        runtime.model_market_language = original_model

    assert [m.name for m in result] == ["BASE", "MARKET_LANGUAGE"]
    assert nexus_ai.run_ensemble is nexus_models.run_ensemble
    assert nexus_ai._market_language_runtime_installed is True
