"""TD-watermark range repair uses captured Baostock row shapes and queries only missing symbols."""
from __future__ import annotations

import json
from datetime import date

import pytest

from td_sink_support import load_real


def _seed_td_day(store, day, rows):
    from engine_core.data_pipeline.td_sink import map_rows

    mapped, dropped = map_rows(rows, {"provider": "baostock", "field_units": {"volume": "shares"}})
    assert dropped == 0
    version = "seed-" + day
    store.replace_day(day, version, mapped)
    store.write_version({"ts": int(date.fromisoformat(day).strftime("%Y%m%d")) * 1_000_000,
                         "trade_date": day, "version": version, "provider": "baostock", "source": "baostock",
                         "business_status": "FORMAL", "rank_level": 3, "needs_upgrade": False,
                         "row_count": len(mapped), "sha256": "seed-" + day,
                         "available_at": day + "T18:00:00+08:00"})


def test_gapfill_queries_only_lagging_symbol_from_next_session_and_commits_catalog_and_td(tmp_path):
    from engine_core.data_pipeline.daily_kline_gapfill import run_td_daily_kline_gapfill
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from engine_core.data_pipeline.td_sink import MemoryTdStore

    target = "2026-10-23"
    sessions = ["2026-10-21", "2026-10-22", target]
    calendar = tmp_path / "trade_calendar" / "calendar.json"
    calendar.parent.mkdir(parents=True)
    calendar.write_text(json.dumps({"schema": "CoreTradeCalendarMatrixV1", "lit_from": sessions[0],
                                    "lit_to": sessions[-1], "days": [{"d": day, "t": 1} for day in sessions]}),
                        encoding="utf-8")

    _manifest, captured = load_real("formal")
    real = {row["symbol"]: row for row in captured if row["symbol"] in {"000001", "000002"}}
    assert set(real) == {"000001", "000002"}
    store = MemoryTdStore()
    store.ensure_schema()
    for day in sessions[:2]:
        _seed_td_day(store, day, [{**real[symbol], "date": day} for symbol in sorted(real)])
    _seed_td_day(store, target, [{**real["000002"], "date": target}])

    class Result:
        error_code = "0"
        fields = ["code"]

        def __init__(self):
            self.rows = [["sz.000001"], ["sz.000002"]]
            self.index = 0

        def next(self):
            self.index += 1
            return self.index <= len(self.rows)

        def get_row_data(self):
            return self.rows[self.index - 1]

    class Module:
        def query_all_stock(self, day):
            assert day == target
            return Result()

    class RangeClient:
        read_timeout = 5
        module = Module()
        poisoned = False
        logout_error = None

        def __init__(self):
            self.queries = []

        def login(self):
            pass

        def query(self, symbol, start, end):
            self.queries.append((symbol, start, end))
            assert symbol == "000001" and start == target and end == target
            return [{**real[symbol], "date": target}]

        def logout(self):
            return True

    client = RangeClient()
    outcome = run_td_daily_kline_gapfill(tmp_path, target, store=store,
                                         history_min_trading_days=len(sessions), client=client)

    assert outcome["status"] == "WAITING"
    assert outcome["partitions"][0]["business_status"] == "PROVISIONAL"
    assert outcome["range_symbols"] == 1 and outcome["range_queries"] == 1
    assert client.queries == [("000001", target, target)]
    assert store.calls.count("latest_watermarks") == 1
    assert set(store.read_day_rows(target)) == {"000001", "000002"}
    partition = outcome["partitions"][0]
    assert partition["trade_date"] == target and partition["row_count"] == 2 and partition["td_publish"] == "READY"
    entry = ArtifactCatalog(tmp_path).locate("daily_kline", target)
    manifest = json.loads(entry.read_text(encoding="utf-8"))
    assert manifest["provider"] == "baostock" and manifest["version"] == partition["version"]
    assert manifest["sha256"] == partition["sha256"]
    version_row = next(item for item in store.read_versions(target) if item["version"] == partition["version"])
    assert version_row["sha256"] == manifest["sha256"] and version_row["row_count"] == 2


def test_gapfill_with_missing_target_rows_does_not_report_success(tmp_path):
    import pytest

    from engine_core.data_pipeline.daily_kline_gapfill import run_td_daily_kline_gapfill
    from engine_core.data_pipeline.td_sink import MemoryTdStore

    target = "2026-10-23"
    calendar = tmp_path / "trade_calendar" / "calendar.json"
    calendar.parent.mkdir(parents=True)
    calendar.write_text(json.dumps({"schema": "CoreTradeCalendarMatrixV1", "lit_from": target,
                                    "lit_to": target, "days": [{"d": target, "t": 1}]}), encoding="utf-8")

    class Result:
        error_code = "0"
        fields = ["code"]

        def __init__(self):
            self.done = False

        def next(self):
            if self.done:
                return False
            self.done = True
            return True

        def get_row_data(self):
            return ["sz.000001"]

    class Module:
        def query_all_stock(self, day):
            assert day == target
            return Result()

    class RangeClient:
        read_timeout = 5
        module = Module()
        poisoned = False
        logout_error = None

        def login(self):
            pass

        def query(self, symbol, start, end):
            assert (symbol, start, end) == ("000001", target, target)
            return []

        def logout(self):
            return True

    store = MemoryTdStore()
    store.ensure_schema()
    with pytest.raises(RuntimeError, match="BAOSTOCK_GAP_UNRESOLVED"):
        run_td_daily_kline_gapfill(tmp_path, target, store=store,
                                   history_min_trading_days=1, client=RangeClient())


def test_real_td_waterline_with_no_missing_days_returns_no_gaps(tmp_path):
    import gzip

    from engine_core.data_pipeline.daily_kline_gapfill import run_td_daily_kline_gapfill
    from engine_core.data_pipeline.td_sink import MemoryTdStore

    root = __import__("pathlib").Path(__file__).parents[1]
    fixtures = root / "tests" / "fixtures" / "real" / "core_artifacts" / "c594"
    target = "2026-10-28"
    calendar = tmp_path / "trade_calendar" / "calendar.json"
    calendar.parent.mkdir(parents=True)
    calendar.write_text(json.dumps({"schema": "CoreTradeCalendarMatrixV1", "lit_from": target,
                                    "lit_to": target, "days": [{"d": target, "t": 1}]}), encoding="utf-8")
    with gzip.open(fixtures / "daily_kline" / target / "coverage_0986" / "rows.jsonl.gz",
                   "rt", encoding="utf-8") as handle:
        partition_rows = [json.loads(line) for line in handle if line.strip()]
    source_payload = json.loads((root / "tests" / "fixtures" / "real" /
                                 "baostock_query_all_stock_2026-10-28_2301.json").read_text(encoding="utf-8"))

    class Result:
        error_code = "0"
        fields = source_payload["fields"]

        def __init__(self):
            self.rows = source_payload["rows"]
            self.index = -1

        def next(self):
            self.index += 1
            return self.index < len(self.rows)

        def get_row_data(self):
            return self.rows[self.index]

    class Module:
        def query_all_stock(self, *, day):
            assert day == target
            return Result()

    class RangeClient:
        read_timeout = 5
        module = Module()
        poisoned = False

        def __init__(self):
            self.queries = []

        def login(self):
            pass

        def query(self, symbol, start, end):
            self.queries.append((symbol, start, end))
            return []

        def logout(self):
            return True

    store = MemoryTdStore()
    store.ensure_schema()
    _seed_td_day(store, target, partition_rows)
    client = RangeClient()
    outcome = run_td_daily_kline_gapfill(tmp_path, target, store=store,
                                         history_min_trading_days=1, client=client)

    assert outcome["status"] == "NO_GAPS"
    assert outcome["range_symbols"] == 0 and outcome["range_queries"] == 0
    assert client.queries == []


@pytest.mark.parametrize("only_source_empty", [True, False], ids=["late-source-empty", "waterline-backfill"])
@pytest.mark.parametrize("coverage_variant", ["coverage_0986", "coverage_0668"])
def test_real_partial_catalog_waterline_requests_only_source_empty_symbols(
        tmp_path, only_source_empty, coverage_variant):
    import gzip

    import pytest

    from engine_core.data_pipeline.daily_kline_gapfill import run_td_daily_kline_gapfill
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from engine_core.data_pipeline.td_sink import MemoryTdStore

    root = __import__("pathlib").Path(__file__).parents[1]
    fixtures = root / "tests" / "fixtures" / "real" / "core_artifacts" / "c594"
    target = "2026-10-28"
    sessions = ["2026-10-27", target]
    calendar = tmp_path / "trade_calendar" / "calendar.json"
    calendar.parent.mkdir(parents=True)
    calendar.write_text(json.dumps({"schema": "CoreTradeCalendarMatrixV1", "lit_from": sessions[0],
                                    "lit_to": sessions[-1], "days": [{"d": day, "t": 1} for day in sessions]}),
                        encoding="utf-8")

    def rows(path):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    catalog = ArtifactCatalog(tmp_path)
    universe_rows = rows(fixtures / "symbol_universe" / target / "baostock_query_all_stock" / "rows.jsonl.gz")
    partition_rows = rows(fixtures / "daily_kline" / target / coverage_variant / "rows.jsonl.gz")
    catalog.commit("symbol_universe", target, universe_rows,
                   {"trade_date": target, "as_of": target, "source_asof": target}, source="baostock")
    catalog.commit("daily_kline", target, partition_rows,
                   {"trade_date": target, "as_of": target, "source_asof": target,
                    "provider": "baostock", "business_status": "PARTIAL"}, source="baostock")

    source_payload = json.loads((root / "tests" / "fixtures" / "real" /
                                 "baostock_query_all_stock_2026-10-28_2301.json").read_text(encoding="utf-8"))
    code_index = source_payload["fields"].index("code")
    source_symbols = {str(row[code_index]).rsplit(".", 1)[-1] for row in source_payload["rows"]}
    active_symbols = {str(row["symbol"]).zfill(6) for row in universe_rows
                      if str(row.get("trade_status", "")).strip() != "0"}
    partition_symbols = {str(row["symbol"]).zfill(6) for row in partition_rows} & active_symbols
    missing = active_symbols - partition_symbols
    expected_gap = {symbol for symbol in missing if symbol in source_symbols}
    if coverage_variant == "coverage_0986":
        assert len(expected_gap) == 73
    else:
        assert len(expected_gap) == 1733

    class Result:
        error_code = "0"
        fields = source_payload["fields"]

        def __init__(self):
            self.rows = source_payload["rows"]
            self.index = -1

        def next(self):
            self.index += 1
            return self.index < len(self.rows)

        def get_row_data(self):
            return self.rows[self.index]

    class Module:
        def query_all_stock(self, *, day):
            assert day == target
            return Result()

    class RangeClient:
        read_timeout = 5
        module = Module()
        poisoned = False

        def __init__(self):
            self.queries = []

        def login(self):
            pass

        def query(self, symbol, start, end):
            self.queries.append((symbol, start, end))
            return []

        def logout(self):
            return True

    client = RangeClient()
    store = MemoryTdStore()
    store.ensure_schema()
    outcome = run_td_daily_kline_gapfill(tmp_path, target, store=store, history_min_trading_days=1,
                                         client=client, only_source_empty=only_source_empty,
                                         minimum_coverage=0.90,
                                         completion_policy={"formal_coverage_threshold": 0.995,
                                                            "source_empty_max": 20})

    requested = {symbol for symbol, start, end in client.queries if start == target and end == target}
    if coverage_variant == "coverage_0668":
        assert requested == set()
        assert client.queries == []
        assert outcome["status"] == "NOT_ELIGIBLE"
        assert outcome["range_symbols"] == 0 and outcome["range_queries"] == 0
        return
    assert requested == expected_gap, (
        f"requested={len(requested)} expected={len(expected_gap)} "
        f"unexpected={sorted(requested - expected_gap)[:8]} missing={sorted(expected_gap - requested)[:8]}"
    )
    assert len(client.queries) == len(expected_gap)
    assert outcome["status"] == "WAITING"
    assert outcome["range_symbols"] == len(expected_gap)
    assert outcome["range_queries"] == len(expected_gap)
    assert outcome["remaining_missing"] == 74
    assert outcome["remaining_source_empty"] == 73
    assert outcome["partitions"][0]["business_status"] == "PARTIAL"
    assert outcome["partitions"][0]["td_publish"] == "SKIPPED_PARTIAL"
