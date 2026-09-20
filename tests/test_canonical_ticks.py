from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest
from zoneinfo import ZoneInfo

from engine_core import (
    ArrivalOrderStatus,
    BatchQuality,
    CANONICAL_SOURCE_AUTHORITY,
    CompatibilityResult,
    FieldMetaV1,
    FieldProvenance,
    FieldQuality,
    HistoricalAvailabilityStatus,
    MarketTickV1,
    ParityResult,
    RabbitFixtureAdapter,
    ReplayOrderStatus,
    SequenceStatus,
    TDFrameAdapter,
    TickBatchV1,
    classify_legacy_compatibility,
    compare_batches,
    compare_ticks,
    cxx_llround,
    normalize_td_symbol,
)
from engine_core.replay import TDEventV1


SHANGHAI = ZoneInfo("Asia/Shanghai")
START = int(datetime(2026, 9, 18, 9, 15, tzinfo=SHANGHAI).timestamp() * 1000)


def _tick(**overrides):
    values = {
        "trade_date": "2026-09-18",
        "event_time_ms": START,
        "symbol": "600000",
        "market_code": "sh",
        "px_milli": 10000,
        "pc_milli": 9900,
        "amt_yuan": 100,
        "vol_units": 10,
    }
    values.update(overrides)
    return MarketTickV1(**values)


def _row(ts, symbol="600000", **extra):
    row = {
        "ts": ts,
        "symbol": symbol,
        "market": "sh",
        "px_milli": 10000,
        "pc_milli": 9900,
        "amt_yuan": 100,
        "vol_units": 10,
    }
    row.update(extra)
    return row


def test_field_quality_value_combinations_are_frozen():
    with pytest.raises(ValueError, match="PRESENT_VALUE"):
        MarketTickV1(
            trade_date="2026-09-18",
            event_time_ms=1,
            symbol="600000",
            field_meta=(("px_milli", FieldMetaV1(FieldQuality.PRESENT_VALUE)),),
        )
    with pytest.raises(ValueError, match="typed NULL"):
        MarketTickV1(
            trade_date="2026-09-18",
            event_time_ms=1,
            symbol="600000",
            px_milli=0,
            field_meta=(("px_milli", FieldMetaV1(FieldQuality.UNKNOWN)),),
        )
    tick = MarketTickV1(
        trade_date="2026-09-18",
        event_time_ms=1,
        symbol="600000",
        px_milli=0,
        field_meta=((
            "px_milli",
            FieldMetaV1(FieldQuality.WIRE_DEFAULT_AMBIGUOUS, FieldProvenance.SOURCE_SNAPSHOT),
        ),),
    )
    assert tick.px_milli == 0
    assert tick.field_meta_map["px_milli"].quality is FieldQuality.WIRE_DEFAULT_AMBIGUOUS


def test_invalid_numeric_quality_never_hashes_nan():
    with pytest.raises(ValueError, match="invalid_reason"):
        FieldMetaV1(FieldQuality.INVALID)
    with pytest.raises(ValueError, match="typed NULL"):
        MarketTickV1(
            trade_date="2026-09-18",
            event_time_ms=1,
            symbol="600000",
            px_milli=1,
            field_meta=((
                "px_milli",
                FieldMetaV1(FieldQuality.INVALID, invalid_reason="NON_FINITE"),
            ),),
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.49, 0), (0.5, 1), (1.5, 2), (-0.49, 0), (-0.5, -1), (-1.5, -2)],
)
def test_cxx_llround_uses_half_away_from_zero(value, expected):
    assert cxx_llround(value) == expected


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_cxx_llround_rejects_non_finite(value):
    with pytest.raises(ValueError):
        cxx_llround(value)


def test_cxx_llround_rejects_int64_overflow():
    with pytest.raises(OverflowError):
        cxx_llround(float(2**63))


def test_tick_hash_layers_and_content_alias_are_stable():
    left = _tick()
    right = _tick()
    assert left.canonical_value_hash == right.canonical_value_hash
    assert left.canonical_semantic_hash == right.canonical_semantic_hash
    assert left.canonical_content_hash == left.canonical_semantic_hash
    ambiguous = _tick(field_meta=((
        "px_milli",
        FieldMetaV1(FieldQuality.WIRE_DEFAULT_AMBIGUOUS),
    ),))
    assert ambiguous.px_milli == left.px_milli
    assert ambiguous.canonical_value_hash == left.canonical_value_hash
    assert ambiguous.canonical_semantic_hash != left.canonical_semantic_hash
    assert compare_ticks(left, ambiguous) is ParityResult.VALUE_EQUAL_QUALITY_DIFFERENT


def test_td_adapter_uses_stable_source_identity_and_unknown_completeness():
    start = START
    adapter = TDFrameAdapter()
    batch_a = adapter.build_batch(
        [_row(start + 1_000, "SH:600000")],
        trade_date="2026-09-18",
        frame_start_ms=start,
        frame_end_ms=start + 3_000,
        seq_no=100,
    )
    batch_b = adapter.build_batch(
        [_row(start + 1_000, "600000")],
        trade_date="2026-09-18",
        frame_start_ms=start,
        frame_end_ms=start + 3_000,
        seq_no=2,
    )
    assert batch_a.source_batch_id == batch_b.source_batch_id
    assert batch_a.source_evidence_hash == batch_b.source_evidence_hash
    assert batch_a.run_evidence_hash(run_id="a", wall_ts_ms=1) != batch_b.run_evidence_hash(run_id="a", wall_ts_ms=1)
    assert batch_a.batch_quality is BatchQuality.UNKNOWN
    assert batch_a.replay_order_status is ReplayOrderStatus.SYNTHETIC_DETERMINISTIC
    assert batch_a.arrival_order_status is ArrivalOrderStatus.UNKNOWN
    assert batch_a.source_sequence_status is SequenceStatus.UNKNOWN
    assert batch_a.historical_available_at_status is HistoricalAvailabilityStatus.UNKNOWN


def test_td_adapter_keeps_empty_frames_and_can_prove_complete_only_explicitly():
    start = START
    adapter = TDFrameAdapter()
    empty = adapter.build_batch(
        [], trade_date="2026-09-18", frame_start_ms=start,
        frame_end_ms=start + 3_000, seq_no=0,
    )
    complete = adapter.build_batch(
        [_row(start + 1_000)], trade_date="2026-09-18", frame_start_ms=start,
        frame_end_ms=start + 3_000, seq_no=0, completeness_proven=True,
    )
    assert empty.batch_quality is BatchQuality.EMPTY
    assert empty.ticks == ()
    assert complete.batch_quality is BatchQuality.COMPLETE


def test_td_adapter_marks_same_time_symbol_without_source_key_ambiguous():
    start = START
    rows = [
        _row(start + 1_000, amt_yuan=100),
        _row(start + 1_000, amt_yuan=120),
    ]
    batch = TDFrameAdapter().build_batch(
        rows, trade_date="2026-09-18", frame_start_ms=start,
        frame_end_ms=start + 3_000, seq_no=0,
    )
    assert batch.same_event_order_ambiguity is True
    assert batch.batch_quality is BatchQuality.UNKNOWN


def test_td_adapter_stable_key_removes_order_ambiguity():
    start = START
    rows = [
        _row(start + 1_000, amt_yuan=100, stable_td_row_key="a"),
        _row(start + 1_000, amt_yuan=120, stable_td_row_key="b"),
    ]
    batch = TDFrameAdapter().build_batch(
        rows, trade_date="2026-09-18", frame_start_ms=start,
        frame_end_ms=start + 3_000, seq_no=0, completeness_proven=True,
    )
    assert batch.same_event_order_ambiguity is False
    assert batch.batch_quality is BatchQuality.COMPLETE


def test_td_adapter_converts_source_float_units_with_cxx_rounding():
    row = {
        "ts": START + 1_000,
        "symbol": "SH:600000",
        "exchange": "SH",
        "lp": 10.005,
        "lc": 9.995,
        "a": 100.5,
        "v": 10,
        "ap1": 10.005,
    }
    batch = TDFrameAdapter().build_batch(
        [row], trade_date="2026-09-18", frame_start_ms=START,
        frame_end_ms=START + 3_000, seq_no=0,
    )
    tick = batch.ticks[0]
    assert tick.px_milli == 10005
    assert tick.pc_milli == 9995
    assert tick.amt_yuan == 101
    assert tick.ap_milli[0] == 10005


def test_td_adapter_marks_non_finite_source_as_invalid_null():
    row = _row(START + 1_000)
    row.pop("px_milli")
    row["lp"] = float("nan")
    batch = TDFrameAdapter().build_batch(
        [row], trade_date="2026-09-18", frame_start_ms=START,
        frame_end_ms=START + 3_000, seq_no=0,
    )
    tick = batch.ticks[0]
    assert tick.px_milli is None
    assert tick.field_meta_map["px_milli"].quality is FieldQuality.INVALID


def test_trade_date_invariant_rejects_mixed_batch():
    tick = _tick(trade_date="2026-09-19")
    with pytest.raises(ValueError, match="trade_date"):
        TickBatchV1(
            schema_version=1,
            hash_encoding_version="CanonicalHashEncodingV1",
            mode="REPLAY",
            trade_date="2026-09-18",
            logical_ts_ms=2,
            wall_ts_ms=None,
            seq_no=0,
            source="td",
            source_batch_id="td-replay:2026-09-18:1:2",
            source_sequence=None,
            source_sequence_status=SequenceStatus.UNKNOWN,
            arrival_order_status=ArrivalOrderStatus.UNKNOWN,
            replay_order_status=ReplayOrderStatus.SYNTHETIC_DETERMINISTIC,
            historical_available_at_ms=None,
            historical_available_at_status=HistoricalAvailabilityStatus.UNKNOWN,
            same_event_order_ambiguity=False,
            batch_quality=BatchQuality.UNKNOWN,
            ticks=(tick,),
        )


def test_batch_scope_and_value_mismatch_precedence():
    start = START
    adapter = TDFrameAdapter()
    left = adapter.build_batch(
        [_row(start + 1_000, amt_yuan=100)], trade_date="2026-09-18",
        frame_start_ms=start, frame_end_ms=start + 3_000, seq_no=0,
    )
    right = adapter.build_batch(
        [_row(start + 1_000, amt_yuan=120)], trade_date="2026-09-18",
        frame_start_ms=start, frame_end_ms=start + 3_000, seq_no=0,
    )
    assert compare_batches(left, right) is ParityResult.NOT_COMPARABLE
    assert compare_batches(left, right, equivalent_boundary=True) is ParityResult.VALUE_MISMATCH
    ambiguous = adapter.build_batch(
        [_row(start + 1_000, amt_yuan=100), _row(start + 1_000, amt_yuan=120)],
        trade_date="2026-09-18", frame_start_ms=start, frame_end_ms=start + 3_000,
        seq_no=0,
    )
    assert compare_batches(right, ambiguous, equivalent_boundary=True) is ParityResult.VALUE_MISMATCH


def test_legacy_compatibility_value_mismatch_precedes_order_ambiguity():
    old = [TDEventV1.from_mapping(_row(START, amt_yuan=100))]
    new = [_tick(amt_yuan=120)]
    assert classify_legacy_compatibility(old, new, order_ambiguous=True) is CompatibilityResult.VALUE_MISMATCH


def test_legacy_compatibility_compares_verified_values_not_source_metadata():
    old = [TDEventV1.from_mapping(_row(START, amt_yuan=100))]
    new = [_tick(amt_yuan=100)]
    assert classify_legacy_compatibility(old, new) is CompatibilityResult.STRICT_COMPATIBLE


def test_normalize_td_symbol_matches_cpp_td_row_rules():
    assert normalize_td_symbol("SH:600000") == "600000"
    assert normalize_td_symbol("600000.SH") == "600000"
    assert normalize_td_symbol("prefix-600000") == "600000"
    with pytest.raises(ValueError):
        normalize_td_symbol("bad")


def test_rabbit_fixture_adapter_uses_parsed_object_boundary_without_client_import():
    class Record:
        tss = START
        lp = 10.0
        lc = 9.9
        o = h = l = 10.0
        a = 100.0
        v = 10
        symbol = "600000"
        exchange = "SH"
        market = "sh"

        def ListFields(self):
            # Simulate proto3: only non-default values are present.
            class Descriptor:
                def __init__(self, name):
                    self.name = name
            return [(Descriptor(name), value) for name, value in (
                ("tss", self.tss), ("lp", self.lp), ("lc", self.lc),
                ("o", self.o), ("h", self.h), ("l", self.l),
                ("a", self.a), ("v", self.v), ("symbol", self.symbol),
                ("exchange", self.exchange), ("market", self.market),
            ) if value not in (0, False, "")]

    class Batch:
        batch_id = "rabbit-fixture-1"
        records = (Record(),)

    batch = RabbitFixtureAdapter().build_batch(
        Batch(), trade_date="2026-09-18", logical_ts_ms=START + 3_000, seq_no=0,
    )
    assert batch.source == "rabbit"
    assert batch.source_batch_id == "rabbit-fixture-1"
    assert batch.ticks[0].canonical_value_hash
    assert batch.batch_quality is BatchQuality.UNKNOWN


def test_rabbit_default_scalar_is_wire_ambiguous():
    class Record:
        tss = START
        lp = 0.0
        lc = 0.0
        o = h = l = 0.0
        a = 0.0
        v = 0
        symbol = "600000"
        exchange = "SH"
        market = "sh"

        def ListFields(self):
            return []

    class Batch:
        batch_id = "rabbit-defaults"
        records = (Record(),)

    tick = RabbitFixtureAdapter().build_batch(
        Batch(), trade_date="2026-09-18", logical_ts_ms=START + 3_000, seq_no=0,
    ).ticks[0]
    assert tick.px_milli == 0
    assert tick.field_meta_map["px_milli"].quality is FieldQuality.WIRE_DEFAULT_AMBIGUOUS


def test_rabbit_wire_shape_is_canonical_authority_and_absent_fields_are_missing():
    assert CANONICAL_SOURCE_AUTHORITY == "RABBITMQ_DATASERVICE_RAWTICK_V1"

    class Record:
        tss = START
        lp = 10.0
        lc = 9.9
        a = 100.0
        v = 10
        symbol = "600000"
        exchange = "SH"
        market = "sh"

        def ListFields(self):
            return []

    class Batch:
        batch_id = "rabbit-authority"
        records = (Record(),)

    tick = RabbitFixtureAdapter().build_batch(
        Batch(), trade_date="2026-09-18", logical_ts_ms=START + 3_000, seq_no=0,
    ).ticks[0]
    assert tick.field_meta_map["inst_vol"].quality is FieldQuality.MISSING
    assert tick.inst_vol is None


def test_rabbit_fixture_can_use_real_proto3_serialize_parse_when_runtime_available():
    protobuf = pytest.importorskip("google.protobuf")
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    file_proto = descriptor_pb2.FileDescriptorProto(
        name="canonical_fixture.proto", package="dataservice", syntax="proto3"
    )
    record = file_proto.message_type.add(name="DataRecord")
    fields = [
        ("tss", 1, 3), ("lp", 2, 2), ("lc", 6, 2), ("a", 7, 2),
        ("v", 8, 3), ("symbol", 30, 9), ("exchange", 31, 9),
        ("market", 32, 9),
    ]
    for name, number, field_type in fields:
        item = record.field.add(name=name, number=number, label=1, type=field_type)
    batch = file_proto.message_type.add(name="DataBatch")
    item = batch.field.add(name="batch_id", number=1, label=1, type=9)
    item = batch.field.add(name="records", number=2, label=3, type=11)
    item.type_name = ".dataservice.DataRecord"
    item = batch.field.add(name="sent_at", number=3, label=1, type=3)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    batch_desc = pool.FindMessageTypeByName("dataservice.DataBatch")
    record_desc = pool.FindMessageTypeByName("dataservice.DataRecord")
    get_class = getattr(message_factory, "GetMessageClass", None)
    if get_class is None:
        factory = message_factory.MessageFactory(pool)
        batch_cls = factory.GetPrototype(batch_desc)
        record_cls = factory.GetPrototype(record_desc)
    else:
        batch_cls = get_class(batch_desc)
        record_cls = get_class(record_desc)
    source = batch_cls(batch_id="wire-batch", sent_at=START + 3_000)
    source.records.add(tss=START, lp=10.0, lc=9.9, a=100.0, v=10,
                       symbol="600000", exchange="SH", market="sh")
    parsed = batch_cls()
    parsed.ParseFromString(source.SerializeToString())
    result = RabbitFixtureAdapter().build_batch(
        parsed, trade_date="2026-09-18", logical_ts_ms=START + 3_000, seq_no=0,
    )
    assert result.source_batch_id == "wire-batch"
    assert result.ticks[0].px_milli == 10000
    assert result.ticks[0].field_meta_map["px_milli"].quality is FieldQuality.PRESENT_VALUE
