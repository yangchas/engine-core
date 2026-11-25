import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import pytest

from engine_core import (
    AuctionAnchorFactV1,
    AuctionTimeline,
    AuctionTimingPolicyV1,
    FACT_ONLY,
    OBSERVING,
    PARTIAL,
    RECOVERY_APPLIED,
    RecoveryResultV1,
    READY,
    build_auction_anchor_fact_v1,
    local_datetime_ms,
    normalize_q2,
)


def _row(symbol, ts, **fields):
    return {"symbol": symbol, "ts": ts, **fields}


def test_standalone_0925_anchor_fact_does_not_depend_on_prior_anchors():
    evaluation_ms = local_datetime_ms("2026-09-18", "09:25:06")
    fact = build_auction_anchor_fact_v1(
        trade_date="2026-09-18",
        tag="0925",
        symbol="600519",
        row={
            "symbol": "600519",
            "price_milli": 12_300,
            "auction_anchor_0925_price_milli": 12_250,
            "source_record_time_ms": evaluation_ms - 1_000,
        },
        evaluation_time_ms=evaluation_ms,
        freeze_time_ms=evaluation_ms,
        source_layer="t1_v2_q2frame_event_time_replay",
    )

    assert isinstance(fact, AuctionAnchorFactV1)
    assert fact.status == "AVAILABLE"
    assert fact.price_milli == 12_250
    assert fact.source_time_ms == evaluation_ms - 1_000
    assert fact.business_anchor_ms == local_datetime_ms("2026-09-18", "09:25:00")
    assert fact.historical_available_at_status == "UNKNOWN"
    assert fact.content_hash and fact.evidence_hash


def test_missing_0925_anchor_is_not_filled_from_latest_quote_price():
    evaluation_ms = local_datetime_ms("2026-09-18", "09:25:06")
    fact = build_auction_anchor_fact_v1(
        trade_date="2026-09-18",
        tag="0925",
        symbol="600519",
        row={
            "symbol": "600519",
            "price_milli": 12_300,
            "auction_anchor_0925_price_milli": None,
            "raw_fields": {"a25": 0},
        },
        evaluation_time_ms=evaluation_ms,
        freeze_time_ms=evaluation_ms,
        source_layer="t1_v2_q2frame_event_time_replay",
    )

    assert fact.status == "MISSING"
    assert fact.price_milli is None
    assert fact.reason_code == "ANCHOR_ZERO_UNAVAILABLE"


def test_absent_raw_0925_field_is_unknown_not_confirmed_missing():
    evaluation_ms = local_datetime_ms("2026-09-18", "09:25:06")
    fact = build_auction_anchor_fact_v1(
        trade_date="2026-09-18",
        tag="0925",
        symbol="600519",
        row={"symbol": "600519", "auction_anchor_0925_price_milli": None},
        evaluation_time_ms=evaluation_ms,
        freeze_time_ms=evaluation_ms,
        source_layer="t1_v2_q2frame_event_time_replay",
    )

    assert fact.status == "UNKNOWN"
    assert fact.price_milli is None
    assert fact.reason_code == "ANCHOR_RAW_FIELD_NOT_PRESENT"


def test_default_auction_policy_has_adaptive_0925_grace():
    policy = AuctionTimingPolicyV1.default("0925")
    times = policy.at("2026-09-18")
    assert times["first_observable_ms"] == local_datetime_ms("2026-09-18", "09:25:06")
    assert times["preferred_finalize_ms"] == local_datetime_ms("2026-09-18", "09:25:10")
    assert times["soft_deadline_ms"] == local_datetime_ms("2026-09-18", "09:25:30")


def test_observe_isolates_one_malformed_row_and_keeps_valid_anchor_facts():
    timeline = AuctionTimeline("2026-09-18")
    evaluation_ms = local_datetime_ms("2026-09-18", "09:25:10")

    revision = timeline.observe(
        "0925",
        [
            _row(
                "600519",
                evaluation_ms - 1_000,
                auction_anchor_0925_price_milli=10_000,
            ),
            "malformed source member",
        ],
        evaluation_time_ms=evaluation_ms,
        expected_symbols=("600519", "000001"),
    )

    assert revision.state == PARTIAL
    assert revision.available_anchor_symbols == ("600519",)
    assert revision.missing_anchor_symbols == ("000001",)
    assert revision.source_anomaly_count == 1
    assert revision.source_anomaly_codes == ("ROW_NOT_MAPPING",)
    assert revision.invalid_source_symbols == ()
    bundle = timeline.build_analysis_bundle("0925")
    assert bundle["source_anomalies"] == {
        "count": 1,
        "codes": ("ROW_NOT_MAPPING",),
        "invalid_symbols": (),
    }


def test_clean_anchor_bundle_keeps_legacy_shape_without_anomaly_diagnostics():
    timeline = AuctionTimeline("2026-09-18")
    evaluation_ms = local_datetime_ms("2026-09-18", "09:25:10")
    timeline.observe(
        "0925",
        {
            "600519": {
                "auction_anchor_0925_price_milli": 10_000,
                "source_record_time_ms": evaluation_ms,
            }
        },
        evaluation_time_ms=evaluation_ms,
        expected_symbols=("600519",),
    )

    bundle = timeline.build_analysis_bundle("0925")

    assert "source_anomalies" not in bundle


def test_invalid_expected_symbol_keeps_real_anchor_fact_but_drops_coverage_claim():
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/q2/q2frame_0925_real_limit_states_20260930.json"
        ).read_text(encoding="utf-8")
    )
    assert fixture["q2frame_sha256"] == (
        "10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0"
    )
    trade_date = fixture["trade_date"]
    raw = fixture["records"][0]["q2_update"]
    symbol = raw["symbol"]
    row = normalize_q2(
        symbol,
        {key: value for key, value in raw.items() if key != "symbol"},
    ).to_mapping()
    evaluation_ms = local_datetime_ms(trade_date, "09:25:10")

    timeline = AuctionTimeline(trade_date)
    revision = timeline.observe(
        "0925",
        {symbol: row},
        evaluation_time_ms=evaluation_ms,
        expected_symbols=(symbol, "BAD"),
        source_layers=("PINNED_REAL_Q2FRAME",),
    )
    bundle = timeline.build_analysis_bundle("0925")

    assert revision.state == PARTIAL
    assert revision.expected_symbols == (symbol,)
    assert revision.available_anchor_symbols == (symbol,)
    assert revision.missing_anchor_symbols == ()
    assert revision.anchor_coverage is None
    assert revision.source_coverage is None
    assert revision.source_anomaly_codes == ("INVALID_EXPECTED_SYMBOL",)
    assert bundle["fact_status"] == FACT_ONLY
    assert bundle["anchor_coverage"] is None


def test_all_invalid_expected_symbols_leave_universe_unknown_and_recovery_available():
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/q2/q2frame_0925_real_limit_states_20260930.json"
        ).read_text(encoding="utf-8")
    )
    raw = fixture["records"][0]["q2_update"]
    symbol = raw["symbol"]
    row = normalize_q2(
        symbol,
        {key: value for key, value in raw.items() if key != "symbol"},
    ).to_mapping()
    evaluation_ms = local_datetime_ms(fixture["trade_date"], "09:25:10")

    timeline = AuctionTimeline(fixture["trade_date"])
    revision = timeline.observe(
        "0925",
        {symbol: row},
        evaluation_time_ms=evaluation_ms,
        expected_symbols=("BAD",),
        source_layers=("PINNED_REAL_Q2FRAME",),
    )
    bundle = timeline.build_analysis_bundle("0925")

    assert revision.state == PARTIAL
    assert revision.expected_symbols == ()
    assert revision.available_anchor_symbols == (symbol,)
    assert revision.anchor_coverage is None
    assert revision.source_coverage is None
    assert revision.source_anomaly_codes == ("INVALID_EXPECTED_SYMBOL",)
    assert bundle["recovery_required"] is True
    assert bundle["recovery_plan"].requested_symbols == ()


def test_observe_discards_only_ambiguous_duplicate_symbol_rows():
    timeline = AuctionTimeline("2026-09-18")
    evaluation_ms = local_datetime_ms("2026-09-18", "09:25:10")

    revision = timeline.observe(
        "0925",
        [
            _row("600519", evaluation_ms, auction_anchor_0925_price_milli=10_000),
            _row("600519", evaluation_ms + 1, auction_anchor_0925_price_milli=10_100),
            _row("000001", evaluation_ms, auction_anchor_0925_price_milli=20_000),
        ],
        evaluation_time_ms=evaluation_ms,
        expected_symbols=("600519", "000001"),
    )

    assert revision.state == PARTIAL
    assert revision.available_anchor_symbols == ("000001",)
    assert revision.missing_anchor_symbols == ("600519",)
    assert revision.invalid_source_symbols == ("600519",)
    assert revision.source_anomaly_count == 1
    assert revision.source_anomaly_codes == ("DUPLICATE_NORMALIZED_SYMBOL",)

    reversed_timeline = AuctionTimeline("2026-09-18")
    reversed_revision = reversed_timeline.observe(
        "0925",
        [
            _row("000001", evaluation_ms, auction_anchor_0925_price_milli=20_000),
            _row("600519", evaluation_ms + 1, auction_anchor_0925_price_milli=10_100),
            _row("600519", evaluation_ms, auction_anchor_0925_price_milli=10_000),
        ],
        evaluation_time_ms=evaluation_ms,
        expected_symbols=("600519", "000001"),
    )
    assert reversed_revision.content_hash == revision.content_hash


def test_observe_isolates_non_mapping_symbol_value_without_dropping_siblings():
    timeline = AuctionTimeline("2026-09-18")
    evaluation_ms = local_datetime_ms("2026-09-18", "09:25:10")

    revision = timeline.observe(
        "0925",
        {
            "600519": None,
            "000001": {
                "auction_anchor_0925_price_milli": 20_000,
                "source_record_time_ms": evaluation_ms,
            },
        },
        evaluation_time_ms=evaluation_ms,
        expected_symbols=("600519", "000001"),
    )

    assert revision.state == PARTIAL
    assert revision.available_anchor_symbols == ("000001",)
    assert revision.missing_anchor_symbols == ("600519",)
    assert revision.invalid_source_symbols == ("600519",)
    assert revision.source_anomaly_count == 1
    assert revision.source_anomaly_codes == ("ROW_NOT_MAPPING",)


def test_invalid_timestamp_only_degrades_time_bounds_not_valid_anchor_facts():
    timeline = AuctionTimeline("2026-09-18")
    evaluation_ms = local_datetime_ms("2026-09-18", "09:25:10")

    revision = timeline.observe(
        "0925",
        {
            "600519": {
                "auction_anchor_0925_price_milli": 10_000,
                "source_record_time_ms": "bad timestamp",
            },
            "000001": {
                "auction_anchor_0925_price_milli": 20_000,
                "source_record_time_ms": evaluation_ms,
            },
        },
        evaluation_time_ms=evaluation_ms,
        expected_symbols=("600519", "000001"),
    )

    assert revision.state == READY
    assert revision.available_anchor_symbols == ("000001", "600519")
    assert revision.source_time_min_ms == evaluation_ms
    assert revision.source_time_max_ms == evaluation_ms
    assert revision.invalid_source_symbols == ()
    assert revision.source_anomaly_count == 1
    assert revision.source_anomaly_codes == ("INVALID_SOURCE_TIME",)


@pytest.mark.parametrize(
    "times",
    [
        ("09:20:00", "09:20:0X", "09:20:1X", "09:20:3X"),
        ("09:20:00", "09:99:00", "09:99:01", "09:99:03"),
        ("09:20:00", "24:00:00", "24:00:01", "24:00:03"),
    ],
)
def test_auction_policy_rejects_invalid_clock_values(times):
    with pytest.raises(ValueError, match="HH:MM:SS"):
        AuctionTimingPolicyV1("0920", *times)


def test_anchor_fact_truncates_subseconds_for_preferred_finalize_status():
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/q2/q2frame_0925_real_limit_states_20260930.json"
        ).read_text(encoding="utf-8")
    )
    assert fixture["q2frame_sha256"] == (
        "10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0"
    )
    record = next(
        item
        for item in fixture["records"]
        if item["seq_no"] == 601
        and item["q2_update"]["symbol"] == "000560"
    )
    row = normalize_q2("000560", record["q2_update"]).to_mapping()
    trade_date = fixture["trade_date"]
    preferred_finalize_ms = local_datetime_ms(trade_date, "09:25:10")
    assert row["auction_anchor_0925_price_milli"] == 3_380
    assert row["source_record_time_ms"] == local_datetime_ms(trade_date, "09:25:00")

    within_same_second = build_auction_anchor_fact_v1(
        trade_date=trade_date,
        tag="0925",
        symbol="000560",
        row=row,
        evaluation_time_ms=preferred_finalize_ms + 197,
        source_layer="real_q2frame_event_time_replay",
    )
    following_second = build_auction_anchor_fact_v1(
        trade_date=trade_date,
        tag="0925",
        symbol="000560",
        row=row,
        evaluation_time_ms=preferred_finalize_ms + 1_000,
        source_layer="real_q2frame_event_time_replay",
    )

    assert within_same_second.status == "AVAILABLE"
    assert within_same_second.late_execution is False
    assert following_second.late_execution is True


def test_revision_truncates_subseconds_for_soft_deadline_lateness():
    soft_deadline_ms = local_datetime_ms("2026-09-18", "09:25:30")
    timeline = AuctionTimeline("2026-09-18")
    unavailable_row = _row(
        "600519",
        soft_deadline_ms,
        auction_anchor_0925_price_milli=0,
    )

    at_deadline_second = timeline.observe(
        "0925",
        [unavailable_row],
        evaluation_time_ms=soft_deadline_ms + 999,
        expected_symbols=("600519",),
    )
    after_deadline_second = timeline.observe(
        "0925",
        [unavailable_row],
        evaluation_time_ms=soft_deadline_ms + 1_000,
        expected_symbols=("600519",),
    )

    assert at_deadline_second.state == "MISSING"
    assert at_deadline_second.late_execution is False
    assert after_deadline_second.late_execution is True


def test_auction_timeline_accepts_0925_without_optional_prior_anchors():
    timeline = AuctionTimeline("2026-09-18")
    before = local_datetime_ms("2026-09-18", "09:25:05")
    observing = timeline.observe("0925", {}, evaluation_time_ms=before, expected_symbols=("600519",))
    assert observing.state == OBSERVING
    at_first = local_datetime_ms("2026-09-18", "09:25:06")
    partial = timeline.observe(
        "0925",
        [_row("600519", at_first, auction_anchor_0925_price_milli=10000)],
        evaluation_time_ms=at_first,
        expected_symbols=("600519", "000001"),
    )
    assert partial.state == PARTIAL
    assert partial.freeze_time_ms == at_first
    bundle = timeline.build_analysis_bundle("0925")
    assert bundle["fact_status"] == FACT_ONLY
    assert bundle["prior_deltas"] == {"0920": "UNKNOWN", "0924": "UNKNOWN"}
    assert bundle["recovery_plan"] is not None
    assert bundle["recovery_plan"].recovery_state == "REQUESTED"
    assert bundle["recovery_plan"].missing_fields == ("auction_anchor_0925_price_milli",)


def test_all_source_rows_do_not_make_missing_0925_anchor_ready():
    timeline = AuctionTimeline("2026-09-18")
    at_first = local_datetime_ms("2026-09-18", "09:25:06")
    revision = timeline.observe(
        "0925",
        [
            _row("600519", at_first, auction_anchor_0925_price_milli=10000),
            _row("000001", at_first, auction_anchor_0925_price_milli=0),
        ],
        evaluation_time_ms=at_first,
        expected_symbols=("600519", "000001"),
    )

    assert revision.state == PARTIAL
    assert revision.observed_symbols == ("600519",)
    assert revision.missing_symbols == ("000001",)
    assert revision.source_observed_symbols == ("000001", "600519")
    assert revision.source_missing_symbols == ()
    assert revision.coverage == 0.5
    assert revision.source_coverage == 1.0
    bundle = timeline.build_analysis_bundle("0925")
    assert bundle["recovery_required"] is True
    assert bundle["recovery_plan"].requested_symbols == ("000001",)
    assert bundle["recovery_plan"].missing_fields == ("auction_anchor_0925_price_milli",)


def test_0925_can_be_ready_without_optional_0920_or_0924_anchors():
    timeline = AuctionTimeline("2026-09-18")
    at_first = local_datetime_ms("2026-09-18", "09:25:06")
    revision = timeline.observe(
        "0925",
        [_row("600519", at_first, auction_anchor_0925_price_milli=10000)],
        evaluation_time_ms=at_first,
        expected_symbols=("600519",),
    )

    assert revision.state == READY
    assert revision.anchor_coverage == 1.0
    assert timeline.build_analysis_bundle("0925")["prior_deltas"] == {
        "0920": "UNKNOWN",
        "0924": "UNKNOWN",
    }


def test_missing_optional_prior_anchors_do_not_schedule_recovery():
    timeline = AuctionTimeline("2026-09-18")
    for tag, field, clock in (
        ("0920", "auction_anchor_0920_price_milli", "09:20:03"),
        ("0924", "auction_anchor_0924_price_milli", "09:24:10"),
    ):
        at_first = local_datetime_ms("2026-09-18", clock)
        revision = timeline.observe(
            tag,
            [_row("600519", at_first, **{field: 0})],
            evaluation_time_ms=at_first,
            expected_symbols=("600519",),
        )
        bundle = timeline.build_analysis_bundle(tag)
        assert revision.state == PARTIAL
        assert bundle["recovery_required"] is False
        assert bundle["recovery_plan"] is None

    at_0925 = local_datetime_ms("2026-09-18", "09:25:06")
    final_anchor = timeline.observe(
        "0925",
        [_row("600519", at_0925, auction_anchor_0925_price_milli=10000)],
        evaluation_time_ms=at_0925,
        expected_symbols=("600519",),
    )
    assert final_anchor.state == READY
    assert timeline.build_analysis_bundle("0925")["prior_deltas"] == {
        "0920": "UNKNOWN",
        "0924": "UNKNOWN",
    }


def test_late_correction_is_a_new_revision_and_same_hash_is_idempotent():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    first = timeline.observe("0925", [_row("600519", t, auction_anchor_0925_price_milli=10000)], evaluation_time_ms=t, expected_symbols=("600519", "000001"), source_layers=("REDIS",))
    same = timeline.observe("0925", [_row("600519", t, auction_anchor_0925_price_milli=10000)], evaluation_time_ms=t + 1_000, expected_symbols=("600519", "000001"), source_layers=("REDIS",))
    corrected = timeline.observe("0925", [_row("600519", t, auction_anchor_0925_price_milli=10000), _row("000001", t + 1, auction_anchor_0925_price_milli=20000)], evaluation_time_ms=t + 21_000, expected_symbols=("600519", "000001"), source_layers=("REDIS", "WENCAI"))
    assert same.revision == first.revision
    assert corrected.revision == first.revision + 1
    assert corrected.supersedes_revision == first.revision
    assert len(timeline.revisions("0925")) == 2
    assert corrected.state == READY
    assert corrected.late_execution is True

    changed_value = timeline.observe("0925", [_row("600519", t, auction_anchor_0925_price_milli=10100)], evaluation_time_ms=t + 22_000, expected_symbols=("600519", "000001"))
    assert changed_value.revision == corrected.revision + 1


def test_real_q2_anchor_observation_update_creates_immutable_superseding_revision():
    """Real captures reproduce immutable revision summaries without row history.

    This verifies summary/hash retention against externally retained source
    captures; it does not claim that AuctionTimeline exposes per-revision rows.
    """
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/q2/real_q2_anchor_observation_update_20261009.json"
        ).read_text(encoding="utf-8")
    )
    canonical_rows = json.dumps(
        {
            "before_rows": fixture["before_rows"],
            "after_rows": fixture["after_rows"],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert sha256(canonical_rows).hexdigest() == fixture["source_evidence"][
        "rows_canonical_sha256"
    ]

    groups = fixture["symbol_groups"]
    expected_symbols = tuple(
        sorted(symbol for symbols in groups.values() for symbol in symbols)
    )
    assert len(expected_symbols) == 20
    assert len(set(expected_symbols)) == len(expected_symbols)

    def epoch_ms(timestamp: str) -> int:
        return int(datetime.fromisoformat(timestamp).timestamp() * 1_000)

    def normalized_rows(rows):
        return [
            normalize_q2(
                row["symbol"], {"a24": row["a24"], "ts": row["ts"]}
            ).to_mapping()
            for row in rows
        ]

    before_rows = normalized_rows(fixture["before_rows"])
    after_rows = normalized_rows(fixture["after_rows"])
    before_by_symbol = {row["symbol"]: row for row in before_rows}
    after_by_symbol = {row["symbol"]: row for row in after_rows}
    anchor_field = fixture["canonical_field"]

    filled = set(groups["filled_from_zero"])
    rewritten = set(groups["rewritten_positive"])
    stable = set(groups["stable_positive"])
    remained_zero = set(groups["remained_zero"])

    assert set(before_by_symbol) == set(after_by_symbol) == set(expected_symbols)
    assert set(row["symbol"] for row in fixture["before_rows"]) == set(expected_symbols)
    assert set(row["symbol"] for row in fixture["after_rows"]) == set(expected_symbols)
    for symbol in filled | remained_zero:
        assert before_by_symbol[symbol][anchor_field] is None
        assert before_by_symbol[symbol]["auction_anchor_field_quality"]["a24"] == "MISSING"
    for symbol in filled:
        assert after_by_symbol[symbol][anchor_field] > 0
    for symbol in rewritten:
        assert before_by_symbol[symbol][anchor_field] > 0
        assert after_by_symbol[symbol][anchor_field] > 0
        assert before_by_symbol[symbol][anchor_field] != after_by_symbol[symbol][anchor_field]
    for symbol in stable:
        assert before_by_symbol[symbol][anchor_field] == after_by_symbol[symbol][anchor_field] > 0
    for symbol in remained_zero:
        assert after_by_symbol[symbol][anchor_field] is None
        assert after_by_symbol[symbol]["auction_anchor_field_quality"]["a24"] == "MISSING"

    before_meta = fixture["source_evidence"]["before_capture"]
    after_meta = fixture["source_evidence"]["after_capture"]
    before_observed_ms = epoch_ms(before_meta["read_completed_at"])
    after_observed_ms = epoch_ms(after_meta["read_completed_at"])
    timeline = AuctionTimeline(fixture["trade_date"])
    first_revision = timeline.observe(
        fixture["tag"],
        before_rows,
        evaluation_time_ms=before_observed_ms,
        observed_at_ms=before_observed_ms,
        expected_symbols=expected_symbols,
        source_layers=(fixture["source_layer"],),
    )
    preserved_first_revision = timeline.revisions(fixture["tag"])[0]

    second_revision = timeline.observe(
        fixture["tag"],
        after_rows,
        evaluation_time_ms=after_observed_ms,
        observed_at_ms=after_observed_ms,
        expected_symbols=expected_symbols,
        source_layers=(fixture["source_layer"],),
    )
    history = timeline.revisions(fixture["tag"])

    assert first_revision.state == PARTIAL
    assert first_revision.late_execution is False
    assert set(first_revision.available_anchor_symbols) == rewritten | stable
    assert set(first_revision.missing_anchor_symbols) == filled | remained_zero
    assert second_revision.state == PARTIAL
    assert second_revision.late_execution is True
    assert second_revision.revision == first_revision.revision + 1
    assert second_revision.supersedes_revision == first_revision.revision
    assert second_revision.freeze_time_ms == first_revision.freeze_time_ms
    assert second_revision.content_hash != first_revision.content_hash
    assert second_revision.observations_hash != first_revision.observations_hash
    assert set(second_revision.available_anchor_symbols) == filled | rewritten | stable
    assert set(second_revision.missing_anchor_symbols) == remained_zero
    assert len(history) == 2
    assert history[0] == preserved_first_revision == first_revision
    assert history[0].content_hash == first_revision.content_hash
    assert history[0].observations_hash == first_revision.observations_hash

    # Replaying either source capture independently must reproduce that
    # revision's observation hash. The external captures provide the row
    # evidence; the timeline intentionally exposes immutable revision
    # summaries rather than a per-revision symbol-value accessor.
    before_only = AuctionTimeline(fixture["trade_date"]).observe(
        fixture["tag"],
        before_rows,
        evaluation_time_ms=before_observed_ms,
        observed_at_ms=before_observed_ms,
        expected_symbols=expected_symbols,
        source_layers=(fixture["source_layer"],),
    )
    after_only = AuctionTimeline(fixture["trade_date"]).observe(
        fixture["tag"],
        after_rows,
        evaluation_time_ms=after_observed_ms,
        observed_at_ms=after_observed_ms,
        expected_symbols=expected_symbols,
        source_layers=(fixture["source_layer"],),
    )
    assert before_only.observations_hash == first_revision.observations_hash
    assert after_only.observations_hash == second_revision.observations_hash


def test_identical_cohort_advances_soft_cutoff_timing_without_new_revision():
    timeline = AuctionTimeline("2026-09-18")
    before = local_datetime_ms("2026-09-18", "09:25:05")
    after = local_datetime_ms("2026-09-18", "09:25:10")
    rows = [_row("600519", after, auction_anchor_0925_price_milli=10000)]
    observing = timeline.observe(
        "0925",
        rows,
        evaluation_time_ms=before,
        expected_symbols=("600519", "000001"),
    )
    finalized = timeline.observe(
        "0925",
        rows,
        evaluation_time_ms=after,
        expected_symbols=("600519", "000001"),
    )
    assert observing.revision == finalized.revision == 1
    assert observing.state == OBSERVING
    assert finalized.state == PARTIAL
    assert finalized.evaluation_time_ms == after
    assert len(timeline.revisions("0925")) == 1
    assert timeline.latest("0925") == finalized


def test_non_anchor_q2_updates_do_not_create_an_auction_anchor_revision():
    timeline = AuctionTimeline("2026-09-18")
    first_time = local_datetime_ms("2026-09-18", "09:25:06")
    first = timeline.observe(
        "0925",
        [
            _row(
                "600519",
                first_time,
                auction_anchor_0925_price_milli=10000,
                price_milli=10000,
                amount_2m_yuan=1000,
            )
        ],
        evaluation_time_ms=first_time,
        expected_symbols=("600519",),
    )

    later_time = local_datetime_ms("2026-09-18", "09:25:10")
    same_anchor = timeline.observe(
        "0925",
        [
            _row(
                "600519",
                first_time,
                auction_anchor_0925_price_milli=10000,
                price_milli=10100,
                amount_2m_yuan=1200,
            )
        ],
        evaluation_time_ms=later_time,
        expected_symbols=("600519",),
    )

    assert same_anchor.revision == first.revision
    assert same_anchor.content_hash == first.content_hash
    assert same_anchor.evidence_hash != first.evidence_hash
    assert len(timeline.revisions("0925")) == 1


def test_anchor_quality_change_is_revision_content_even_if_anchor_is_unavailable():
    timeline = AuctionTimeline("2026-09-18")
    at_first = local_datetime_ms("2026-09-18", "09:25:06")
    missing = timeline.observe(
        "0925",
        [
            _row(
                "600519",
                at_first,
                auction_anchor_0925_price_milli=None,
                auction_anchor_field_quality={"a25": "MISSING"},
            )
        ],
        evaluation_time_ms=at_first,
        expected_symbols=("600519",),
    )
    unknown = timeline.observe(
        "0925",
        [
            _row(
                "600519",
                at_first,
                auction_anchor_0925_price_milli=None,
                auction_anchor_field_quality={"a25": "UNKNOWN"},
            )
        ],
        evaluation_time_ms=local_datetime_ms("2026-09-18", "09:25:10"),
        expected_symbols=("600519",),
    )

    assert missing.anchor_coverage == unknown.anchor_coverage == 0.0
    assert unknown.revision == missing.revision + 1
    assert unknown.supersedes_revision == missing.revision
    assert len(timeline.revisions("0925")) == 2


def test_recovery_cohort_is_idempotent_and_does_not_promote_fact_status():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    first = timeline.observe(
        "0925",
        [_row("600519", t, auction_anchor_0925_price_milli=10000)],
        evaluation_time_ms=t,
        expected_symbols=("600519", "000001"),
        source_layers=("t1_v2_q2frame_event_time_replay",),
    )
    bundle = timeline.build_analysis_bundle("0925")
    recovered = timeline.apply_recovery(
        bundle["recovery_plan"],
        [_row("600519", t, auction_anchor_0925_price_milli=10000), _row("000001", t + 1, auction_anchor_0925_price_milli=20000)],
        evaluation_time_ms=t + 1_000,
    )
    repeated = timeline.apply_recovery(
        bundle["recovery_plan"],
        [_row("600519", t, auction_anchor_0925_price_milli=10000), _row("000001", t + 1, auction_anchor_0925_price_milli=20000)],
        evaluation_time_ms=t + 2_000,
    )
    assert recovered.revision == first.revision + 1
    assert repeated.revision == recovered.revision
    assert timeline.build_analysis_bundle("0925")["fact_status"] == FACT_ONLY
    assert recovered.state == PARTIAL
    assert recovered.anchor_coverage == 1.0
    assert recovered.recovery_state == "APPLIED"
    assert recovered.source_layers == (
        "t1_v2_q2frame_event_time_replay",
        "wencai",
    )
    assert timeline.build_analysis_bundle("0925")["recovery_required"] is False
    assert timeline.build_analysis_bundle("0925")["recovery_plan"] is None


def test_direct_recovery_path_also_preserves_observed_primary_fields():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    timeline.observe(
        "0925",
        {
            "600519": {
                "auction_anchor_0925_price_milli": 10000,
                "source_time_ms": t,
                "auction_amount_yuan": 123,
            },
            "000001": {
                "auction_anchor_0925_price_milli": 0,
                "source_time_ms": t,
                "auction_amount_yuan": 0,
            },
        },
        evaluation_time_ms=t,
        expected_symbols=("600519", "000001"),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]

    applied = timeline.apply_recovery(
        plan,
        {
            "600519": {
                "auction_anchor_0925_price_milli": 10000,
                "source_time_ms": t,
                "auction_amount_yuan": 999999,
            },
            "000001": {
                "auction_anchor_0925_price_milli": 10100,
                "source_time_ms": t,
                "auction_amount_yuan": 0,
            },
        },
        evaluation_time_ms=t + 1000,
    )

    assert applied.available_anchor_symbols == ("000001", "600519")
    assert applied.invalid_source_symbols == ("600519",)
    assert "RECOVERY_CONFLICTING_SOURCE_ROW_QUARANTINED" in applied.source_anomaly_codes
    assert timeline._source_rows["0925"]["600519"]["auction_amount_yuan"] == 123


def test_partial_recovery_requests_only_remaining_missing_symbols():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    first = timeline.observe(
        "0925",
        [
            _row("600519", t, auction_anchor_0925_price_milli=10000),
            _row("000001", t, auction_anchor_0925_price_milli=0),
            _row("000002", t, auction_anchor_0925_price_milli=0),
        ],
        evaluation_time_ms=t,
        expected_symbols=("600519", "000001", "000002"),
        source_layers=("t1_v2_q2frame_event_time_replay",),
    )
    first_plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    assert first.state == PARTIAL
    assert first_plan.requested_symbols == ("000001", "000002")

    recovered = timeline.apply_recovery(
        first_plan,
        [
            _row("600519", t, auction_anchor_0925_price_milli=10000),
            _row("000001", t, auction_anchor_0925_price_milli=10100),
            _row("000002", t, auction_anchor_0925_price_milli=0),
        ],
        evaluation_time_ms=t + 1_000,
    )
    next_bundle = timeline.build_analysis_bundle("0925")
    next_plan = next_bundle["recovery_plan"]

    assert recovered.revision == first.revision + 1
    assert recovered.state == PARTIAL
    assert recovered.available_anchor_symbols == ("000001", "600519")
    assert recovered.missing_anchor_symbols == ("000002",)
    assert recovered.source_layers == (
        "t1_v2_q2frame_event_time_replay",
        "wencai",
    )
    assert next_bundle["recovery_required"] is True
    assert next_plan.requested_symbols == ("000002",)
    assert next_plan.current_revision == recovered.revision
    assert next_plan.idempotency_key != first_plan.idempotency_key


def test_apply_recovery_result_binds_plan_and_applies_merged_cohort_idempotently():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    first = timeline.observe(
        "0925",
        {
            "600519": {"auction_anchor_0925_price_milli": 10000, "source_time_ms": t},
            "000001": {"auction_anchor_0925_price_milli": 0, "source_time_ms": t},
        },
        evaluation_time_ms=t,
        expected_symbols=("600519", "000001"),
        source_layers=("t1_v2_q2frame_event_time_replay",),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    assert first.revision == plan.current_revision

    anchor_field = "auction_anchor_0925_price_milli"
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1_000,
        available_at_ms=None,
        # This case supplies the full merged cohort. Sparse responses are also
        # accepted and completed from the saved primary cohort by the timeline.
        rows={
            "600519": {anchor_field: 10000, "source_time_ms": t},
            "000001": {anchor_field: 10100, "source_time_ms": t},
        },
        filled_symbols=("000001",),
        filled_fields=(anchor_field,),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2_000
    )
    repeated = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 3_000
    )

    assert applied.revision == first.revision + 1
    assert applied.state == PARTIAL
    assert applied.available_anchor_symbols == ("000001", "600519")
    assert applied.source_layers == (
        "t1_v2_q2frame_event_time_replay",
        "wencai",
    )
    assert repeated.revision == applied.revision
    assert len(timeline.revisions("0925")) == 2


def test_apply_recovery_result_keeps_good_symbols_when_one_recovery_symbol_conflicts():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    anchor_field = "auction_anchor_0925_price_milli"
    first = timeline.observe(
        "0925",
        {
            "SH.600519": {anchor_field: 0, "source_time_ms": t},
            "000001": {anchor_field: 0, "source_time_ms": t},
        },
        evaluation_time_ms=t,
        expected_symbols=("SH.600519", "000001"),
        source_layers=("t1_v2_q2frame_event_time_replay",),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1_000,
        available_at_ms=None,
        rows={
            "SH.600519": {anchor_field: 10_000, "source_time_ms": t},
            "sh.600519": {anchor_field: 10_100, "source_time_ms": t},
            "000001": {anchor_field: 20_000, "source_time_ms": t},
        },
        filled_symbols=("SH.600519", "000001"),
        filled_fields=(anchor_field,),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2_000
    )
    repeated = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 3_000
    )

    assert first.state == PARTIAL
    assert applied.state == PARTIAL
    assert repeated.revision == applied.revision
    assert len(timeline.revisions("0925")) == 2
    assert applied.available_anchor_symbols == ("000001",)
    assert applied.missing_anchor_symbols == ("SH.600519",)
    assert applied.invalid_source_symbols == ("SH.600519",)
    assert applied.source_anomaly_codes == ("DUPLICATE_NORMALIZED_SYMBOL",)
    assert applied.source_layers == (
        "t1_v2_q2frame_event_time_replay",
        "wencai",
    )


def test_apply_recovery_iterable_quarantines_conflicting_symbol_and_keeps_siblings():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    anchor_field = "auction_anchor_0925_price_milli"
    first = timeline.observe(
        "0925",
        {
            "SH.600519": {anchor_field: 0, "source_time_ms": t},
            "000001": {anchor_field: 0, "source_time_ms": t},
        },
        evaluation_time_ms=t,
        expected_symbols=("SH.600519", "000001"),
        source_layers=("t1_v2_q2frame_event_time_replay",),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    rows = [
        {"symbol": "SH.600519", anchor_field: 10_000, "source_time_ms": t},
        {"symbol": "sh.600519", anchor_field: 10_100, "source_time_ms": t},
        {"symbol": "000001", anchor_field: 20_000, "source_time_ms": t},
    ]

    applied = timeline.apply_recovery(
        plan, rows, evaluation_time_ms=t + 2_000, source="wencai"
    )

    assert first.state == PARTIAL
    assert applied.state == PARTIAL
    assert applied.available_anchor_symbols == ("000001",)
    assert applied.missing_anchor_symbols == ("SH.600519",)
    assert applied.invalid_source_symbols == ("SH.600519",)
    assert applied.source_anomaly_codes == ("DUPLICATE_NORMALIZED_SYMBOL",)
    assert len(timeline.revisions("0925")) == 2


def test_apply_recovery_quarantines_embedded_symbol_mismatch_and_keeps_sibling():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    anchor_field = "auction_anchor_0925_price_milli"
    timeline.observe(
        "0925",
        {
            "600519": {anchor_field: 10_000, "source_time_ms": t},
            "000001": {anchor_field: 0, "source_time_ms": t},
            "000002": {anchor_field: 0, "source_time_ms": t},
        },
        evaluation_time_ms=t,
        expected_symbols=("600519", "000001", "000002"),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]

    applied = timeline.apply_recovery(
        plan,
        {
            "600519": {anchor_field: 10_000, "source_time_ms": t},
            "000001": {
                "symbol": "000002",
                anchor_field: 20_000,
                "source_time_ms": t,
            },
            "000002": {
                "symbol": "000002",
                anchor_field: 30_000,
                "source_time_ms": t,
            },
        },
        evaluation_time_ms=t + 2_000,
    )

    assert applied.state == PARTIAL
    assert applied.available_anchor_symbols == ("000002", "600519")
    assert applied.missing_anchor_symbols == ("000001",)
    assert applied.invalid_source_symbols == ("000001",)
    assert "RECOVERY_EMBEDDED_SYMBOL_MISMATCH_QUARANTINED" in applied.source_anomaly_codes
    assert timeline._source_rows["0925"]["000001"][anchor_field] == 0
    assert timeline._source_rows["0925"]["000002"][anchor_field] == 30_000
    assert len(timeline.revisions("0925")) == 2


def test_apply_recovery_identity_mismatch_without_valid_sibling_stays_recoverable():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    anchor_field = "auction_anchor_0925_price_milli"
    timeline.observe(
        "0925",
        {"000001": {anchor_field: 0}},
        evaluation_time_ms=t,
        expected_symbols=("000001",),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]

    applied = timeline.apply_recovery(
        plan,
        {"000001": {"symbol": "000002", anchor_field: 20_000}},
        evaluation_time_ms=t + 2_000,
    )

    assert applied.state == PARTIAL
    assert applied.recovery_state == "ERROR"
    assert applied.available_anchor_symbols == ()
    assert applied.missing_anchor_symbols == ("000001",)
    assert applied.invalid_source_symbols == ("000001",)
    assert timeline.build_analysis_bundle("0925")["recovery_required"] is True
    assert timeline._source_rows["0925"]["000001"][anchor_field] == 0


def test_apply_recovery_result_keeps_valid_fill_when_one_declared_fill_is_zero_or_absent():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    anchor_field = "auction_anchor_0925_price_milli"
    first = timeline.observe(
        "0925",
        {
            "600519": {anchor_field: 0, "source_time_ms": t},
            "000001": {anchor_field: 0, "source_time_ms": t},
            "000002": {anchor_field: 0, "source_time_ms": t},
        },
        evaluation_time_ms=t,
        expected_symbols=("600519", "000001", "000002"),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1_000,
        available_at_ms=None,
        rows={
            "600519": {anchor_field: 0, "source_time_ms": t},
            "000001": {anchor_field: 20_000, "source_time_ms": t},
        },
        filled_symbols=("600519", "000001", "000002"),
        filled_fields=(anchor_field,),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2_000
    )

    assert first.state == PARTIAL
    assert result.filled_symbols == ("000001",)
    assert result.invalid_symbols == ("000002",)
    assert "FILLED_SYMBOL_NOT_RETURNED" in result.source_anomaly_codes
    assert "DECLARED_FILL_NOT_AVAILABLE" in result.source_anomaly_codes
    assert applied.state == PARTIAL
    assert applied.available_anchor_symbols == ("000001",)
    assert applied.missing_anchor_symbols == ("000002", "600519")
    assert len(timeline.revisions("0925")) == 2


def test_recovery_zero_for_missing_q2_anchor_does_not_block_valid_sibling_fill():
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/q2/q2frame_null_a25_live_rows_20260930.json"
        ).read_text(encoding="utf-8")
    )
    assert fixture["q2frame_sha256"] == (
        "10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0"
    )
    trade_date = fixture["trade_date"]
    t = local_datetime_ms(trade_date, "09:25:10")
    anchor_field = "auction_anchor_0925_price_milli"
    primary_rows = {
        raw["symbol"]: normalize_q2(
            raw["symbol"],
            {key: value for key, value in raw.items() if key != "symbol"},
        ).to_mapping()
        for raw in fixture["q2_updates"]
    }
    assert len(primary_rows) == 2
    unavailable_symbol, valid_symbol = sorted(primary_rows)
    assert all(row[anchor_field] is None for row in primary_rows.values())

    timeline = AuctionTimeline(trade_date)
    timeline.observe(
        "0925",
        primary_rows,
        evaluation_time_ms=t,
        expected_symbols=tuple(primary_rows) + ("000002",),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]

    recovery_rows = {symbol: dict(row) for symbol, row in primary_rows.items()}
    recovery_rows[unavailable_symbol][anchor_field] = 0
    recovery_rows[valid_symbol][anchor_field] = 20_000
    recovery_rows["000002"] = {"symbol": "000002", anchor_field: 0}
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1_000,
        available_at_ms=None,
        rows=recovery_rows,
        filled_symbols=(*primary_rows, "000002"),
        filled_fields=(anchor_field,),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2_000
    )
    repeated = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 3_000
    )

    assert result.filled_symbols == (valid_symbol,)
    assert "DECLARED_FILL_NOT_AVAILABLE" in result.source_anomaly_codes
    assert applied.state == PARTIAL
    assert applied.available_anchor_symbols == (valid_symbol,)
    assert applied.missing_anchor_symbols == tuple(sorted(("000002", unavailable_symbol)))
    assert repeated.revision == applied.revision
    assert repeated.content_hash == applied.content_hash
    assert len(timeline.revisions("0925")) == 2


def test_invalid_filled_symbol_declaration_does_not_block_real_primary_sibling():
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/q2/q2frame_null_a25_live_rows_20260930.json"
        ).read_text(encoding="utf-8")
    )
    assert fixture["q2frame_sha256"] == (
        "10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0"
    )
    trade_date = fixture["trade_date"]
    t = local_datetime_ms(trade_date, "09:25:10")
    anchor_field = "auction_anchor_0925_price_milli"
    primary_rows = {
        raw["symbol"]: normalize_q2(
            raw["symbol"],
            {key: value for key, value in raw.items() if key != "symbol"},
        ).to_mapping()
        for raw in fixture["q2_updates"]
    }
    unavailable_symbol, valid_symbol = sorted(primary_rows)
    assert all(row[anchor_field] is None for row in primary_rows.values())

    timeline = AuctionTimeline(trade_date)
    timeline.observe(
        "0925",
        primary_rows,
        evaluation_time_ms=t,
        expected_symbols=tuple(primary_rows),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1_000,
        available_at_ms=None,
        rows={
            "BAD": {anchor_field: 30_000},
            valid_symbol: {anchor_field: 20_000},
        },
        filled_symbols=("BAD", valid_symbol),
        filled_fields=(anchor_field,),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2_000
    )

    assert result.filled_symbols == (valid_symbol,)
    assert "INVALID_SYMBOL" in result.source_anomaly_codes
    assert "INVALID_FILLED_SYMBOL_DECLARATION" in result.source_anomaly_codes
    assert applied.state == PARTIAL
    assert applied.available_anchor_symbols == (valid_symbol,)
    assert applied.missing_anchor_symbols == (unavailable_symbol,)
    assert "BAD" not in timeline._source_rows["0925"]
    assert len(timeline.revisions("0925")) == 2


def test_invalid_only_filled_symbol_records_error_and_keeps_recovery_required():
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/q2/q2frame_null_a25_live_rows_20260930.json"
        ).read_text(encoding="utf-8")
    )
    assert fixture["q2frame_sha256"] == (
        "10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0"
    )
    trade_date = fixture["trade_date"]
    t = local_datetime_ms(trade_date, "09:25:10")
    anchor_field = "auction_anchor_0925_price_milli"
    primary_rows = {
        raw["symbol"]: normalize_q2(
            raw["symbol"],
            {key: value for key, value in raw.items() if key != "symbol"},
        ).to_mapping()
        for raw in fixture["q2_updates"]
    }
    assert all(row[anchor_field] is None for row in primary_rows.values())

    timeline = AuctionTimeline(trade_date)
    timeline.observe(
        "0925",
        primary_rows,
        evaluation_time_ms=t,
        expected_symbols=tuple(primary_rows),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1_000,
        available_at_ms=None,
        rows={"BAD": {anchor_field: 30_000}},
        filled_symbols=("BAD",),
        filled_fields=(anchor_field,),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2_000
    )

    assert result.filled_symbols == ()
    assert result.filled_fields == ()
    assert applied.recovery_state == "ERROR"
    assert applied.state == PARTIAL
    assert applied.available_anchor_symbols == ()
    assert timeline.build_analysis_bundle("0925")["recovery_required"] is True
    assert "INVALID_SYMBOL" in applied.source_anomaly_codes
    assert "INVALID_FILLED_SYMBOL_DECLARATION" in applied.source_anomaly_codes


def test_incomplete_recovery_cohort_restores_omitted_primary_rows_and_fields():
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/q2/q2frame_null_a25_live_rows_20260930.json"
        ).read_text(encoding="utf-8")
    )
    assert fixture["q2frame_sha256"] == (
        "10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0"
    )
    trade_date = fixture["trade_date"]
    t = local_datetime_ms(trade_date, "09:25:10")
    anchor_field = "auction_anchor_0925_price_milli"
    primary_rows = {
        raw["symbol"]: normalize_q2(
            raw["symbol"],
            {key: value for key, value in raw.items() if key != "symbol"},
        ).to_mapping()
        for raw in fixture["q2_updates"]
    }
    omitted_symbol, filled_symbol = sorted(primary_rows)
    timeline = AuctionTimeline(trade_date)
    timeline.observe(
        "0925",
        primary_rows,
        evaluation_time_ms=t,
        expected_symbols=tuple(primary_rows) + ("000002",),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]

    # This response omits one original member entirely and returns only the
    # recovered field for the valid sibling, not a fully merged snapshot.
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1_000,
        available_at_ms=None,
        rows={filled_symbol: {anchor_field: 20_000}},
        filled_symbols=(filled_symbol,),
        filled_fields=(anchor_field,),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2_000
    )
    repeated = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 3_000
    )

    assert applied.state == PARTIAL
    assert applied.available_anchor_symbols == (filled_symbol,)
    assert applied.missing_anchor_symbols == tuple(sorted((omitted_symbol, "000002")))
    assert set(applied.source_observed_symbols) == set(primary_rows)
    assert "RECOVERY_SOURCE_SYMBOL_RESTORED" in applied.source_anomaly_codes
    assert "RECOVERY_SOURCE_FIELD_RESTORED" in applied.source_anomaly_codes
    assert repeated.revision == applied.revision
    assert repeated.content_hash == applied.content_hash
    assert len(timeline.revisions("0925")) == 2


def test_full_missing_anchor_recovery_works_without_known_symbol_universe():
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/q2/q2frame_0925_real_limit_states_20260930.json"
        ).read_text(encoding="utf-8")
    )
    assert fixture["q2frame_sha256"] == (
        "10264d0a6b6251e0c757f2113fd41e8e9e0669fade4ba05a145b78340a886ec0"
    )
    record = next(
        item
        for item in fixture["records"]
        if item["seq_no"] == 601
        and item["q2_update"]["symbol"] == "000560"
    )
    recovered_row = normalize_q2("000560", record["q2_update"]).to_mapping()
    anchor_field = "auction_anchor_0925_price_milli"
    anchor_price = recovered_row[anchor_field]
    assert type(anchor_price) is int and anchor_price > 0

    trade_date = fixture["trade_date"]
    timeline = AuctionTimeline(trade_date)
    t = local_datetime_ms(trade_date, "09:25:10")
    first = timeline.observe(
        "0925",
        {},
        evaluation_time_ms=t,
        expected_symbols=(),
    )
    initial_bundle = timeline.build_analysis_bundle("0925")
    plan = initial_bundle["recovery_plan"]

    assert first.state == PARTIAL
    assert initial_bundle["recovery_required"] is True
    assert plan.requested_symbols == ()
    assert plan.missing_fields == ("auction_anchor_0925_price_milli",)

    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="pinned_real_q2frame_fixture",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=None,
        available_at_ms=None,
        rows={"000560": {anchor_field: anchor_price}},
        filled_symbols=("000560",),
        filled_fields=(anchor_field,),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2_000
    )
    repeated = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 3_000
    )
    recovered_bundle = timeline.build_analysis_bundle("0925")

    assert applied.revision == first.revision + 1
    assert applied.expected_symbols == ()
    assert applied.available_anchor_symbols == ("000560",)
    assert applied.state == PARTIAL
    assert applied.anchor_coverage is None
    assert repeated.revision == applied.revision
    assert recovered_bundle["fact_status"] == FACT_ONLY
    assert recovered_bundle["recovery_required"] is False
    assert recovered_bundle["recovery_plan"] is None
    assert len(timeline.revisions("0925")) == 2


def test_idempotent_recovery_refreshes_evidence_and_stale_retry_returns_latest():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    first = timeline.observe(
        "0925",
        {
            "600519": {"auction_anchor_0925_price_milli": 10000},
            "000001": {"auction_anchor_0925_price_milli": 0},
        },
        evaluation_time_ms=t,
        expected_symbols=("600519", "000001"),
        source_layers=("t1_v2_q2frame_event_time_replay",),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    anchor_field = "auction_anchor_0925_price_milli"

    def result(observed_at_ms):
        return RecoveryResultV1(
            plan_id=plan.plan_id,
            idempotency_key=plan.idempotency_key,
            source="wencai",
            recovery_state=RECOVERY_APPLIED,
            observed_at_ms=observed_at_ms,
            available_at_ms=None,
            rows={
                "600519": {anchor_field: 10000},
                "000001": {anchor_field: 10100},
            },
            filled_symbols=("000001",),
            filled_fields=(anchor_field,),
            base_revision=plan.current_revision,
            resulting_revision=plan.current_revision + 1,
        )

    applied = timeline.apply_recovery_result(
        plan, result(t + 1_000), evaluation_time_ms=t + 2_000
    )
    refreshed = timeline.apply_recovery_result(
        plan, result(t + 2_500), evaluation_time_ms=t + 3_000
    )

    assert refreshed.revision == applied.revision == first.revision + 1
    assert refreshed.content_hash == applied.content_hash
    assert refreshed.evidence_hash != applied.evidence_hash
    assert refreshed.observed_at_ms == t + 2_500
    assert refreshed.evaluation_time_ms == t + 3_000
    assert timeline.latest("0925") == refreshed
    assert len(timeline.revisions("0925")) == 2

    older_source_observation = timeline.apply_recovery_result(
        plan, result(t + 2_000), evaluation_time_ms=t + 4_000
    )
    assert older_source_observation.revision == refreshed.revision
    assert older_source_observation.observed_at_ms == refreshed.observed_at_ms
    assert older_source_observation.evaluation_time_ms == t + 4_000
    assert len(timeline.revisions("0925")) == 2

    delayed_evaluation = timeline.apply_recovery_result(
        plan, result(t + 5_000), evaluation_time_ms=t + 3_500
    )
    assert delayed_evaluation == older_source_observation
    assert timeline.latest("0925") == older_source_observation

    newer_revision = timeline.observe(
        "0925",
        {
            "600519": {anchor_field: 10050},
            "000001": {anchor_field: 10100},
        },
        evaluation_time_ms=t + 5_000,
        expected_symbols=("600519", "000001"),
        source_layers=("later_q2_observation",),
    )
    stale_retry = timeline.apply_recovery_result(
        plan, result(t + 6_000), evaluation_time_ms=t + 7_000
    )
    assert stale_retry.revision == newer_revision.revision
    assert stale_retry.content_hash == newer_revision.content_hash
    assert timeline.latest("0925") == newer_revision
    assert len(timeline.revisions("0925")) == 3
    assert timeline.revisions("0925")[-2].revision == older_source_observation.revision
    assert timeline.revisions("0925")[-2].content_hash == older_source_observation.content_hash
    assert timeline.revisions("0925")[-1] == newer_revision


def test_apply_recovery_result_rejects_wrong_plan_identity_without_mutating_timeline():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    timeline.observe(
        "0925",
        {"600519": {"auction_anchor_0925_price_milli": 10000}},
        evaluation_time_ms=t,
        expected_symbols=("600519", "000001"),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    result = RecoveryResultV1(
        plan_id="not-the-requested-plan",
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1_000,
        available_at_ms=None,
        rows={
            "600519": {"auction_anchor_0925_price_milli": 10000},
            "000001": {"auction_anchor_0925_price_milli": 10100},
        },
        filled_symbols=("000001",),
        filled_fields=("auction_anchor_0925_price_milli",),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    with pytest.raises(ValueError, match="plan identity"):
        timeline.apply_recovery_result(plan, result, evaluation_time_ms=t + 2_000)

    assert len(timeline.revisions("0925")) == 1
    assert timeline.latest("0925").revision == plan.current_revision


def test_apply_recovery_result_rejects_stale_base_revision():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    timeline.observe(
        "0925",
        {
            "600519": {"auction_anchor_0925_price_milli": 10000},
            "000001": {"auction_anchor_0925_price_milli": 0},
        },
        evaluation_time_ms=t,
        expected_symbols=("600519", "000001"),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    timeline.observe(
        "0925",
        {
            "600519": {"auction_anchor_0925_price_milli": 10050},
            "000001": {"auction_anchor_0925_price_milli": 0},
        },
        evaluation_time_ms=t + 1_000,
        expected_symbols=("600519", "000001"),
    )
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 2_000,
        available_at_ms=None,
        rows={
            "600519": {"auction_anchor_0925_price_milli": 10000},
            "000001": {"auction_anchor_0925_price_milli": 10100},
        },
        filled_symbols=("000001",),
        filled_fields=("auction_anchor_0925_price_milli",),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    with pytest.raises(ValueError, match="stale recovery plan"):
        timeline.apply_recovery_result(plan, result, evaluation_time_ms=t + 3_000)

    assert timeline.latest("0925").revision == plan.current_revision + 1
    assert timeline.latest("0925").available_anchor_symbols == ("600519",)


def test_apply_recovery_result_cannot_replace_an_existing_anchor_value():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    timeline.observe(
        "0925",
        {
            "600519": {"auction_anchor_0925_price_milli": 10000},
            "000001": {"auction_anchor_0925_price_milli": 0},
        },
        evaluation_time_ms=t,
        expected_symbols=("600519", "000001"),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1_000,
        available_at_ms=None,
        rows={
            "600519": {"auction_anchor_0925_price_milli": 9999},
            "000001": {"auction_anchor_0925_price_milli": 10100},
        },
        filled_symbols=("000001",),
        filled_fields=("auction_anchor_0925_price_milli",),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2_000
    )

    assert applied.available_anchor_symbols == ("000001", "600519")
    assert applied.invalid_source_symbols == ("600519",)
    assert "RECOVERY_CONFLICTING_SOURCE_ROW_QUARANTINED" in applied.source_anomaly_codes
    assert timeline._source_rows["0925"]["600519"]["auction_anchor_0925_price_milli"] == 10000
    assert len(timeline.revisions("0925")) == 2


@pytest.mark.parametrize(
    "field_name,replacement",
    [
        ("source_time_ms", 99_999_999_999_999),
        ("auction_amount_yuan", 999_999),
        ("auction_amount_yuan", None),
    ],
)
def test_apply_recovery_result_preserves_every_observed_primary_field(
    field_name, replacement
):
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    timeline.observe(
        "0925",
        {
            "600519": {
                "auction_anchor_0925_price_milli": 10000,
                "source_time_ms": t - 1000,
                "auction_amount_yuan": 123,
            },
            "000001": {
                "auction_anchor_0925_price_milli": 0,
                "source_time_ms": t - 1000,
                "auction_amount_yuan": 0,
            },
        },
        evaluation_time_ms=t,
        expected_symbols=("600519", "000001"),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    existing_row = {
        "auction_anchor_0925_price_milli": 10000,
        "source_time_ms": t - 1000,
        "auction_amount_yuan": 123,
    }
    existing_row[field_name] = replacement
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1000,
        available_at_ms=None,
        rows={
            "600519": existing_row,
            "000001": {
                "auction_anchor_0925_price_milli": 10100,
                "source_time_ms": t - 1000,
                "auction_amount_yuan": 0,
            },
        },
        filled_symbols=("000001",),
        filled_fields=("auction_anchor_0925_price_milli",),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2000
    )
    repeated = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 3000
    )

    assert applied.available_anchor_symbols == ("000001", "600519")
    assert applied.invalid_source_symbols == ("600519",)
    assert "RECOVERY_CONFLICTING_SOURCE_ROW_QUARANTINED" in applied.source_anomaly_codes
    assert repeated.revision == applied.revision
    assert repeated.content_hash == applied.content_hash
    assert timeline._source_rows["0925"]["600519"] == {
        "auction_anchor_0925_price_milli": 10000,
        "source_time_ms": t - 1000,
        "auction_amount_yuan": 123,
    }
    assert len(timeline.revisions("0925")) == 2


def test_apply_recovery_result_quarantines_unrequested_field_and_keeps_valid_fill():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    timeline.observe(
        "0925",
        {
            "600519": {"auction_anchor_0925_price_milli": 10000},
            "000001": {"auction_anchor_0925_price_milli": 0},
        },
        evaluation_time_ms=t,
        expected_symbols=("600519", "000001"),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1000,
        available_at_ms=None,
        rows={
            "600519": {
                "auction_anchor_0925_price_milli": 10000,
                "unexpected_metric": 9,
            },
            "000001": {"auction_anchor_0925_price_milli": 10100},
        },
        filled_symbols=("000001",),
        filled_fields=("auction_anchor_0925_price_milli",),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2000
    )

    assert applied.available_anchor_symbols == ("000001", "600519")
    assert applied.invalid_source_symbols == ()
    assert any(
        code.startswith("RECOVERY_OUT_OF_SCOPE_FIELD_QUARANTINED:unexpected_metric")
        for code in applied.source_anomaly_codes
    )
    assert timeline._source_rows["0925"]["600519"] == {
        "auction_anchor_0925_price_milli": 10000,
    }
    assert timeline._source_rows["0925"]["000001"] == {
        "auction_anchor_0925_price_milli": 10100,
    }
    assert len(timeline.revisions("0925")) == 2


def test_sparse_recovery_quarantines_unrequested_symbol_and_keeps_valid_sibling():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    timeline.observe(
        "0925",
        {
            "600519": {"auction_anchor_0925_price_milli": 10000},
            "000001": {"auction_anchor_0925_price_milli": 0},
        },
        evaluation_time_ms=t,
        expected_symbols=("600519", "000001"),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1_000,
        available_at_ms=None,
        rows={
            "000001": {"auction_anchor_0925_price_milli": 10100},
            "000003": {
                "auction_anchor_0925_price_milli": 10200,
                "unexpected_metric": 9,
            },
        },
        filled_symbols=("000001", "000003"),
        filled_fields=("auction_anchor_0925_price_milli",),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2_000
    )

    assert applied.available_anchor_symbols == ("000001", "600519")
    assert applied.invalid_source_symbols == ("000003",)
    assert "RECOVERY_OUT_OF_SCOPE_SYMBOL_QUARANTINED" in applied.source_anomaly_codes
    assert any(
        code.startswith("RECOVERY_OUT_OF_SCOPE_FIELD_QUARANTINED:unexpected_metric")
        for code in applied.source_anomaly_codes
    )
    assert "000003" not in timeline._source_rows["0925"]
    assert len(timeline.revisions("0925")) == 2
    repeated = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 3_000
    )
    assert repeated.revision == applied.revision
    assert repeated.content_hash == applied.content_hash


def test_recovery_with_only_out_of_scope_fill_records_error_without_poisoning_cohort():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    anchor_field = "auction_anchor_0925_price_milli"
    timeline.observe(
        "0925",
        {"000001": {anchor_field: 0}},
        evaluation_time_ms=t,
        expected_symbols=("000001",),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1_000,
        available_at_ms=None,
        rows={"000001": {anchor_field: 0, "unexpected_metric": 9}},
        filled_symbols=("000001",),
        filled_fields=("unexpected_metric",),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2_000
    )

    assert applied.recovery_state == "ERROR"
    assert applied.available_anchor_symbols == ()
    assert applied.state == PARTIAL
    assert timeline.build_analysis_bundle("0925")["recovery_required"] is True
    assert any(
        code.startswith("RECOVERY_OUT_OF_SCOPE_FIELD_QUARANTINED:unexpected_metric")
        for code in applied.source_anomaly_codes
    )
    assert "unexpected_metric" not in timeline._source_rows["0925"]["000001"]


def test_apply_recovery_result_quarantines_embedded_symbol_mismatch_and_keeps_sibling():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    anchor_field = "auction_anchor_0925_price_milli"
    timeline.observe(
        "0925",
        {
            "000001": {anchor_field: 0},
            "000003": {anchor_field: 0},
        },
        evaluation_time_ms=t,
        expected_symbols=("000001", "000003"),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1_000,
        available_at_ms=None,
        rows={
            "000001": {anchor_field: 10100, "symbol": "000002"},
            "000003": {anchor_field: 10300, "symbol": "000003"},
        },
        filled_symbols=("000001", "000003"),
        filled_fields=(anchor_field,),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2_000
    )
    repeated = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 3_000
    )

    assert applied.state == PARTIAL
    assert applied.available_anchor_symbols == ("000003",)
    assert applied.missing_anchor_symbols == ("000001",)
    assert applied.invalid_source_symbols == ("000001",)
    assert "RECOVERY_EMBEDDED_SYMBOL_MISMATCH_QUARANTINED" in applied.source_anomaly_codes
    assert timeline._source_rows["0925"]["000001"][anchor_field] == 0
    assert timeline._source_rows["0925"]["000003"][anchor_field] == 10300
    assert repeated.revision == applied.revision
    assert repeated.content_hash == applied.content_hash
    assert len(timeline.revisions("0925")) == 2


def test_recovery_quarantines_conflicting_symbol_and_keeps_valid_sibling_fill():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    anchor_field = "auction_anchor_0925_price_milli"
    timeline.observe(
        "0925",
        {
            "600519": {
                anchor_field: 10000,
                "source_time_ms": t - 1000,
            },
            "000001": {
                anchor_field: 0,
                "source_time_ms": t - 1000,
            },
            "000002": {
                anchor_field: 0,
                "source_time_ms": t - 1000,
            },
        },
        evaluation_time_ms=t,
        expected_symbols=("600519", "000001", "000002"),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1000,
        available_at_ms=None,
        rows={
            # One changed, already-observed field makes this symbol's recovery
            # row conflicting, but must not discard the valid sibling fill.
            "600519": {
                anchor_field: 10000,
                "source_time_ms": t - 2000,
            },
            "000001": {
                anchor_field: 10100,
                "source_time_ms": t - 1000,
            },
        },
        filled_symbols=("000001",),
        filled_fields=(anchor_field,),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2000
    )
    repeated = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 3000
    )

    assert applied.available_anchor_symbols == ("000001", "600519")
    assert applied.missing_anchor_symbols == ("000002",)
    assert applied.invalid_source_symbols == ("600519",)
    assert "RECOVERY_CONFLICTING_SOURCE_ROW_QUARANTINED" in applied.source_anomaly_codes
    assert repeated.revision == applied.revision
    assert repeated.content_hash == applied.content_hash
    assert repeated.invalid_source_symbols == ("600519",)
    assert len(timeline.revisions("0925")) == 2


def test_all_conflicting_recovery_rows_keep_retry_available_and_report_error():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    anchor_field = "auction_anchor_0925_price_milli"
    timeline.observe(
        "0925",
        {
            "000001": {
                anchor_field: 0,
                "source_time_ms": t - 1000,
            }
        },
        evaluation_time_ms=t,
        expected_symbols=(),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1000,
        available_at_ms=None,
        rows={
            "000001": {
                anchor_field: 10100,
                "source_time_ms": t - 2000,
            }
        },
        filled_symbols=("000001",),
        filled_fields=(anchor_field,),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2000
    )
    repeated = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 3000
    )

    assert applied.recovery_state == "ERROR"
    assert applied.expected_symbols == ()
    assert applied.anchor_coverage is None
    assert applied.available_anchor_symbols == ()
    assert applied.state == PARTIAL
    assert timeline.build_analysis_bundle("0925")["recovery_required"] is True
    assert applied.invalid_source_symbols == ("000001",)
    assert repeated.revision == applied.revision
    assert repeated.content_hash == applied.content_hash
    assert repeated.recovery_state == "ERROR"


def test_apply_recovery_result_quarantines_unreported_anchor_fill_only():
    timeline = AuctionTimeline("2026-09-18")
    t = local_datetime_ms("2026-09-18", "09:25:10")
    timeline.observe(
        "0925",
        {
            "600519": {"auction_anchor_0925_price_milli": 10000},
            "000001": {"auction_anchor_0925_price_milli": 0},
            "000002": {"auction_anchor_0925_price_milli": 0},
        },
        evaluation_time_ms=t,
        expected_symbols=("600519", "000001", "000002"),
    )
    plan = timeline.build_analysis_bundle("0925")["recovery_plan"]
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=t + 1_000,
        available_at_ms=None,
        rows={
            "600519": {"auction_anchor_0925_price_milli": 10000},
            "000001": {"auction_anchor_0925_price_milli": 10100},
            "000002": {"auction_anchor_0925_price_milli": 10200},
        },
        filled_symbols=("000001",),
        filled_fields=("auction_anchor_0925_price_milli",),
        base_revision=plan.current_revision,
        resulting_revision=plan.current_revision + 1,
    )

    applied = timeline.apply_recovery_result(
        plan, result, evaluation_time_ms=t + 2_000
    )

    assert applied.available_anchor_symbols == ("000001", "600519")
    assert applied.missing_anchor_symbols == ("000002",)
    assert "RECOVERY_UNDECLARED_FILL_QUARANTINED:000002.auction_anchor_0925_price_milli" in applied.source_anomaly_codes
    assert timeline._source_rows["0925"]["000002"]["auction_anchor_0925_price_milli"] == 0
    assert len(timeline.revisions("0925")) == 2
