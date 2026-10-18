from engine_core.auction_stock_facts import build_auction_stock_facts


def test_stock_facts_rank_amounts_without_price_and_use_final_buy1_for_seal():
    snapshots = [
        {"symbol": "002815", "name": "比亚迪", "auction_amount_yuan": 100, "price_milli": 25580,
         "pre_close_milli": 23250},
        {"symbol": "000001", "name": "平安", "auction_amount_yuan": 100, "price_milli": None,
         "pre_close_milli": None},
        {"symbol": "600001", "name": "甲", "auction_amount_yuan": None},
    ]
    ticks = [{"symbol": "002815", "source_ts": "2026-10-16T09:25:00+08:00",
              "px_milli": 25580, "amt_yuan": 100, "bp1_milli": 25580, "bv1": 350287},
             {"symbol": "002815", "source_ts": "2026-10-16T09:26:01+08:00",
              "px_milli": 25580, "amt_yuan": 200, "bp1_milli": 25580, "bv1": 999999}]

    result = build_auction_stock_facts(
        "2026-10-16", "2026-10-16T09:26:00+08:00", snapshots, ticks,
        tick_volume_unit="lots",
        now="2026-10-16T09:26:05+08:00", read_at="2026-10-16T09:26:05+08:00",
    )

    rows = {row["symbol"]: row for row in result["rows"]}
    assert rows["002815"]["amount_rank"] == rows["000001"]["amount_rank"] == 1
    assert rows["002815"]["amount_top50"] is rows["000001"]["amount_top50"] is True
    assert rows["000001"]["pct_change"] is None
    assert rows["002815"]["seal"]["status"] == "VERIFIED"
    assert rows["002815"]["seal"]["seal_amount_yuan"] == 896034146
    assert rows["600001"]["amount_rank"] is None
    assert result["summary"]["valid_amount_n"] == 2
    assert rows["002815"]["seal"]["basis"] == "LIVE_READ"
    assert rows["002815"]["seal"]["tick_source_ts"] == "2026-10-16T09:25:00+08:00"
    assert rows["002815"]["seal"]["read_at"] == "2026-10-16T09:26:05+08:00"


def test_replay_uses_event_time_and_excludes_after_asof_ticks():
    snapshots = [{"symbol": "002815", "name": "比亚迪", "pre_close_milli": 23250}]
    ticks = [
        {"symbol": "002815", "source_ts": "2026-10-15T09:26:01+08:00", "px_milli": 25580,
         "amt_yuan": 100, "bp1_milli": 25580, "bv1": 2},
        {"symbol": "002815", "source_ts": "2026-10-15T09:26:00+08:00", "px_milli": 25580,
         "amt_yuan": 100, "bp1_milli": 25580, "bv1": 2},
    ]
    result = build_auction_stock_facts("2026-10-15", "2026-10-15T09:26:00+08:00",
                                       snapshots, ticks, tick_volume_unit="lots",
                                       now="2026-10-18T12:00:00+08:00")
    seal = result["rows"][0]["seal"]
    assert seal["status"] == "VERIFIED_REPLAY"
    assert seal["basis"] == "EVENT_TIME_ONLY"
    assert seal["tick_source_ts"] == "2026-10-15T09:26:00+08:00"
    assert result["summary"]["verified_live_n"] == 0
    assert result["summary"]["verified_replay_n"] == 1
