from dataclasses import replace

import pytest

from engine_core import (
    ArrivalOrderStatus,
    BatchQuality,
    CanonicalReplayBlocked,
    CanonicalReplayStatus,
    DataStatus,
    DeterministicEngine,
    FieldMetaV1,
    FieldQuality,
    MarketTickV1,
    MarketStateReducer,
    OfflineCanonicalReplay,
    ProbeStrategy,
    ReplaySessionTimeline,
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


def test_event_outside_replay_window_is_rejected_instead_of_dropped():
    source = OfflineCanonicalReplay(
        TRADE_DATE,
        ("600000",),
        start_ms=START,
        end_exclusive_ms=START + 3_000,
    )
    with pytest.raises(CanonicalReplayBlocked, match="outside"):
        tuple(source.iter_frame_results([_batch([_tick(event_time_ms=START + 3_000)])]))


def test_skipped_tick_diagnostics_stay_in_their_event_time_frame():
    source = OfflineCanonicalReplay(
        TRADE_DATE,
        ("600000",),
        start_ms=START,
        end_exclusive_ms=START + 9_000,
    )
    invalid = _tick(
        event_time_ms=START + 6_500,
        px_milli=None,
        field_meta=(("px_milli", FieldMetaV1(FieldQuality.MISSING)),),
    )
    results = tuple(source.iter_frame_results([_batch([_tick(), invalid])]))
    assert results[0].status is CanonicalReplayStatus.READY
    assert results[1].status is CanonicalReplayStatus.EMPTY
    assert results[2].status is CanonicalReplayStatus.BLOCKED
    assert results[2].skipped_symbols == ("600000",)


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


def test_all_missing_batch_is_degraded_and_does_not_stop_the_timeline():
    source = OfflineCanonicalReplay(
        TRADE_DATE,
        ("600000",),
        start_ms=START,
        end_exclusive_ms=START + 6_000,
    )
    invalid = _tick(
        px_milli=None,
        field_meta=(("px_milli", FieldMetaV1(FieldQuality.MISSING)),),
    )
    valid = _tick(event_time_ms=START + 3_500)
    results = tuple(source.iter_frame_results([_batch([invalid]), _batch([valid])]))
    assert results[0].status is CanonicalReplayStatus.BLOCKED
    assert results[0].frame.completeness == "EMPTY"
    assert results[0].skipped_symbols == ("600000",)
    assert results[1].status is CanonicalReplayStatus.READY
    assert len(results) == 2

    class CaptureEngine:
        def __init__(self):
            self.signals = []

        def submit(self, signal):
            self.signals.append(signal)

        def run_until_empty(self):
            return None

    engine = CaptureEngine()
    source.replay([_batch([invalid]), _batch([valid])], engine)
    assert len(engine.signals) == 2
    assert engine.signals[0].payload.replay_status == "BLOCKED"
    assert engine.signals[0].payload.status is DataStatus.MISSING


def test_empty_batch_keeps_frame_identity_and_source_sequence():
    source = OfflineCanonicalReplay(
        TRADE_DATE,
        ("600000",),
        start_ms=START,
        end_exclusive_ms=START + 9_000,
    )
    empty = replace(
        _batch([], logical_ts_ms=START + 3_000),
        source_sequence="wire-0",
        source_sequence_status=SequenceStatus.KNOWN,
    )
    results = tuple(source.iter_frame_results([empty]))
    assert len(results) == 3
    assert results[0].status is CanonicalReplayStatus.EMPTY
    assert results[0].frame.batch_quality == "EMPTY"
    assert results[0].frame.source_batch_ids == (empty.source_batch_id,)
    assert results[0].source_sequences == ("wire-0",)
    assert results[1].status is CanonicalReplayStatus.EMPTY


def test_conflicting_historical_availability_is_not_collapsed_to_known():
    source = OfflineCanonicalReplay(
        TRADE_DATE,
        ("600000",),
        start_ms=START,
        end_exclusive_ms=START + 3_000,
    )
    first = replace(
        _batch([_tick(event_time_ms=START + 100)]),
        historical_available_at_ms=START + 50,
        historical_available_at_status="KNOWN",
    )
    second = replace(
        _batch([_tick(event_time_ms=START + 200)]),
        historical_available_at_ms=START + 60,
        historical_available_at_status="KNOWN",
    )
    result = next(source.iter_frame_results([first, second]))
    assert result.historical_available_at_ms is None
    assert result.historical_available_at_status == "UNKNOWN"


def test_canonical_replay_retains_all_500_empty_frames():
    source = OfflineCanonicalReplay(
        TRADE_DATE,
        ("600000",),
        start_ms=START,
        end_exclusive_ms=START + 500 * 3_000,
    )
    results = tuple(source.iter_frame_results(()))
    assert len(results) == 500
    assert tuple(item.frame.frame_no for item in results) == tuple(range(500))
    assert all(item.status is CanonicalReplayStatus.EMPTY for item in results)
    assert all(item.batch_quality == "UNKNOWN" for item in results)


def test_canonical_replay_can_join_one_session_timeline():
    base = OfflineCanonicalReplay(
        TRADE_DATE,
        ("600000",),
        start_ms=START,
        end_exclusive_ms=START + 9_000,
    )
    manifest = base.source.manifest(base.source.frames(()))
    timeline = ReplaySessionTimeline(manifest)
    replay = OfflineCanonicalReplay(
        TRADE_DATE,
        ("600000",),
        start_ms=START,
        end_exclusive_ms=START + 9_000,
        session_timeline=timeline,
    )

    class CaptureEngine:
        def submit(self, signal):
            return None

        def run_until_empty(self):
            return None

    replay.replay((), CaptureEngine())
    assert timeline.frame_count == 3
    at_0925 = local_datetime_ms(TRADE_DATE, "09:25:06")
    revision = replay.observe_auction(
        "0925",
        [{"symbol": "600000", "ts": at_0925}],
        evaluation_time_ms=at_0925,
        expected_symbols=("600000",),
    )
    assert revision.revision == 1
    assert timeline.snapshot()["anchors"]["0925"]["revision"] == 1
    timeline.finalize()


def test_canonical_replay_session_timeline_covers_500_frames_and_auction_anchor():
    end = START + 500 * 3_000
    base = OfflineCanonicalReplay(
        TRADE_DATE,
        ("600000",),
        start_ms=START,
        end_exclusive_ms=end,
    )
    timeline = ReplaySessionTimeline(base.source.manifest(base.source.frames(())))
    replay = OfflineCanonicalReplay(
        TRADE_DATE,
        ("600000",),
        start_ms=START,
        end_exclusive_ms=end,
        session_timeline=timeline,
    )

    class CaptureEngine:
        def submit(self, signal):
            return None

        def run_until_empty(self):
            return None

    replay.replay((), CaptureEngine())
    assert timeline.frame_count == 500
    assert timeline.node_count == 500

    at_0925 = local_datetime_ms(TRADE_DATE, "09:25:06")
    revision = replay.observe_auction(
        "0925",
        [{"symbol": "600000", "ts": at_0925}],
        evaluation_time_ms=at_0925,
        expected_symbols=("600000",),
    )
    assert revision.revision == 1
    checkpoint = timeline.finalize()
    assert checkpoint.node_id == "CHECKPOINT:0940"
    snapshot = timeline.snapshot()
    assert snapshot["frame_count"] == 500
    assert snapshot["expected_frame_count"] == 500
    assert snapshot["anchors"]["0925"]["revision"] == 1
    assert snapshot["node_count"] == 502


def test_replay_propagates_frame_diagnostics_and_batch_quality():
    source = OfflineCanonicalReplay(
        TRADE_DATE,
        ("600000",),
        start_ms=START,
        end_exclusive_ms=START + 3_000,
    )
    invalid = _tick(
        event_time_ms=START + 1_000,
        px_milli=None,
        field_meta=(("px_milli", FieldMetaV1(FieldQuality.MISSING)),),
    )
    batch = _batch([_tick(), invalid])
    batch = replace(
        batch,
        batch_quality=BatchQuality.PARTIAL,
        same_event_order_ambiguity=True,
        source_sequence="wire-1",
        source_sequence_status=SequenceStatus.KNOWN,
        replay_order_status=ReplayOrderStatus.SYNTHETIC_DETERMINISTIC,
    )

    class CaptureEngine:
        def __init__(self):
            self.signals = []

        def submit(self, signal):
            self.signals.append(signal)

        def run_until_empty(self):
            return None

    engine = CaptureEngine()
    source.replay([batch], engine)
    payload = engine.signals[0].payload
    assert payload.status is DataStatus.PARTIAL
    assert payload.replay_status == "PARTIAL"
    assert payload.replay_reasons
    assert payload.skipped_symbols == ("600000",)
    assert payload.batch_quality == "PARTIAL"
    assert payload.same_event_order_ambiguity is True
    assert payload.cross_section.replay_order_status == "SYNTHETIC_DETERMINISTIC"
    assert payload.source_sequences == ("wire-1",)

    result = next(source.iter_frame_results([batch]))
    assert result.frame.batch_quality == "PARTIAL"
    assert result.frame.same_event_order_ambiguity is True
    normal = next(source.iter_frame_results([_batch([_tick()])]))
    assert normal.frame.evidence_hash != result.frame.evidence_hash
    assert normal.content_hash != result.content_hash
