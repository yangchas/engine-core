from engine_core import (
    AuctionTimeline,
    AuctionTimingPolicyV1,
    FACT_ONLY,
    OBSERVING,
    PARTIAL,
    READY,
    local_datetime_ms,
)


def _row(symbol, ts, **fields):
    return {"symbol": symbol, "ts": ts, **fields}


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
