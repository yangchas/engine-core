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
