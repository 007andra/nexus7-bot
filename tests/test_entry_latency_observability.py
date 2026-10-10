from bot.entry_latency_observability import EntryLatencyCollector


def test_full_entry_latency_trace_emits_stage_metrics_and_summary():
    c = EntryLatencyCollector()
    events = [
        ("🔍 [ATOMUSDT] WS cache hit (15m=100 1h=100 4h=100) — sem REST", 1_000_000_000),
        ("[ATOMUSDT] ✅ SINAL SHORT score=70/100 RR=2.0 entry=PULLBACK", 1_010_000_000),
        ("[PULLBACK_CONFIRMATION] symbol=ATOMUSDT side=SHORT result=APPROVED reason=closed_votes", 1_020_000_000),
        ("[AI_DECISION] symbol=ATOMUSDT side=SHORT decision=APPROVE approved=True", 1_050_000_000),
        ("🔎 _open ATOMUSDT SHORT | entry=1 sl=2 tp=0.5 | qty=1", 1_060_000_000),
        ("📡 _open ATOMUSDT tentativa 1/1 | side=Sell qty=1", 1_070_000_000),
        ("📤 [ORDER] clientOid=bgx7-x orderId=123 symbol=ATOMUSDT side=Sell qty=1 leverage=50x", 1_120_000_000),
        ("🛡️ ATOMUSDT: SL=$2.0000 TP=$0.5000 anexados à posição", 1_550_000_000),
        ("✅ [FILLED] source=REST clientOid=bgx7-x orderId=123 symbol=ATOMUSDT filledSize=1", 1_600_000_000),
    ]
    output = []
    for msg, ts in events:
        output.extend(c.observe(msg, ts))

    summary = [line for line in output if line.startswith("[ENTRY_LATENCY_SUMMARY]")][-1]
    assert "symbol=ATOMUSDT" in summary
    assert "market_to_signal_ms=10.0" in summary
    assert "send_to_ack_ms=50.0" in summary
    assert "ack_to_fill_ms=480.0" in summary
    assert "ack_to_tpsl_ms=430.0" in summary
    assert "telemetry_only=true" in summary
    assert "execution_effect=NONE" in summary


def test_candidate_after_signal_does_not_create_duplicate_trace():
    c = EntryLatencyCollector()
    c.observe("🔍 [ETHUSDT] WS cache hit (15m=100 1h=100 4h=100) — sem REST", 1_000_000_000)
    first = c.observe("[ETHUSDT] ✅ SINAL LONG score=70/100 RR=2.0 entry=PULLBACK", 1_010_000_000)
    duplicate = c.observe("✅ [ETHUSDT] CANDIDATO: LONG score=70 R:R=2.0 PnL_est=+1.00%", 1_012_000_000)
    assert len(first) == 1
    assert "trace=ETHUSDT-1" in first[0]
    assert duplicate == []


def test_latest_market_observation_is_used_for_next_signal():
    c = EntryLatencyCollector()
    c.observe("🔍 [ATOMUSDT] WS cache hit (15m=100 1h=100 4h=100) — sem REST", 1_000_000_000)
    c.observe("[ATOMUSDT] ✅ SINAL SHORT score=63/100 RR=2.0 entry=PULLBACK", 1_010_000_000)
    c.observe("[PULLBACK_CONFIRMATION] symbol=ATOMUSDT side=SHORT result=BLOCKED reason=insufficient_reversal_votes", 1_020_000_000)
    c.observe("🔍 [ATOMUSDT] WS cache hit (15m=100 1h=100 4h=100) — sem REST", 2_000_000_000)
    output = c.observe("[ATOMUSDT] ✅ SINAL SHORT score=66/100 RR=2.0 entry=PULLBACK", 2_025_000_000)
    assert "trace=ATOMUSDT-2" in output[0]
    assert "stage_ms=25.0" in output[0]
    assert "total_ms=25.0" in output[0]


def test_blocked_pullback_emits_terminal_summary_without_execution_stages():
    c = EntryLatencyCollector()
    c.observe("🔍 [ATOMUSDT] WS cache hit (15m=100 1h=100 4h=100) — sem REST", 1_000_000_000)
    c.observe("[ATOMUSDT] ✅ SINAL SHORT score=63/100 RR=2.0 entry=PULLBACK", 1_010_000_000)
    output = c.observe(
        "[PULLBACK_CONFIRMATION] symbol=ATOMUSDT side=SHORT result=BLOCKED reason=insufficient_reversal_votes",
        1_020_000_000,
    )
    summary = output[-1]
    assert "terminal=pullback" in summary
    assert "market_to_signal_ms=10.0" in summary
    assert "signal_to_pullback_ms=10.0" in summary
    assert "send_to_ack_ms=NA" in summary


def test_rejected_nexus_terminates_trace_without_fabricating_order_latency():
    c = EntryLatencyCollector()
    c.observe("🔍 [ETHUSDT] WS cache hit (15m=100 1h=100 4h=100) — sem REST", 1_990_000_000)
    c.observe("[ETHUSDT] ✅ SINAL LONG score=70/100 RR=2.0 entry=PULLBACK", 2_000_000_000)
    c.observe("[PULLBACK_CONFIRMATION] symbol=ETHUSDT side=LONG result=APPROVED reason=closed_votes", 2_010_000_000)
    output = c.observe(
        "[AI_DECISION] symbol=ETHUSDT side=LONG decision=REJECT approved=False decision_source=nexus_ai",
        2_030_000_000,
    )
    summary = output[-1]
    assert "terminal=nexus" in summary
    assert "market_to_signal_ms=10.0" in summary
    assert "pullback_to_nexus_ms=20.0" in summary
    assert "ack_to_fill_ms=NA" in summary


def test_generic_order_without_open_dispatch_is_ignored():
    c = EntryLatencyCollector()
    c.observe("[NEARUSDT] ✅ SINAL LONG score=70/100 RR=2.0 entry=MOMENTUM", 1_000_000_000)
    output = c.observe(
        "📤 [ORDER] clientOid=bgx7-close orderId=close-1 symbol=NEARUSDT side=Sell qty=10 leverage=50x",
        20_000_000_000,
    )
    assert output == []
    assert "exchange_ack" not in c._traces["NEARUSDT"].stages


def test_reduce_only_close_order_after_completed_entry_cannot_overwrite_ack_latency():
    c = EntryLatencyCollector()
    c.observe("[NEARUSDT] ✅ SINAL LONG score=70/100 RR=2.0 entry=MOMENTUM", 1_000_000_000)
    c.observe("🔎 _open NEARUSDT LONG | entry=2 sl=1.9 tp=2.2 | qty=10", 1_010_000_000)
    c.observe("📡 _open NEARUSDT tentativa 1/1 | side=Buy qty=10", 1_020_000_000)
    ack = c.observe(
        "📤 [ORDER] clientOid=bgx7-open orderId=open-1 symbol=NEARUSDT side=Buy qty=10 leverage=50x",
        1_050_000_000,
    )
    assert len(ack) == 1
    assert "stage=exchange_ack" in ack[0]
    c.observe(
        "✅ [FILLED] source=REST clientOid=bgx7-open orderId=open-1 symbol=NEARUSDT filledSize=10",
        1_100_000_000,
    )

    close_ack = c.observe(
        "📤 [ORDER] clientOid=bgx7-close orderId=close-1 symbol=NEARUSDT side=Sell qty=10 leverage=50x",
        301_000_000_000,
    )
    close_fill = c.observe(
        "✅ [FILLED] source=REST clientOid=bgx7-close orderId=close-1 symbol=NEARUSDT filledSize=10",
        301_100_000_000,
    )

    assert close_ack == []
    assert close_fill == []
    assert c._traces["NEARUSDT"].stages["exchange_ack"] == 1_050_000_000
    assert c._traces["NEARUSDT"].stages["fill"] == 1_100_000_000


def test_fill_for_unknown_order_id_is_ignored_even_when_symbol_has_trace():
    c = EntryLatencyCollector()
    c.observe("[ATOMUSDT] ✅ SINAL SHORT score=70/100 RR=2.0 entry=PULLBACK", 1_000_000_000)
    c.observe("🔎 _open ATOMUSDT SHORT | entry=1 sl=2 tp=0.5 | qty=1", 1_010_000_000)
    c.observe("📡 _open ATOMUSDT tentativa 1/1 | side=Sell qty=1", 1_020_000_000)
    output = c.observe(
        "✅ [FILLED] source=REST clientOid=bgx7-other orderId=other-1 symbol=ATOMUSDT filledSize=1",
        1_040_000_000,
    )
    assert output == []
    assert "fill" not in c._traces["ATOMUSDT"].stages


def test_unrelated_logs_are_ignored():
    c = EntryLatencyCollector()
    assert c.observe("heartbeat ok", 1_000_000_000) == []


def test_unknown_market_data_is_reported_as_na_never_zero():
    """Audit P1-1: without an observed acquisition event the value is unknown."""
    c = EntryLatencyCollector()
    first = c.observe("[ATOMUSDT] ✅ SINAL SHORT score=63/100 RR=2.0 entry=PULLBACK", 1_010_000_000)
    assert "stage_ms=NA" in first[0]
    assert "total_ms=NA" in first[0]
    output = c.observe(
        "[PULLBACK_CONFIRMATION] symbol=ATOMUSDT side=SHORT result=BLOCKED reason=x",
        1_020_000_000,
    )
    summary = output[-1]
    assert "market_to_signal_ms=NA" in summary
    assert "market_to_signal_ms=0.0" not in summary
    assert "market_data_observed=false" in summary
    assert "signal_to_pullback_ms=10.0" in summary
