import pytest
from bot.kronos_shadow import shadow_record, summarize_forecast_paths

def _path(*closes):
    return [{"close":c,"high":c*1.01,"low":c*0.99} for c in closes]

def test_probabilistic_summary_is_execution_neutral():
    f=summarize_forecast_paths([_path(101,102),_path(100,103),_path(99,98),_path(101,104)],reference_price=100)
    assert f.sample_count==4 and f.horizon_steps==2
    assert f.probability_up==pytest.approx(0.75)
    assert f.probability_down==pytest.approx(0.25)
    assert 0.0 <= f.confidence <= 1.0
    record=shadow_record("linkusdt","15m",f)
    assert record["mode"]=="shadow" and record["execution_authority"] is False
    assert not ({"side","qty","leverage"} & set(record))

def test_rejects_mismatched_horizons():
    with pytest.raises(ValueError,match="same horizon"):
        summarize_forecast_paths([_path(101),_path(101,102)],reference_price=100)

@pytest.mark.parametrize("reference",[0,-1,float("nan"),float("inf")])
def test_rejects_invalid_reference(reference):
    with pytest.raises(ValueError):
        summarize_forecast_paths([_path(101)],reference_price=reference)

def test_rejects_invalid_candle_geometry():
    with pytest.raises(ValueError,match="high"):
        summarize_forecast_paths([[{"close":101,"high":99,"low":100}]],reference_price=100)
