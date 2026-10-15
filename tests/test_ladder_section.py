from engine_core.ladder_section import build_ladder_section


def test_unknown_path_and_small_sample_are_downgraded():
    section = build_ladder_section(
        [{"symbol": "600001", "lb_days": 2, "path_type": "UNKNOWN", "close_pct": 1}],
        stage="open", asof="2026-10-15T09:45:00",
        frequencies={"UNKNOWN": {"k": 9, "n": 15, "days": 1}},
    )
    assert section["asof"] == "2026-10-15T09:45:00"
    assert section["rows"][0]["path_type"] == "UNKNOWN"
    assert "样本不足" in section["rows"][0]["history_frequency"]
    assert "close_pct" not in section["rows"][0]


def test_live_chapter_checkpoint_keys_map_to_ladder_fields():
    section = build_ladder_section(
        [{"symbol": "600001", "streak": 2, "0925": 1.0, "0932": 2.0, "0945": 3.0}],
        stage="open", asof="2026-10-15T09:46:00")
    row = section["rows"][0]
    assert (row["prior_streak"], row["p0925_pct"], row["p0932_pct"], row["p0945_pct"]) == (2, 1.0, 2.0, 3.0)


def test_ladder_preserves_yesterday_pool_streak_and_marks_any_field_conflict():
    section = build_ladder_section(
        [{"symbol": "000530", "lb_days": 2, "prior_limit_streak": 0,
          "prior_streak": 1}], stage="close", asof="2026-10-15T15:00:00")
    row = section["rows"][0]
    assert row["prior_streak"] == 2
    assert row["prior_streak_status"] == "字段冲突"


def test_0945_emotion_uses_full_market_coverage_and_previous_ladder_counts():
    stocks = [{"p0932_pct": -1.0, "p0945_pct": -1.8, "limit_pct": 10} for _ in range(9)]
    stocks += [{"p0932_pct": 0.5, "p0945_pct": 0.7, "limit_pct": 10}]
    section = build_ladder_section(
        [{"symbol": "600001", "prior_streak": 2}], stage="close",
        asof="2026-10-15T15:00:00", market_stocks=stocks,
        summary=[{"board_height": "2板", "previous_n": 8}],
    )
    assert section["emotion"]["state"] == "WEAK_CONTINUE"
    assert section["emotion"]["coverage_0945"] == 1.0
    assert section["yesterday_board_counts"] == [{"board_height": "2板", "count": 8}]


def test_auction_stage_never_classifies_future_emotion():
    section = build_ladder_section([], stage="auction", asof="2026-10-15T09:26:00",
                                   market_stocks=[{"p0945_pct": -2, "p0925_pct": 0}])
    assert section["emotion"]["state"] == "UNKNOWN"
    assert section["emotion"]["label"] == "无09:45全市场截面"


def test_auction_stage_does_not_reuse_later_path_or_frequency():
    section = build_ladder_section(
        [{"symbol": "600001", "path_type": "LIMIT_HOLD"}], stage="auction",
        asof="2026-10-15T09:26:00",
        frequencies={"LIMIT_HOLD": {"n": 93, "k": 85, "days": 13}})
    assert section["rows"][0]["path_type"] == "UNKNOWN"
    assert section["rows"][0]["history_frequency"] == "竞价截点尚无开盘路径分类"


def test_open_emotion_buckets_do_not_read_close_fields():
    base = [{"p0932_pct": -1.0, "p0945_pct": -1.8, "limit_pct": 10} for _ in range(9)]
    base += [{"p0932_pct": 0.5, "p0945_pct": 0.7, "limit_pct": 10}]
    poisoned_close = [dict(row, close_pct=1000) for row in base]
    first = build_ladder_section([], stage="open", asof="2026-10-15T09:46:00", market_stocks=base)
    second = build_ladder_section([], stage="open", asof="2026-10-15T09:46:00", market_stocks=poisoned_close)
    assert first["emotion"] == second["emotion"]
