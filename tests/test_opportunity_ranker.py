from bot.opportunity_ranker import Opportunity, rank_opportunities


def test_better_ev_rr_and_liquidity_rank_higher():
    better = Opportunity("a", "BTCUSDT", 0.012, 2.2, 80, 95, 85, 0.001)
    worse = Opportunity("b", "ALTUSDT", 0.002, 1.2, 65, 50, 60, 0.004)
    ranked = rank_opportunities([worse, better])
    assert ranked[0][0].candidate_id == "a"
    assert ranked[0][1] > ranked[1][1]


def test_tie_break_is_deterministic():
    a = Opportunity("a", "BTCUSDT", 0.01, 2, 80, 80, 80, 0.001)
    b = Opportunity("b", "ETHUSDT", 0.01, 2, 80, 80, 80, 0.001)
    assert [x[0].candidate_id for x in rank_opportunities([b, a])] == ["a", "b"]
