from engine_core import (
    AuctionAnchorFactV1,
    AuctionTimeline,
    AuctionTimingPolicyV1,
    FACT_ONLY,
    OBSERVING,
    PARTIAL,
    READY,
    build_auction_anchor_fact_v1,
    local_datetime_ms,
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
    assert bundle["recovery_plan"].missing_fields == ("anchor",)


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
    first = timeline.observe("0925", [_row("600519", t, auction_anchor_0925_price_milli=10000)], evaluation_time_ms=t, expected_symbols=("600519", "000001"))
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
    assert timeline.build_analysis_bundle("0925")["recovery_required"] is False
    assert timeline.build_analysis_bundle("0925")["recovery_plan"] is None
