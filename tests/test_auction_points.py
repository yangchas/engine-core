import gzip
import json

from engine_core.auction_points import normalize_auction_capture


def test_normalize_market_and_a2_rank_rows_preserves_missing_and_units():
    market = {"key": "market:auction:20261016:0920", "fetched_at": "2026-10-17T08:51:00+08:00",
              "fields": {"meta": json.dumps({"tag": "0920", "ts": 1792113603048}),
                         "summary": json.dumps({"total_auction_amount_yuan": 100}),
                         "top_amount": json.dumps([{"symbol": "600000", "price": 9.73,
                                                     "auction_amount_yuan": 20}])}}
    a2 = {"key": "a2:20261016:0920", "fetched_at": "2026-10-17T08:51:00+08:00",
          "fields": {"meta": json.dumps({"tag": "0920", "ts": 1792113603048}),
                     "top_chg": json.dumps([{"s": "600000", "px": 9730, "chg": 100,
                                             "am": 20, "br": 4, "ar": 5, "ls": 0}])}}

    rows = normalize_auction_capture("2026-10-16", [market, a2])

    a2_row = next(row for row in rows if row["source_key"] == "a2" and row["row_kind"] == "rank")
    market_row = next(row for row in rows if row["source_key"] == "market:auction" and row["row_kind"] == "rank")
    assert a2_row["symbol"] == "600000"
    assert a2_row["price_milli"] == 9730
    assert a2_row["change_bp"] == 100
    assert a2_row["auction_amount_yuan"] == 20
    assert market_row["rest_bid_amount_yuan"] is None
    assert {row["source_key"] for row in rows} == {"market:auction", "a2"}


def test_import_raw_auction_captures_is_idempotent(tmp_path, monkeypatch):
    from engine_core.auction_points import import_raw_auction_captures
    monkeypatch.delenv("REDIS_URL", raising=False)

    raw_root = tmp_path / "raw"
    day = raw_root / "trade_date=2026-10-16"
    day.mkdir(parents=True)
    capture = {"key": "a2:20261016:0920", "fetched_at": "2026-10-17T08:51:00+08:00",
               "fields": {"meta": json.dumps({"tag": "0920", "ts": 1792113603048}),
                          "top_amt": json.dumps([{"s": "600000", "px": 9730, "chg": 100,
                                                  "am": 20, "br": 4, "ar": 5, "ls": 0}])}}
    (day / "a2_20261016_0920.json.gz").write_bytes(gzip.compress(json.dumps(capture).encode()))

    first = import_raw_auction_captures("2026-10-16", raw_root=raw_root, core_root=tmp_path / "core")
    second = import_raw_auction_captures("2026-10-16", raw_root=raw_root, core_root=tmp_path / "core")

    assert first["status"] == "IMPORTED"
    assert first["row_count"] == 1
    assert second["status"] == "ALREADY_EXISTS"
