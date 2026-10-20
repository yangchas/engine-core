import json
import engine_core.auction_stock_facts as stock_facts


def test_auction_points_late_rebuild_is_marked_and_uses_snapshot_tags(tmp_path, monkeypatch):
    from examples import fetch_auction_item
    monkeypatch.setattr(stock_facts, "build_auction_stock_facts",
                        lambda *a, **k: {"trade_date": a[0], "asof": a[1], "rows": [],
                                         "summary": {}, "rank_method": "fixture"})
    snapshots = [
        {"auction_tag": "0920", "symbol": "000001", "auction_amount_yuan": 10, "change_bp": 5},
        {"auction_tag": "0925", "symbol": "000002", "auction_amount_yuan": 20, "change_bp": 10},
    ]
    result = fetch_auction_item._products("2026-10-20", "auction_points", tmp_path, snapshots, [])
    assert result["rows"] == 2
    from engine_core.data_pipeline.storage import ArtifactCatalog
    path = ArtifactCatalog(tmp_path).locate("auction_points", "2026-10-20")
    manifest = json.loads(path.read_text())
    assert manifest["late_rebuild"] is True
    assert manifest["not_a_0925_live_capture"] is True
    assert manifest["available_at"]


def test_auction_fetch_read_only_mode_does_not_publish(tmp_path, monkeypatch, capsys):
    from examples import fetch_auction_item

    monkeypatch.setattr(fetch_auction_item, "_source_rows", lambda _day: ([{"auction_tag": "0925"}], []))
    assert fetch_auction_item.main(["--item", "auction_stock_facts", "--date", "2026-10-20",
                                   "--artifact-root", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["read_only"] is True and result["snapshot_rows"] == 1
    assert not (tmp_path / "catalog.json").exists()
