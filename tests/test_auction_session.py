from engine_core import (
    AuctionAnchorSessionV1,
    local_datetime_ms,
)


def _rows(a20: int = 10_100, *, a24: int = 11_100, a25: int = 12_100):
    source_time = local_datetime_ms("2026-09-18", "09:25:06")
    return {
        "000001": {
            "symbol": "000001",
            "auction_anchor_0920_price_milli": a20,
            "auction_anchor_0924_price_milli": a24,
            "auction_anchor_0925_price_milli": a25,
            "auction_anchor_field_quality": {
                "a20": "PRESENT_VALUE",
                "a24": "PRESENT_VALUE",
                "a25": "PRESENT_VALUE",
            },
            "source_record_time_ms": source_time,
            "source_time_ms": source_time,
        }
    }


def test_barrier_timer_identity_is_shared_and_stable():
    logical_ms = local_datetime_ms("2026-09-18", "09:20:03")
    q2frame = AuctionAnchorSessionV1("2026-09-18", ("000001",))
    redis = AuctionAnchorSessionV1(
        "2026-09-18", ("000001",), signal_prefix="redis-q2-capture"
    )

    q2frame_signal = q2frame.barrier_timer_signal("0920", logical_ms, 0)
    redis_signal = redis.barrier_timer_signal("0920", logical_ms, 0)

    assert q2frame_signal.signal_id == "q2frame-timer:0920"
    assert redis_signal.signal_id == "redis-q2-capture-timer:0920"
    assert q2frame_signal.logical_time_ms == redis_signal.logical_time_ms == logical_ms
    assert q2frame_signal.signal_seq == redis_signal.signal_seq == 1_000_000_000
    assert q2frame_signal.payload == redis_signal.payload == {"trigger_id": "AUCTION_0920"}


def test_late_start_requires_raw_field_presence_and_does_not_backdate():
    session = AuctionAnchorSessionV1("2026-09-18", ("000001",))
    logical_ms = local_datetime_ms("2026-09-18", "09:42:26")

    absent = session.observe_late_start(
        "0925",
        _rows(),
        logical_time_ms=logical_ms,
        anchor_field_observed=False,
    )
    assert absent.status == "ANCHOR_FIELD_NOT_PRESENT"
    assert absent.revision is None
    assert absent.timer_signal is None
    assert session.timeline.latest("0925") is None

    observed = session.observe_late_start(
        "0925",
        _rows(),
        logical_time_ms=logical_ms,
        anchor_field_observed=True,
        observed_at_ms=logical_ms + 250,
        source_layers=("redis_q2_projection",),
    )
    assert observed.status == "LATE_START_OBSERVED"
    assert observed.revision is not None
    assert observed.revision.evaluation_time_ms == logical_ms
    assert observed.revision.observed_at_ms == logical_ms + 250
    assert observed.revision.late_execution is True
    assert observed.timer_signal is not None
    assert observed.timer_signal.logical_time_ms == logical_ms
    assert observed.timer_signal.signal_id == (
        f"q2frame-late-start-timer:0925:{observed.revision.revision}"
    )


def test_late_revision_timer_only_fires_for_new_content():
    session = AuctionAnchorSessionV1("2026-09-18", ("000001",))
    first_ms = local_datetime_ms("2026-09-18", "09:25:06")
    first = session.observe(
        "0925",
        _rows(),
        evaluation_time_ms=first_ms,
        source_layers=("q2frame",),
    )

    unchanged = session.observe_late_revision(
        "0925",
        _rows(),
        logical_time_ms=first_ms + 1_000,
        source_layers=("q2frame",),
    )
    assert unchanged.status == "UNCHANGED"
    assert unchanged.revision is not None
    assert unchanged.revision.revision == first.revision
    assert unchanged.timer_signal is None

    changed = session.observe_late_revision(
        "0925",
        _rows(a25=12_200),
        logical_time_ms=first_ms + 2_000,
        source_layers=("q2frame",),
    )
    assert changed.status == "REVISION_EMITTED"
    assert changed.revision is not None
    assert changed.revision.revision == first.revision + 1
    assert changed.revision.supersedes_revision == first.revision
    assert changed.timer_signal is not None
    assert changed.timer_signal.signal_id == "q2frame-revision-timer:0925:2"
    assert changed.timer_signal.signal_seq == 2_002_000_002


def test_same_q2_rows_have_same_content_revision_across_source_sessions():
    logical_ms = local_datetime_ms("2026-09-18", "09:25:06")
    first = AuctionAnchorSessionV1("2026-09-18", ("000001",))
    second = AuctionAnchorSessionV1("2026-09-18", ("000001",))

    q2frame_revision = first.observe(
        "0925", _rows(), evaluation_time_ms=logical_ms, source_layers=("q2frame",)
    )
    redis_revision = second.observe(
        "0925", _rows(), evaluation_time_ms=logical_ms, source_layers=("redis_q2",)
    )

    assert q2frame_revision.content_hash == redis_revision.content_hash
    assert q2frame_revision.evidence_hash != redis_revision.evidence_hash


def _final_tick_rows(*, when: str = "09:26:33", anchor=None, phase=0, volume=120):
    source_time = local_datetime_ms("2026-10-09", when)
    quality = {"a25": "PRESENT_VALUE"} if anchor else {}
    return {
        "000001": {
            "symbol": "000001",
            "phase": phase,
            "price_milli": 12_950,
            "volume_lots": volume,
            "amount_yuan": 1_554_000,
            "auction_anchor_0925_price_milli": anchor,
            "auction_anchor_field_quality": quality,
            "source_record_time_ms": source_time,
            "source_time_ms": source_time,
        }
    }


def test_phase_zero_final_snapshot_recovers_by_default_and_can_be_disabled():
    at = local_datetime_ms("2026-10-09", "09:26:40")
    off = AuctionAnchorSessionV1(
        "2026-10-09", ("000001",), recover_final_from_phase_zero=False
    )
    on = AuctionAnchorSessionV1("2026-10-09", ("000001",))
    rows = _final_tick_rows()
    assert off.observe("0925", rows, evaluation_time_ms=at).available_anchor_symbols == ()
    revision = on.observe("0925", rows, evaluation_time_ms=at)
    assert revision.available_anchor_symbols == ("000001",)
    recovered = on.phase_zero_recoveries["000001"]
    assert recovered["price_milli"] == 12_950
    assert recovered["source_time_ms"] == local_datetime_ms("2026-10-09", "09:26:33")
    assert recovered["used"] is True


def test_phase_zero_recovery_ignores_other_phases_zero_volume_and_existing_anchor():
    at = local_datetime_ms("2026-10-09", "09:26:40")
    for rows in (
        _final_tick_rows(phase=1),
        _final_tick_rows(volume=0),
        _final_tick_rows(when="09:24:57"),
        _final_tick_rows(when="09:30:03"),
    ):
        session = AuctionAnchorSessionV1(
            "2026-10-09", ("000001",), recover_final_from_phase_zero=True
        )
        revision = session.observe("0925", rows, evaluation_time_ms=at)
        assert revision.available_anchor_symbols == ()
        assert session.phase_zero_recoveries == {}
    present = AuctionAnchorSessionV1(
        "2026-10-09", ("000001",), recover_final_from_phase_zero=True
    )
    present.observe("0925", _final_tick_rows(anchor=12_900), evaluation_time_ms=at)
    assert present.phase_zero_recoveries == {}


def test_phase_zero_final_recovery_helper_marks_only_late_final_snapshots():
    from engine_core.auction_final_recovery import PhaseZeroFinalRecovery

    helper = PhaseZeroFinalRecovery("2026-10-09")
    stamp = lambda hhmmss: local_datetime_ms("2026-10-09", hhmmss)
    updates = [
        {"symbol": "000001", "ph": 0, "vol": 10, "px": 12_950, "a25": 0, "ts": stamp("09:26:33")},
        {"symbol": "000002", "ph": 0, "vol": 10, "px": 5_000, "a25": 5_000, "ts": stamp("09:25:00")},
        {"symbol": "000003", "ph": 1, "vol": 0, "px": 7_000, "a25": 0, "ts": stamp("09:24:57")},
        {"symbol": "000004", "ph": 0, "vol": 0, "px": 7_000, "a25": 0, "ts": stamp("09:26:10")},
    ]
    out = helper.apply(updates)
    assert out[0]["a25"] == 12_950 and out[1]["a25"] == 5_000
    assert out[2]["a25"] == 0 and out[3]["a25"] == 0
    assert set(helper.recovered) == {"000001"}
    assert helper.recovered["000001"]["source_time_ms"] == stamp("09:26:33")
