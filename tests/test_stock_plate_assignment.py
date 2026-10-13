from engine_core.stock_plate_assignment import build_stock_plate_assignments


def test_pool_assignment_merges_candidates_and_orders_primary_first():
    result = build_stock_plate_assignments(
        trade_date="2026-10-13",
        static_primary_by_symbol={"000001": "通信"},
        static_themes_by_symbol={"000001": ["光纤", "芯片"]},
        pool_rows=[{"symbol": "000001", "plate": "国企改革,通信"}],
        reasons_by_symbol={"000001": [{"reason": "算力", "group_str": "芯片", "gnsm": ""}]},
    )
    row = result.assignments["000001"]
    assert row.primary_plate == "通信"
    assert row.themes == ("通信", "芯片", "算力", "国企改革", "光纤")
    assert row.sources == ("static", "pool", "reason")


def test_static_only_cleans_names_and_missing_primary_stays_missing():
    result = build_stock_plate_assignments(
        trade_date="2026-10-13",
        static_primary_by_symbol={"000001": "北京市"},
        static_themes_by_symbol={"000001": ["芯片概念", "公司主营业务", "通信"]},
    )
    row = result.assignments["000001"]
    assert row.primary_plate is None
    assert row.themes == ("芯片", "通信")
    assert "primary_not_in_themes" not in row.flags
    assert row.status == "PARTIAL"


def test_pool_other_is_not_a_candidate_and_flags_status_are_explicit():
    result = build_stock_plate_assignments(
        trade_date="2026-10-13",
        static_primary_by_symbol={},
        static_themes_by_symbol={},
        pool_rows=[{"symbol": "000002", "plate": "其他"}],
    )
    row = result.assignments["000002"]
    assert row.primary_plate is None
    assert row.themes == ()
    assert set(row.flags) == {"pool_plate_missing", "themes_missing"}
    assert row.status == "MISSING"


def test_assignment_content_hash_is_stable_and_exports_public_types():
    from engine_core import PlateAssignments, StockPlateAssignment

    kwargs = dict(
        trade_date="2026-10-13",
        static_primary_by_symbol={"000001": "通信"},
        static_themes_by_symbol={"000001": ["光纤", "通信"]},
    )
    first = build_stock_plate_assignments(**kwargs)
    second = build_stock_plate_assignments(**kwargs)
    assert isinstance(first, PlateAssignments)
    assert isinstance(first.assignments["000001"], StockPlateAssignment)
    assert first.content_hash == second.content_hash
