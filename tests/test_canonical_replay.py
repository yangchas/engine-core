import pytest

from engine_core import (
    ArrivalOrderStatus,
    BatchQuality,
    CanonicalReplayBlocked,
    CanonicalReplayStatus,
    DeterministicEngine,
    FieldMetaV1,
    FieldQuality,
    MarketTickV1,
    MarketStateReducer,
    OfflineCanonicalReplay,
    ProbeStrategy,
    ReplayOrderStatus,
    SequenceStatus,
    TickBatchV1,
    WindowManager,
    WindowSpec,
    local_datetime_ms,
)


TRADE_DATE = "2026-09-18"
START = local_datetime_ms(TRADE_DATE, "09:15:00")


def _tick(symbol="600000", event_time_ms=START + 500, **overrides):
    values = {
        "trade_date": TRADE_DATE,
        "event_time_ms": event_time_ms,
        "symbol": symbol,
        "market_code": "sh",
        "px_milli": 10000,
        "pc_milli": 9900,
        "amt_yuan": 100,
        "vol_units": 10,
    }
    values.update(overrides)
    return MarketTickV1(**values)


def _batch(ticks, *, logical_ts_ms=START + 3_000, mode="REPLAY"):
    return TickBatchV1(
        schema_version=1,
        hash_encoding_version="CanonicalHashEncodingV1",
        mode=mode,
        trade_date=TRADE_DATE,
        logical_ts_ms=logical_ts_ms,
        wall_ts_ms=None,
        seq_no=0,
        source="rabbit",
        source_batch_id="fixture-%d" % logical_ts_ms,
        source_sequence=None,
        source_sequence_status=SequenceStatus.UNKNOWN,
        arrival_order_status=ArrivalOrderStatus.UNKNOWN,
        replay_order_status=ReplayOrderStatus.UNKNOWN,
        historical_available_at_ms=None,
        historical_available_at_status="UNKNOWN",
        same_event_order_ambiguity=False,
        batch_quality=BatchQuality.UNKNOWN if ticks else BatchQuality.EMPTY,
        ticks=tuple(ticks),
    )


def test_rabbit_primary_batch_projects_to_streamed_empty_preserving_frames():
    source = OfflineCanonicalReplay(
        TRADE_DATE,
        ("600000", "000001"),
        start_ms=START,
        end_exclusive_ms=START + 9_000,
    )
    batches = [
        _batch([_tick("600000", START + 500)]),
        _batch([_tick("000001", START + 6_500)], logical_ts_ms=START + 9_000),
    ]
    results = tuple(source.iter_frame_results(batches))
    assert len(results) == 3
    assert results[0].frame.completeness == "PARTIAL"
    assert results[1].frame.completeness == "EMPTY"
    assert results[2].frame.completeness == "PARTIAL"
    assert results[0].status is CanonicalReplayStatus.READY
    assert results[1].status is CanonicalReplayStatus.EMPTY
    assert results[2].status is CanonicalReplayStatus.READY


def test_missing_required_canonical_value_is_explicit_and_never_zero_filled():
    tick = _tick(
        px_milli=None,
        field_meta=(("px_milli", FieldMetaV1(FieldQuality.MISSING)),),
    )
    projection = OfflineCanonicalReplay.project_batch(_batch([tick]))
    assert projection.status is CanonicalReplayStatus.BLOCKED
    assert projection.events == ()
    assert projection.skipped_symbols == ("600000",)
    assert "px_milli" in projection.reasons[0]


def test_live_batch_is_rejected_from_offline_replay():
    with pytest.raises(CanonicalReplayBlocked, match="mode"):
        OfflineCanonicalReplay.project_batch(_batch([_tick()], mode="LIVE"))


def test_shuffled_batches_have_same_frame_hashes_when_events_are_equivalent():
    source = OfflineCanonicalReplay(
        TRADE_DATE,
        ("600000", "000001"),
        start_ms=START,
        end_exclusive_ms=START + 6_000,
    )
    first = [_batch([_tick("600000", START + 100), _tick("000001", START + 200)])]
    second = [_batch([_tick("000001", START + 200), _tick("600000", START + 100)])]
    left = tuple(item.frame.content_hash for item in source.iter_frame_results(first))
    right = tuple(item.frame.content_hash for item in source.iter_frame_results(second))
    assert left == right


def test_auction_timeline_remains_fact_only_with_optional_prior_anchors():
    source = OfflineCanonicalReplay(TRADE_DATE, ("600000",), start_ms=START, end_exclusive_ms=START + 3_000)
    at_0925 = local_datetime_ms(TRADE_DATE, "09:25:06")
    revision = source.observe_auction(
        "0925",
        [{"symbol": "600000", "ts": at_0925}],
        evaluation_time_ms=at_0925,
        expected_symbols=("600000", "000001"),
        source_layers=("RABBIT_FIXTURE",),
    )
    assert revision.state == "PARTIAL"
    analysis = source.auction_analysis("0925")
    assert analysis["fact_status"] == "FACT_ONLY"
    assert analysis["prior_deltas"] == {"0920": "UNKNOWN", "0924": "UNKNOWN"}
    assert analysis["recovery_plan"] is not None


def test_replay_uses_one_engine_update_per_global_frame():
    source = OfflineCanonicalReplay(
        TRADE_DATE,
        ("600000",),
        start_ms=START,
        end_exclusive_ms=START + 9_000,
    )
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("offline", START, START + 9_000),)),
        ProbeStrategy(),
        session_id="canonical-offline",
        phase="REPLAY",
    )
    source.replay([_batch([_tick()])], engine)
    assert engine._processed == 3
    assert engine._reducer.state.revision == 3
