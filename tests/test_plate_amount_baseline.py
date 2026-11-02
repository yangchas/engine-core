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


class _FakeCursor:
    def __init__(self, log, tick_rows):
        self.log, self.tick_rows, self.sql = log, tick_rows, ""

    def execute(self, sql):
        self.sql = sql
        self.log.append(sql)

    def fetchall(self):
        if "auction_snapshot_v2" in self.sql:
            return [("2026-10-29 09:25:06", "000001", "20261029", "0925", 111),
                    ("2026-10-30 09:25:07", "000001", "20261030", "0925", 222)]
        return self.tick_rows


class _FakeConnection:
    def __init__(self, log, tick_rows):
        self.log, self.tick_rows = log, tick_rows

    def cursor(self):
        return _FakeCursor(self.log, self.tick_rows)

    def close(self):
        pass


def test_tick_amounts_for_the_whole_window_come_from_one_sliced_query(monkeypatch):
    from engine_core import plate_amount_baseline as module

    dates = [f"2026-10-{day:02d}" for day in range(1, 25)]            # 24 trading days
    ticks = [("2026-10-29 09:15:01", "000001", 5),                    # other slices of the same day are ignored
             ("2026-10-29 09:30:01", "000001", 400), ("2026-10-29 09:30:01", "600519", 900),
             ("2026-10-29 09:45:01", "000001", 450),
             ("2026-10-30 09:30:01", "000001", 410), ("2026-10-30 09:30:01", "688981", None),   # None never becomes 0
             ("2026-11-05 09:30:01", "000001", 7)]                    # outside the requested dates
    log: list[str] = []
    monkeypatch.setattr(module, "_td_connect", lambda: _FakeConnection(log, ticks))
    rows = module._read_td("2026-11-02", dates + ["2026-10-29", "2026-10-30"])
    tick_queries = [sql for sql in log if "stock_tick_v2" in sql]
    assert len(tick_queries) == 1 and len(log) == 2                   # one auction query + one tick query, not 1 + 26
    sql = tick_queries[0]
    assert "INTERVAL(15m, 1s)" in sql and "PARTITION BY symbol" in sql and "LAST(amt_yuan)" in sql
    assert '"2026-10-01 09:30:00"' in sql and '"2026-10-30 09:45:00"' in sql
    slot = sorted((r["trade_date"], r["symbol"], r["amount_yuan"]) for r in rows if r["slot"] == "0945")
    assert slot == [("2026-10-29", "000001", 400), ("2026-10-29", "600519", 900), ("2026-10-30", "000001", 410)]
    assert sorted((r["trade_date"], r["amount_yuan"]) for r in rows if r["slot"] == "0925") == [
        ("2026-10-29", 111), ("2026-10-30", 222)]


def test_no_dates_means_no_td_connection(monkeypatch):
    from engine_core import plate_amount_baseline as module

    monkeypatch.setattr(module, "_td_connect", lambda: (_ for _ in ()).throw(AssertionError("must not connect")))
    assert module._read_td("2026-11-02", []) == []
