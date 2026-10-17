from engine_core.stock_theme_assignment import build_stock_theme_assignments


def test_assignment_uses_priority_preserves_secondary_and_filters_future_reason():
    result = build_stock_theme_assignments(
        trade_date="2026-10-16", previous_trade_date="2026-10-15",
        pool_labels={"000001": [{"code": "P1", "name": "池主题"}, {"code": "P2", "name": "池副题"}]},
        reason_rows=[
            {"symbol": "000001", "Date": "2026-10-15", "ZSCode": ["R1"], "Reason": "昨日原因"},
            {"symbol": "000002", "Date": "2026-10-16", "ZSCode": ["FUTURE"]},
            {"symbol": "000003", "Date": "2026-10-15", "ZSCode": ["R3", "R4"]},
        ],
        static_themes={"000001": [{"code": "S1", "name": "静态"}],
                       "000002": [{"code": "S2", "name": "静态二"}]},
        hot_plate_names={"000001": [{"code": "H1", "name": "热板"}],
                         "000004": [{"code": "H4", "name": "热板四"}]},
        members={"000001": ["P1", "P2", "R1", "S1", "H1"], "000002": ["S2"],
                 "000003": ["R3", "R4"], "000004": ["H4"], "000005": []},
    )
    assert result["000001"]["primary_theme"] == {"id": "P1", "name": "池主题"}
    assert result["000001"]["source"] == "POOL_LABEL"
    assert [item["id"] for item in result["000001"]["secondary_themes"]] == ["P2", "R1", "S1", "H1"]
    assert result["000002"]["source"] == "STATIC"
    assert result["000003"]["source"] == "REASON_LATEST"
    assert result["000004"]["source"] == "HOT_PLATE_NAME"
    assert result["000005"]["source"] == "UNKNOWN"
    assert result["000005"]["primary_theme"] is None
    assert all(row["as_of"] == "2026-10-15" for row in result.values())
