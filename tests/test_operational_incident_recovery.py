import asyncio
from types import SimpleNamespace

from bot import operational_incident_recovery as recovery


class DummyLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


class FakeStore:
    def __init__(self, peak):
        self.values = {
            recovery.drawdown.DURABLE_EQUITY_PEAK_KEY: str(peak),
        }

    async def load(self, key, strict=False):
        return self.values.get(key)

    async def save(self, key, value, strict=False):
        self.values[key] = value
        return True



def test_legacy_hwm_rebase_is_not_installed_on_future_live_release():
    """A release gate must never implicitly enable the old incident HWM reset."""
    import ast
    import inspect

    from bot import runtime_bootstrap

    tree = ast.parse(inspect.getsource(runtime_bootstrap.install))
    installs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "install"
        and isinstance(node.func.value, ast.Name)
        and "operational_incident_recovery" in node.func.value.id
    ]
    assert installs == [], "historical HWM incident repair cannot auto-install"


def test_incident_target_preserves_clean_drawdown():
    equity = 18.5482
    peak = recovery._target_peak(equity)
    dd = (peak - equity) / peak
    assert abs(dd - recovery.PRESERVED_STRATEGY_DRAWDOWN) < 1e-12
    assert peak > equity


def test_incident_rebase_is_one_time_and_preserves_9_10_pct():
    store = FakeStore(29.1924)
    risk = SimpleNamespace(peak_balance=29.1924, drawdown=0.3646)
    engine = SimpleNamespace(paper_trade=False, risk=risk)

    old_load = recovery.db.load_key_value
    old_save = recovery.db.save_key_value
    old_peak_load = recovery.drawdown._load_peak

    async def peak_load(_risk, strict=True):
        raw = store.values[recovery.drawdown.DURABLE_EQUITY_PEAK_KEY]
        return float(raw), "test"

    recovery.db.load_key_value = store.load
    recovery.db.save_key_value = store.save
    recovery.drawdown._load_peak = peak_load
    try:
        applied = asyncio.run(
            recovery.maybe_rebase(
                engine,
                18.5482,
                DummyLog(),
                preflight_clear=True,
            )
        )
        assert applied is True
        assert abs(risk.drawdown - recovery.PRESERVED_STRATEGY_DRAWDOWN) < 1e-12
        assert recovery.MARKER_KEY in store.values

        applied_again = asyncio.run(
            recovery.maybe_rebase(
                engine,
                18.5482,
                DummyLog(),
                preflight_clear=True,
            )
        )
        assert applied_again is False
    finally:
        recovery.db.load_key_value = old_load
        recovery.db.save_key_value = old_save
        recovery.drawdown._load_peak = old_peak_load


def test_incident_rebase_refuses_non_contaminated_drawdown():
    store = FakeStore(20.0)
    risk = SimpleNamespace(peak_balance=20.0, drawdown=0.075)
    engine = SimpleNamespace(paper_trade=False, risk=risk)

    old_load = recovery.db.load_key_value
    old_save = recovery.db.save_key_value
    old_peak_load = recovery.drawdown._load_peak

    async def peak_load(_risk, strict=True):
        return 20.0, "test"

    recovery.db.load_key_value = store.load
    recovery.db.save_key_value = store.save
    recovery.drawdown._load_peak = peak_load
    try:
        applied = asyncio.run(
            recovery.maybe_rebase(
                engine,
                18.5,
                DummyLog(),
                preflight_clear=True,
            )
        )
        assert applied is False
        assert recovery.MARKER_KEY not in store.values
        assert risk.peak_balance == 20.0
    finally:
        recovery.db.load_key_value = old_load
        recovery.db.save_key_value = old_save
        recovery.drawdown._load_peak = old_peak_load
