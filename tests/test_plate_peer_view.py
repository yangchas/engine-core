from engine_core.plate_peer_view import (
    classify_day_type,
    classify_peer,
    limit_streaks,
    range_position,
)


def test_streaks_range_position_and_missing_history():
    history = [
        {"date": f"2026-01-{day:02d}", "symbol": "000001", "open": 10,
         "high": 11, "low": 9, "close": close, "pct_chg": 10}
        for day, close in enumerate(([10] * 18 + [11, 12]), start=1)
    ]
    assert limit_streaks(history, "2026-01-04")["000001"] == 3
    assert range_position(history, "000001", "2026-01-21") == 1.0
    assert range_position(history, "999999", "2026-01-21") is None


def test_peer_type_thresholds_and_amount_rank_are_explicit():
    flags = classify_peer(pct=0.02, plate_mean=0.01, amount_rank=15,
                          member_count=20, position=0.30, positive_amount=True)
    assert flags == ["LOW_POS", "STRONG_REL", "HOLLOW_TOP", "LOW_AMOUNT_UP"]
    assert "AMOUNT_LEAD" in classify_peer(pct=0.01, plate_mean=0.01, amount_rank=4,
                                         member_count=20, position=None, positive_amount=True)
    assert "WEAK_REL" in classify_peer(pct=-0.01, plate_mean=0.01, amount_rank=20,
                                       member_count=20, position=None, positive_amount=True)


def test_yesterday_day_type_priority_and_boundaries():
    assert classify_day_type(left_tail=300, right_tail=999, median=1, up_share=0.8) == "恐慌"
    assert classify_day_type(left_tail=0, right_tail=200, median=0.01, up_share=0.4) == "修复/强势"
    assert classify_day_type(left_tail=0, right_tail=0, median=-1.2, up_share=0.4) == "弱势"
    assert classify_day_type(left_tail=0, right_tail=0, median=0, up_share=0.25) == "弱势"


def test_cluster_deduplicates_a_coverage_and_tracks_peer_types():
    from engine_core.plate_peer_view import summarize_clusters

    rows = [
        {"symbol": "A", "plate": "传媒", "is_a": True, "a_buyable": False, "peer_flags": []},
        {"symbol": "B", "plate": "传媒", "is_a": False, "peer_flags": ["LOW_POS"], "has_observed_support": True},
        {"symbol": "C", "plate": "传媒", "is_a": False, "peer_flags": ["STRONG_REL", "HOLLOW_TOP"]},
    ]
    result = summarize_clusters(rows)
    assert result[0]["a_count"] == 1
    assert result[0]["peer_count"] == 2
    assert result[0]["low_pos_count"] == 1
    assert result[0]["strong_rel_count"] == 1
    assert result[0]["hollow_top_count"] == 1
    assert result[0]["a_unbuyable_with_peers"] is True
