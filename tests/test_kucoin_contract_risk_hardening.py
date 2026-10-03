import asyncio
from dataclasses import dataclass

from bot import kucoin_contract_risk_hardening as hardening
from bot import liquidation
from bot.config import cfg
from bot.kucoin_contract_risk_hardening import (
    _contract_list,
    _geometry_from_exact_mmr,
    _normalize_mmr,
    _positive_finite,
    _select_cross_risk,
)
from bot.nexus_types import NexusDecision


def test_normalize_mmr_accepts_fractional_exchange_value():
    assert _normalize_mmr(0.004) == 0.004
    assert _normalize_mmr("0.015") == 0.015


def test_normalize_mmr_rejects_missing_nonfinite_boolean_and_out_of_range():
    assert _normalize_mmr(None) is None
    assert _normalize_mmr(True) is None
    assert _normalize_mmr("nan") is None
    assert _normalize_mmr(0) is None
    assert _normalize_mmr(1) is None
    assert _normalize_mmr(-0.01) is None


def test_positive_finite_rejects_invalid_account_margin():
    assert _positive_finite("18.5482") == 18.5482
    assert _positive_finite(0) is None
    assert _positive_finite(-1) is None
    assert _positive_finite(False) is None
    assert _positive_finite("inf") is None


def test_contract_list_supports_kucoin_shapes_without_inventing_data():
    rows = [{"symbol": "ADAUSDTM", "maintainMargin": 0.015}]
    assert _contract_list(rows) is rows
    assert _contract_list({"data": rows}) is rows
    assert _contract_list({"dataList": rows}) is rows
    assert _contract_list({"items": rows}) is rows
    assert _contract_list({"data": {"symbol": "ADAUSDTM"}}) == []
    assert _contract_list(None) == []


def test_select_cross_risk_requires_exact_symbol_and_valid_mmr():
    rows = [
        {"symbol": "XBTUSDTM", "mmr": "0.0041", "leverage": "50"},
        {
            "symbol": "ADAUSDTM",
            "mmr": "0.0123",
            "imr": "0.0200",
            "totalMargin": "18.5482",
            "price": "0.205",
            "leverage": "50.00",
        },
    ]
    risk = _select_cross_risk(rows, "ADAUSDTM")
    assert risk["mmr"] == 0.0123
    assert risk["imr"] == 0.02
    assert risk["totalMargin"] == 18.5482
    assert risk["leverage"] == 50.0
    assert _select_cross_risk(rows, "ETHUSDTM") is None


def test_select_cross_risk_fails_closed_on_invalid_exact_row():
    rows = [
        {"symbol": "ADAUSDTM", "mmr": "nan", "leverage": "50"},
        {"symbol": "XBTUSDTM", "mmr": "0.004", "leverage": "50"},
    ]
    assert _select_cross_risk({"data": rows}, "ADAUSDTM") is None


class _Log:
    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


class _Pilot:
    enabled = True


@dataclass
class _Signal:
    symbol: str = "ADAUSDT"
    direction: str = "SHORT"
    entry: float = 0.20469
    sl: float = 0.208927
    tp: float = 0.196216
    rr: float = 2.0
    tp1: float = 0.196216
    tp2: float = 0.196216
    rr1: float = 2.0
    rr2: float = 2.0
    total_fees: float = 0.17
    expected_pnl: float = 3.97
    reason: str = "fixture"


class _ClientBase:
    _official_contract_mmr_installed = False

    def __init__(self, events, fail_cross=False, mmr="0.006691"):
        self.events = events
        self.fail_cross = fail_cross
        self.mmr = mmr
        self._cross_mmr_symbols = set()
        self._cross_mmr_cache = {}
        self.cross_calls = 0
        self.account_calls = 0
        self.requested_leverages = []

    async def load_instruments(self):
        return {"ADAUSDT": {"kucoinSymbol": "ADAUSDTM"}}

    async def _get(self, path, params=None, auth=False):
        if path == "/api/v1/contracts/active":
            return [{"symbol": "ADAUSDTM", "maintainMargin": "0.0065"}]
        if path == "/api/v1/account-overview":
            self.account_calls += 1
            self.events.append("account")
            return {"marginBalance": "18.5482"}
        if path == "/api/v2/batchGetCrossOrderLimit":
            self.cross_calls += 1
            self.events.append("cross")
            self.requested_leverages.append(str((params or {}).get("leverage")))
            if self.fail_cross:
                raise RuntimeError("unavailable")
            return [{
                "symbol": "ADAUSDTM",
                "mmr": self.mmr,
                "imr": "0.0200",
                "totalMargin": "18.5482",
                "price": "0.20469",
                "leverage": str(cfg.LEVERAGE),
            }]
        raise AssertionError(path)


def _decision(sig, allowed=True, reason="approved"):
    return NexusDecision(
        symbol=sig.symbol,
        decision=sig.direction if allowed else "WAIT",
        confidence=70.0 if allowed else 0.0,
        setup_quality=70.0 if allowed else 0.0,
        data_quality=100.0,
        entry=float(sig.entry),
        stop_loss=float(sig.sl),
        take_profit=float(sig.tp),
        risk_reward=float(sig.rr),
        expected_value=0.50 if allowed else 0.0,
        execution_allowed=bool(allowed),
        reasoning=[reason],
    )


def _build_runtime(*, first_approval=True, second_approval=True,
                   fail_cross=False, signal=None):
    events = []

    class Scoring:
        @staticmethod
        async def calculate(symbol, direction, closes, highs, lows, volumes, client=None):
            events.append("legacy_score")
            return {
                "total": 54,
                "tecnico": 20,
                "orderflow": 23,
                "macro": 11,
                "news_mod": 0,
                "aprovado": False,
            }

    scoring = Scoring()

    class Client(_ClientBase):
        _official_contract_mmr_installed = False

    class Engine:
        def __init__(self, client):
            self.paper_trade = False
            self.pilot = _Pilot()
            self.client = client
            self.instruments = {"ADAUSDT": {"kucoinSymbol": "ADAUSDTM"}}
            self.positions = {}
            self.nexus_calls = 0
            self.sizing_calls = 0

        async def _nexus_validate(self, sig):
            self.nexus_calls += 1
            events.append((
                "nexus",
                self.nexus_calls,
                round(float(sig.sl), 8),
                round(float(sig.tp), 8),
            ))
            allowed = first_approval if self.nexus_calls == 1 else second_approval
            return _decision(sig, allowed=allowed)

        async def _open(self, sig):
            decision = await self._nexus_validate(sig)
            if decision.execution_allowed is not True:
                events.append("blocked_before_sizing")
                return decision
            self.sizing_calls += 1
            events.append("sized")
            return await scoring.calculate(
                sig.symbol, sig.direction,
                [1.0, 0.9], [1.1, 1.0], [0.8, 0.7], [10.0, 12.0],
                self.client,
            )

    hardening.install(Client, Engine, scoring, liquidation, _Log())
    client = Client(events, fail_cross=fail_cross)
    engine = Engine(client)
    sig = signal or _Signal()
    return events, client, engine, sig


def _run_at_production_50x(engine, sig):
    """Exercise the integration under Railway's explicit 50x setting.

    CI intentionally has a conservative 10x fallback, so tests must set the
    production-specific value locally and restore it to avoid contaminating
    the rest of the offline suite.
    """
    original = cfg.LEVERAGE
    cfg.LEVERAGE = 50
    try:
        return asyncio.run(engine._open(sig))
    finally:
        cfg.LEVERAGE = original


def test_geometry_real_ada_example_uses_exact_cross_mmr_and_stays_50x_safe():
    liquidation.set_mmr_from_api(
        "ADAUSDT", 0.006691, source="test_exact_cross_mmr"
    )
    sig = _Signal()
    geometry = _geometry_from_exact_mmr(liquidation, sig, 50)

    assert geometry["status"] == "ADJUSTED"
    assert geometry["retained_fraction"] >= 0.40
    assert geometry["retained_fraction"] < 0.50
    assert abs(geometry["rr"] - 2.0) < 0.01
    assert geometry["final_stop_pct"] < geometry["original_stop_pct"]

    final_check = liquidation.analyze(
        entry=sig.entry,
        stop=geometry["sl"],
        leverage=50,
        is_long=False,
        symbol=sig.symbol,
        n_open_positions=1,
    )
    assert final_check.stop_effective is True
    assert final_check.gap_pct >= liquidation.MIN_GAP_PCT


def test_live_unsafe_50x_geometry_is_no_trade_without_compressing_technical_stop():
    # F-003: the technical stop is never compressed to fit the liquidation
    # buffer; a stop outside the safe zone at the configured leverage is NO TRADE.
    events, client, engine, sig = _build_runtime()
    old_sl, old_tp = sig.sl, sig.tp

    result = _run_at_production_50x(engine, sig)

    assert result.execution_allowed is False
    assert "TECHNICAL_STOP_OUTSIDE_LIQUIDATION_SAFE_ZONE" in str(result.to_dict())
    assert (sig.sl, sig.tp) == (old_sl, old_tp), "technical stop untouched"
    assert engine.nexus_calls == 1, "no second NEXUS decision on fabricated geometry"
    assert engine.sizing_calls == 0
    assert client.cross_calls == 1
    assert client.requested_leverages == ["50"]
    assert "blocked_before_sizing" in events


def test_pretrade_reuses_fresh_presizing_cross_mmr_cache_without_second_private_call():
    safe_sig = _Signal(sl=0.20571345, tp=0.20264310, rr=2.0, tp1=0.20264310, tp2=0.20264310)
    events, client, engine, sig = _build_runtime(signal=safe_sig)
    _run_at_production_50x(engine, sig)
    assert client.account_calls == 1
    assert client.cross_calls == 1
    assert client.requested_leverages == ["50"]
    assert events.count("legacy_score") == 1


def test_initial_nexus_reject_spends_no_cross_risk_private_request():
    events, client, engine, sig = _build_runtime(first_approval=False)
    result = _run_at_production_50x(engine, sig)

    assert result.execution_allowed is False
    assert client.account_calls == 0
    assert client.cross_calls == 0
    assert engine.sizing_calls == 0
    assert "blocked_before_sizing" in events


def test_cross_mmr_failure_after_nexus_fails_closed_before_sizing():
    events, client, engine, sig = _build_runtime(fail_cross=True)
    result = _run_at_production_50x(engine, sig)

    assert result.execution_allowed is False
    assert client.account_calls == 1
    assert client.cross_calls == 1
    assert client.requested_leverages == ["50"]
    assert engine.sizing_calls == 0
    assert events[-1] == "blocked_before_sizing"
    assert "legacy_score" not in events


def test_unsafe_geometry_never_asks_nexus_to_approve_a_compressed_stop():
    events, client, engine, sig = _build_runtime(second_approval=True)
    result = _run_at_production_50x(engine, sig)

    assert result.execution_allowed is False
    assert engine.nexus_calls == 1
    assert engine.sizing_calls == 0
    assert "legacy_score" not in events


def test_already_safe_geometry_keeps_levels_and_requires_only_one_nexus_decision():
    safe_sig = _Signal(
        sl=0.20571345,  # about 0.50% above SHORT entry
        tp=0.20264310,  # about 1.00% below entry
        rr=2.0,
        tp1=0.20264310,
        tp2=0.20264310,
    )
    events, client, engine, sig = _build_runtime(signal=safe_sig)
    old_levels = (sig.sl, sig.tp)

    result = _run_at_production_50x(engine, sig)

    assert (sig.sl, sig.tp) == old_levels
    assert engine.nexus_calls == 1
    assert engine.sizing_calls == 1
    assert client.cross_calls == 1
    assert client.requested_leverages == ["50"]
    assert result["total"] == 54
    assert events[-2:] == ["sized", "legacy_score"]


def test_pathologically_wide_stop_is_blocked_instead_of_overcompressed():
    wide_sig = _Signal(
        sl=0.21083070,  # about 3.0% above SHORT entry
        tp=0.19240860,  # about 6.0% below entry
        rr=2.0,
        tp1=0.19240860,
        tp2=0.19240860,
    )
    events, client, engine, sig = _build_runtime(signal=wide_sig)
    result = _run_at_production_50x(engine, sig)

    assert result.execution_allowed is False
    assert engine.nexus_calls == 1
    assert engine.sizing_calls == 0
    assert client.cross_calls == 1
    assert client.requested_leverages == ["50"]
    assert "blocked_before_sizing" in events
    assert "legacy_score" not in events


def test_existing_cross_position_blocks_second_entry_before_private_risk_request():
    events, client, engine, sig = _build_runtime()
    engine.positions = {"BTCUSDT": object()}
    result = _run_at_production_50x(engine, sig)

    assert result.execution_allowed is False
    assert engine.nexus_calls == 1
    assert engine.sizing_calls == 0
    assert client.cross_calls == 0
    assert client.account_calls == 0
    assert "blocked_before_sizing" in events
