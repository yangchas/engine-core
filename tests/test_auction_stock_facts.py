from engine_core.auction_stock_facts import build_auction_stock_facts
import pytest


def test_stock_facts_rank_amounts_without_price_and_use_final_buy1_for_seal():
    snapshots = [
        {"symbol": "002815", "name": "比亚迪", "auction_amount_yuan": 100, "price_milli": 25580,
         "pre_close_milli": 23250},
        {"symbol": "000001", "name": "平安", "auction_amount_yuan": 100, "price_milli": None,
         "pre_close_milli": None},
        {"symbol": "000002", "name": "乙", "auction_amount_yuan": 100, "price_milli": 0,
         "pre_close_milli": 10000},
        {"symbol": "600001", "name": "甲", "auction_amount_yuan": None},
    ]
    ticks = [{"symbol": "002815", "source_ts": "2026-10-16T09:25:00+08:00",
              "px_milli": 25580, "amt_yuan": 100, "bp1_milli": 25580, "bv1": 350287},
             {"symbol": "002815", "source_ts": "2026-10-16T09:26:01+08:00",
              "px_milli": 25580, "amt_yuan": 200, "bp1_milli": 25580, "bv1": 999999},
             {"symbol": "000002", "source_ts": "2026-10-16T09:25:00+08:00",
              "px_milli": 11000, "amt_yuan": 100, "bp1_milli": 11000, "bv1": 10}]

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
    assert result["summary"]["valid_amount_n"] == 3
    assert rows["000002"]["price"] == 11.0
    assert rows["000002"]["valid_price"] is True
    assert rows["000002"]["seal"]["status"] == "VERIFIED"
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


def test_snapshot_px_milli_is_the_effective_0925_price():
    result = build_auction_stock_facts(
        "2026-10-16", "2026-10-16T09:26:00+08:00",
        [{"symbol": "002815", "price_milli": 0, "px_milli": 25580, "chg_bp": 1002,
          "pre_close_milli": 23250}], [],
    )

    row = result["rows"][0]
    assert row["price"] == 25.58
    assert row["valid_price"] is True
    assert row["pct_change"] == 10.02
    assert row["official_limit_up_price"] == 25.58


def test_auction_stock_facts_include_both_anchor_amounts(tmp_path):
    from engine_core.auction_stock_facts import store_auction_stock_facts
    from engine_core.data_pipeline.storage import ArtifactCatalog

    result = build_auction_stock_facts(
        "2026-10-16", "2026-10-16T09:26:00+08:00",
        [{"symbol": "002815", "auction_amount_yuan": 250, "auction_tag": "0925"}], [],
        anchor_snapshots=[
            {"symbol": "002815", "auction_tag": "0924", "auction_amount_yuan": 100},
            {"symbol": "002815", "auction_tag": "0925", "auction_amount_yuan": 250},
        ])
    assert result["rows"][0]["p0924_amount_yuan"] == 100
    assert result["rows"][0]["p0925_amount_yuan"] == 250
    store_auction_stock_facts(result, artifact_root=tmp_path)
    rows = ArtifactCatalog(tmp_path).read_rows(
        ArtifactCatalog(tmp_path).locate("auction_stock_facts", "2026-10-16"))
    assert rows[0]["p0924_amount_yuan"] == 100
    assert rows[0]["p0925_amount_yuan"] == 250


def test_auction_collection_coverage_rejects_empty_and_low_counts():
    from examples.fetch_auction_item import _require_snapshot_coverage

    with pytest.raises(RuntimeError, match="SNAPSHOT_COVERAGE_LOW:0/"):
        _require_snapshot_coverage([])
    with pytest.raises(RuntimeError, match="SNAPSHOT_COVERAGE_LOW:100/"):
        _require_snapshot_coverage({f"{value:06d}" for value in range(100)})
    assert _require_snapshot_coverage({f"{value:06d}" for value in range(4050)}) == 4050


def test_empty_auction_stock_facts_cannot_be_committed(tmp_path):
    from engine_core.auction_stock_facts import store_auction_stock_facts
    from engine_core.data_pipeline.storage import ArtifactCatalog

    empty = build_auction_stock_facts(
        "2026-10-16", "2026-10-16T09:26:00+08:00", [], [])
    with pytest.raises(ValueError, match="AUCTION_STOCK_FACTS_EMPTY"):
        store_auction_stock_facts(empty, artifact_root=tmp_path)
    assert "auction_stock_facts" not in ArtifactCatalog(tmp_path).read().get("datasets", {})


def test_missing_auction_v6_facts_are_explicit_and_do_not_use_fallback_stocks():
    from engine_core.auction_v6_inputs import prepare_auction_v6_inputs

    payload = prepare_auction_v6_inputs(
        trade_date="2026-10-16", asof="2026-10-16T09:26:00+08:00", stock_facts=[])
    assert payload["status"] == "MISSING"
    assert payload["stocks"] == []
    assert payload["snapshots"][0]["status"] == "MISSING"
