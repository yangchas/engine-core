from engine_core.plate_amount_baseline import build_plate_amount_baseline


def test_plate_amount_baseline_uses_each_dates_members_and_never_zero_fills_missing():
    memberships = {
        "2026-10-14": [{"plate_code": "p", "symbol": "000001"},
                       {"plate_code": "p", "symbol": "000002"}],
        "2026-10-15": [{"plate_code": "p", "symbol": "000001"}],
    }
    observations = [
        {"trade_date": "2026-10-14", "slot": "0925", "symbol": "000001", "amount_yuan": 10},
        {"trade_date": "2026-10-14", "slot": "0925", "symbol": "000002", "amount_yuan": 20},
        {"trade_date": "2026-10-15", "slot": "0925", "symbol": "000001", "amount_yuan": 30},
    ]
    result = build_plate_amount_baseline(
        target_date="2026-10-16", membership_by_date=memberships,
        observations=observations, candidate_dates=["2026-10-14", "2026-10-15"],
    )
    row = next(r for r in result["rows"] if r["plate_code"] == "p" and r["slot"] == "0925")
    assert row["amounts_yuan"] == [30, 30]
    assert row["dates_used"] == ["2026-10-14", "2026-10-15"]
    assert row["sample_days"] == 2
    assert row["median_yuan"] == 30
    assert row["missing_dates"] == []


def test_plate_amount_baseline_marks_missing_member_version_and_amount_date():
    result = build_plate_amount_baseline(
        target_date="2026-10-16",
        membership_by_date={"2026-10-14": [{"plate_code": "p", "symbol": "000001"}]},
        observations=[], candidate_dates=["2026-10-14", "2026-10-15"],
    )
    row = next(r for r in result["rows"] if r["plate_code"] == "p" and r["slot"] == "0925")
    assert row["amounts_yuan"] == []
    assert row["sample_days"] == 0
    assert row["missing_dates"] == ["2026-10-14", "2026-10-15"]
