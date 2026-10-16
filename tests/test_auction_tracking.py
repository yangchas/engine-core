from engine_core.auction_tracking import Tick, derive_state, matching_amount_yuan, replay_tracking


def test_auction_state_uses_only_equal_first_level_and_exact_match_amount():
    tick = Tick(
        trade_date="2026-10-14", ts="09:20:00", symbol="600000",
        px_milli=0, pc_milli=10000, bp1_milli=10100, ap1_milli=10100,
        bv1=20, av1=30, bp2_milli=0, ap2_milli=0, bv2=4, av2=0,
        amt_yuan=0, vol_units=0,
    )
    state = derive_state(tick)
    assert matching_amount_yuan(tick) == 20200
    assert state["price_milli"] == 10100
    assert state["status"] == "INDICATIVE"
    assert state["match_amount_yuan"] == 20200


def test_tracking_snapshot_retains_unknown_amount_and_limit_coverage():
    from engine_core.auction_tracking import _at_limit, _snapshot

    unknown = {"symbol": "600001", "price_milli": None, "prev_close_milli": 10000,
               "rest_bid_shares": 5, "rest_ask_shares": None,
               "match_amount_yuan": None, "pct_change": None}
    snap = _snapshot({"600001": unknown}, "09:25:00", None, set(), {}, {}, set(), "AUCTION")
    assert snap["market_match_amount_yuan"] is None
    assert _at_limit(unknown, "up") is None
    assert snap["limit_up_count"] == 0
    assert snap["limit_up_unknown_count"] == 1
    assert snap["limit_up_unknown_coverage_pct"] == 100


def test_replay_slices_consume_only_ticks_at_or_before_slice_time():
    def tick(ts, price, volume):
        return Tick("2026-10-14", ts, "600000", pc_milli=10000,
                    bp1_milli=price, ap1_milli=price, bv1=volume, av1=volume)

    report, _ = replay_tracking(trade_date="2026-10-14", ticks=[
        tick("09:15:01", 10000, 10), tick("09:15:31", 10100, 20),
    ])
    first = next(row for row in report["timeline"] if row["timestamp"].endswith("09:15:00"))
    second = next(row for row in report["timeline"] if row["timestamp"].endswith("09:15:30"))
    assert first["stocks"] == []
    assert second["stocks"][0]["price_milli"] == 10000
    assert second["market_match_amount_yuan"] == 10000


def test_plate_share_surge_and_limit_seal_transition_events(monkeypatch):
    import engine_core.auction_tracking as tracking

    monkeypatch.setattr(tracking, "PLATE_SHARE_JUMP_BP", 100)
    monkeypatch.setattr(tracking, "PLATE_MIN_AMOUNT_YUAN", 1)
    monkeypatch.setattr(tracking, "STOCK_MIN_AMOUNT_YUAN", 1)
    monkeypatch.setattr(tracking, "STOCK_AMOUNT_MULTIPLE", 1.5)
    ticks = []
    for i in range(5):
        symbol = f"60000{i}"
        ticks.append(Tick("2026-10-14", "09:19:00", symbol, pc_milli=10000,
                          bp1_milli=10000, ap1_milli=10000, bv1=1, av1=1))
        ticks.append(Tick("2026-10-14", "09:20:00", symbol, pc_milli=10000,
                          bp1_milli=10000, ap1_milli=10000, bv1=1, av1=1))
        ticks.append(Tick("2026-10-14", "09:20:30", symbol, pc_milli=10000,
                          bp1_milli=11000, ap1_milli=11000, bv1=20, av1=20,
                          bv2=1, bp2_milli=11000))
    ticks.extend([
        Tick("2026-10-14", "09:19:00", "601000", pc_milli=10000,
             bp1_milli=10000, ap1_milli=10000, bv1=1, av1=1),
        Tick("2026-10-14", "09:20:00", "601000", pc_milli=10000,
             bp1_milli=10000, ap1_milli=10000, bv1=1, av1=1),
    ])
    report, events = tracking.replay_tracking(trade_date="2026-10-14", ticks=ticks,
        relations={"p": {f"60000{i}" for i in range(5)}}, plate_names={"p": "测试板块"})
    kinds = {event["type"] for event in events}
    assert "PLATE_SHARE_SURGE" in kinds
    assert "STOCK_AMOUNT_SURGE_PRICE_UP" in kinds
    assert "LIMIT_UP_APPEARED" in kinds


def test_auction_events_start_at_092030(monkeypatch):
    import engine_core.auction_tracking as tracking

    monkeypatch.setattr(tracking, "PLATE_SHARE_JUMP_BP", 1)
    monkeypatch.setattr(tracking, "PLATE_MIN_AMOUNT_YUAN", 1)
    ticks = []
    for ts, volume in (("09:20:00", 1), ("09:20:30", 100)):
        for i in range(5):
            ticks.append(Tick("2026-10-14", ts, f"60000{i}", pc_milli=10000,
                              bp1_milli=10000, ap1_milli=10000, bv1=volume, av1=volume))
    _, events = tracking.replay_tracking(trade_date="2026-10-14", ticks=ticks,
        relations={"p": {f"60000{i}" for i in range(5)}}, plate_names={"p": "测试板块"})
    assert events
    assert all(event["timestamp"].endswith(("09:20:30", "09:21:00", "09:21:30", "09:22:00", "09:22:30", "09:23:00", "09:23:30", "09:24:00", "09:24:30", "09:25:00")) for event in events)


def test_event_cooldown_counts_repeats_and_rounds_values():
    from engine_core.auction_tracking import _cooldown_events

    rows = [
        {"timestamp": "2026-10-14T09:21:00", "object_type": "stock", "object": "600000", "type": "X", "values": {"x": 1.23456}},
        {"timestamp": "2026-10-14T09:22:30", "object_type": "stock", "object": "600000", "type": "X", "values": {"x": 2.34567}},
        {"timestamp": "2026-10-14T09:24:01", "object_type": "stock", "object": "600000", "type": "X", "values": {"x": 3.45678}},
    ]
    result = _cooldown_events(rows)
    assert len(result) == 2
    assert result[0]["repeat_count"] == 2
    assert result[0]["values"]["x"] == 1.23
    assert result[1]["repeat_count"] == 1


def test_threshold_constants_and_importance_sort_are_explicit():
    from engine_core.auction_tracking import (_event_importance, _qualifies_plate_share,
        _qualifies_relative, RELATIVE_MOVE_PP, RELATIVE_MIN_AMOUNT_YUAN,
        PLATE_SHARE_JUMP_BP, PLATE_MIN_AMOUNT_YUAN)

    assert (RELATIVE_MOVE_PP, RELATIVE_MIN_AMOUNT_YUAN, PLATE_SHARE_JUMP_BP, PLATE_MIN_AMOUNT_YUAN) == (2.0, 5_000_000, 80, 100_000_000)
    assert _qualifies_plate_share(80, 100_000_000)
    assert not _qualifies_plate_share(79.99, 100_000_000)
    assert _qualifies_relative(2.0, 5_000_000)
    assert not _qualifies_relative(-1.99, 5_000_000)
    assert not _qualifies_relative(2.0, 4_999_999)
    small = {"values": {"share_change_bp": 100, "amount_yuan": 10_000}}
    large = {"values": {"share_change_bp": 100, "amount_yuan": 1_000_000}}
    assert _event_importance(large) > _event_importance(small)


def test_final_print_at_0925_uses_final_price_and_matched_amount():
    # From 09:25:00 the leftover book no longer encodes the match: px_milli/amt_yuan carry final price and amount.
    tick = Tick(
        trade_date="2026-10-14", ts="09:25:00", symbol="002639",
        px_milli=15990, pc_milli=14590, bp1_milli=15990, ap1_milli=15990,
        bv1=1, av1=1, amt_yuan=97_847_600,
    )
    state = derive_state(tick)
    assert state["status"] == "FINAL"
    assert state["price_milli"] == 15990
    assert state["match_amount_yuan"] == 97_847_600
