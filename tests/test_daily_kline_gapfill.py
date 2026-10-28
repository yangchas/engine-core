"""TD-watermark range repair uses captured Baostock row shapes and queries only missing symbols."""
from __future__ import annotations

import json
from datetime import date

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

    assert outcome["status"] == "OK" and outcome["range_symbols"] == 1 and outcome["range_queries"] == 1
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
