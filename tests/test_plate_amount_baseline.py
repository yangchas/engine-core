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


def test_tick_amounts_come_from_a_few_sliced_queries_not_one_per_day_and_not_one_for_everything(monkeypatch):
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
    assert len(tick_queries) == 6 and len(log) == 12                  # 26 days / 5 per slice = 6 tick slices and 6 auction slices
    assert all("INTERVAL(15m, 1s)" in sql and "PARTITION BY symbol" in sql and "LAST(amt_yuan)" in sql
               for sql in tick_queries)
    assert '"2026-10-01 09:30:00"' in tick_queries[0] and '"2026-10-30 09:45:00"' in tick_queries[-1]
    slot = sorted((r["trade_date"], r["symbol"], r["amount_yuan"]) for r in rows if r["slot"] == "0945")
    # the fake answers every slice with the same ticks, so each requested day inside it is reported once per slice
    assert {item for item in slot} == {("2026-10-29", "000001", 400), ("2026-10-29", "600519", 900),
                                       ("2026-10-30", "000001", 410)}
    assert sorted((r["trade_date"], r["amount_yuan"]) for r in rows if r["slot"] == "0925") == [
        ("2026-10-29", 111), ("2026-10-30", 222)]


class _MemoryExhaustedAbove:
    """Cursor whose tick query fails with TDengine's 'Query memory exhausted' when a slice spans too many days."""

    def __init__(self, limit_days, log):
        self.limit_days, self.log, self.sql = limit_days, log, ""

    def execute(self, sql):
        self.sql = sql
        if "stock_tick_v2" in sql:
            first, last = sql.split('ts >= "')[1][:10], sql.split('ts <= "')[1][:10]
            self.log.append((first, last))
            span = int(last[8:]) - int(first[8:]) + 1
            if span > self.limit_days:
                raise RuntimeError("[0x073a]: Query memory exhausted")

    def fetchall(self):
        if "auction_snapshot_v2" in self.sql:
            return []
        first, last = self.log[-1]
        return [(f"{first[:8]}{day:02d} 09:30:01", "000001", day)
                for day in range(int(first[8:]), int(last[8:]) + 1)]


def test_a_slice_that_runs_out_of_query_memory_is_split_and_retried(monkeypatch):
    from engine_core import plate_amount_baseline as module

    log: list = []
    connection = type("C", (), {"cursor": lambda self: _MemoryExhaustedAbove(2, log), "close": lambda self: None})()
    monkeypatch.setattr(module, "_td_connect", lambda: connection)
    dates = [f"2026-10-{day:02d}" for day in range(1, 6)]            # one slice of 5 days, limit 2 days per query
    rows = module._read_td("2026-11-02", dates)
    assert sorted(r["trade_date"] for r in rows) == dates            # nothing lost by splitting
    spans = [int(last[8:]) - int(first[8:]) + 1 for first, last in log]
    assert spans[0] == 5                                              # the whole slice was tried first
    assert sum(span for span in spans if span <= 2) == 5              # the queries that fit cover each day exactly once


def test_other_tick_query_errors_are_not_swallowed(monkeypatch):
    from engine_core import plate_amount_baseline as module

    class Broken(_MemoryExhaustedAbove):
        def execute(self, sql):
            if "stock_tick_v2" in sql:
                raise RuntimeError("[0x2603]: Table does not exist")
            super().execute(sql)

    connection = type("C", (), {"cursor": lambda self: Broken(99, []), "close": lambda self: None})()
    monkeypatch.setattr(module, "_td_connect", lambda: connection)
    import pytest
    with pytest.raises(RuntimeError, match="Table does not exist"):
        module._read_td("2026-11-02", ["2026-10-01", "2026-10-02"])


def test_no_dates_means_no_td_connection(monkeypatch):
    from engine_core import plate_amount_baseline as module

    monkeypatch.setattr(module, "_td_connect", lambda: (_ for _ in ()).throw(AssertionError("must not connect")))
    assert module._read_td("2026-11-02", []) == []


def test_a_single_day_that_still_runs_out_of_memory_is_split_by_symbol_range(monkeypatch):
    from engine_core import plate_amount_baseline as module

    class OneDayTooBig:
        def __init__(self):
            self.sql, self.queries = "", []

        def execute(self, sql):
            self.sql = sql
            if "stock_tick_v2" in sql:
                self.queries.append(sql)
                if "symbol >=" not in sql:
                    raise RuntimeError("[0x073a]: Query memory exhausted")

        def fetchall(self):
            if "auction_snapshot_v2" in self.sql:
                return []
            low = self.sql.split('symbol >= "')[1][:6]
            symbol = {"000000": "000001", "300000": "300750", "600000": "600519"}[low]
            return [("2026-10-30 09:30:01", symbol, 100)]

    cursor = OneDayTooBig()
    connection = type("C", (), {"cursor": lambda self: cursor, "close": lambda self: None})()
    monkeypatch.setattr(module, "_td_connect", lambda: connection)
    rows = module._read_td("2026-11-02", ["2026-10-30"])
    assert sorted(r["symbol"] for r in rows if r["slot"] == "0945") == ["000001", "300750", "600519"]
    assert len(cursor.queries) == 4                                   # the whole day first, then three symbol ranges


def test_a_failed_tick_slice_names_its_stage_and_range(monkeypatch):
    from engine_core import plate_amount_baseline as module
    import pytest

    class Broken:
        def execute(self, sql):
            if "stock_tick_v2" in sql:
                raise RuntimeError("[0x2603]: Table does not exist")

        def fetchall(self):
            return []

    connection = type("C", (), {"cursor": lambda self: Broken(), "close": lambda self: None})()
    monkeypatch.setattr(module, "_td_connect", lambda: connection)
    with pytest.raises(RuntimeError, match=r"plate_amount_baseline_tick_slice 2026-10-01\.\.2026-10-02.*Table does not exist"):
        module._read_td("2026-11-02", ["2026-10-01", "2026-10-02"])


def test_an_auction_slice_that_runs_out_of_query_memory_is_halved(monkeypatch):
    from engine_core import plate_amount_baseline as module

    class Cursor:
        def __init__(self):
            self.sql, self.queries = "", []

        def execute(self, sql):
            self.sql = sql
            if "auction_snapshot_v2" in sql:
                self.queries.append(sql)
                if sql.count('"2026') > 2 or sql.count('"2026') == 0 and sql.count(",") > 1:
                    raise RuntimeError("[0x073a]: Query memory exhausted")

        def fetchall(self):
            if "auction_snapshot_v2" not in self.sql:
                return []
            days = [part.strip('"') for part in self.sql.split("IN (")[1].split(")")[0].split(",")]
            return [(f"{day[:4]}-{day[4:6]}-{day[6:]} 09:25:06", "000001", day, "0925", int(day[6:]))
                    for day in days]

    cursor = Cursor()
    connection = type("C", (), {"cursor": lambda self: cursor, "close": lambda self: None})()
    monkeypatch.setattr(module, "_td_connect", lambda: connection)
    dates = [f"2026-10-{day:02d}" for day in range(1, 6)]
    rows = module._read_td("2026-11-02", dates)
    assert sorted(r["trade_date"] for r in rows if r["slot"] == "0925") == dates   # nothing lost by halving
    assert len(cursor.queries) > 1 and all("ORDER BY" not in sql for sql in cursor.queries)
