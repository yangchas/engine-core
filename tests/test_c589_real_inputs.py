"""C589: captured WenCai JSON and Redis q2 snapshots; live read-only contracts."""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WENCAI_FIX = ROOT / "tests" / "fixtures" / "wencai_raw"
Q2_FIX = ROOT / "tests" / "fixtures" / "q2_snapshot"
TZ = timezone(timedelta(hours=8))


def _wencai_capture(day="2026-10-27"):
    index_path = WENCAI_FIX / "PROVENANCE.json"
    if not index_path.exists():
        pytest.skip("CAPTURE_INCOMPLETE: no raw WenCai capture is recorded")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    capture = index["captures"][day]
    if capture.get("status") != "complete":
        pytest.skip("CAPTURE_INCOMPLETE: raw WenCai pagination did not complete")
    pages = {
        int(page): json.loads((WENCAI_FIX / item["file"]).read_text(encoding="utf-8"))
        for page, item in capture["files"].items()
    }
    return capture, pages


def _q2_snapshot():
    index = json.loads((Q2_FIX / "PROVENANCE.json").read_text(encoding="utf-8"))
    capture = max(index["snapshots"].values(), key=lambda item: item["captured_at"])
    with gzip.open(Q2_FIX / capture["file"], "rt", encoding="utf-8") as handle:
        snapshot = json.load(handle)
    return capture, snapshot


class _SnapshotRedis:
    def __init__(self, snapshot):
        self.symbols = snapshot["active_symbols"]
        self.hashes = snapshot["hashes"]
        self.commands = []

    def smembers(self, key):
        self.commands.append(("smembers", key))
        if key in {"q2:active:" + self.snapshot_date, "q2:active:" + self.snapshot_date.replace("-", "")}:
            return set(self.symbols)
        return set()

    @property
    def snapshot_date(self):
        return self._snapshot_date

    @snapshot_date.setter
    def snapshot_date(self, value):
        self._snapshot_date = value

    def hgetall(self, key):
        self.commands.append(("hgetall", key))
        return dict(self.hashes.get(key.removeprefix("q2:"), {}))

    def pipeline(self, transaction=False):
        outer = self

        class Pipeline:
            def __init__(self):
                self.keys = []

            def hgetall(self, key):
                self.keys.append(key)

            def execute(self):
                return [outer.hgetall(key) for key in self.keys]

        return Pipeline()


def _snapshot_redis(snapshot):
    client = _SnapshotRedis(snapshot)
    client.snapshot_date = snapshot["trade_date"]
    return client


class _ReadOnlyRedis:
    """Expose only the Redis read calls used by q2 providers and record their names."""
    def __init__(self, client):
        self.client = client
        self.commands = []

    def smembers(self, key):
        self.commands.append("SMEMBERS")
        return self.client.smembers(key)

    def hgetall(self, key):
        self.commands.append("HGETALL")
        return self.client.hgetall(key)

    def pipeline(self, transaction=False):
        outer = self
        pipe = self.client.pipeline(transaction=transaction)

        class Pipeline:
            def hgetall(self, key):
                outer.commands.append("HGETALL")
                pipe.hgetall(key)

            def execute(self):
                return pipe.execute()

        return Pipeline()


def test_wencai_raw_pages_replay_through_daily_kline_adapter():
    from pywencai import wencai as pywencai
    from engine_core.data_pipeline.daily_kline_schema import canonicalize_daily_kline_row
    from engine_core.data_pipeline.providers import wencai_daily_adapter as adapter
    from engine_core.data_pipeline.providers.daily_kline_chain import WencaiDailyProvider
    from engine_core.wencai_fallback import _number, _symbol

    day = "2026-10-27"
    capture, raw_pages = _wencai_capture(day)
    expected_pages = int(capture["total_pages"])
    assert set(raw_pages) == {1, 2, 3, expected_pages}
    assert expected_pages > 3 and capture["page_size"] == 100
    base_query = adapter.build_daily_kline_query(day)
    assert capture["query"] == base_query + ";股票代码小于600000"

    def table_rows(page):
        rows, has_table, _ = pywencai._extract_table(raw_pages[page])
        assert has_table and rows
        return rows

    first_rows = table_rows(1)
    headers = [str(key) for key in first_rows[0]]
    assert any("收盘价" in key and key.endswith("[20261027]") for key in headers)
    assert any("成交量" in key and "股" in key and key.endswith("[20261027]") for key in headers)
    assert any("成交额" in key and "元" in key and key.endswith("[20261027]") for key in headers)
    last_rows = table_rows(expected_pages)
    assert 1 <= len(last_rows) <= 100

    calls = []

    def replay_raw_page(**kwargs):
        calls.append(kwargs)
        assert kwargs["query"] == base_query
        assert kwargs["perpage"] == adapter.PAGE_SIZE == 100
        assert kwargs["page"] in {1, 2, 3}
        rows = table_rows(kwargs["page"])
        import pandas as pd
        return {"data": pd.DataFrame.from_records(rows), "row_count": capture["total_rows"],
                "payload": raw_pages[kwargs["page"]]}

    payload = adapter.fetch_daily_kline(
        day, query_fn=replay_raw_page, page_size=adapter.PAGE_SIZE, max_pages=3,
        max_total_requests=3, segment_enabled=False, sleep_fn=lambda _seconds: None,
    )
    replayed_raw = [row for page in (1, 2, 3) for row in table_rows(page)]
    expected_symbols = {_symbol(row) for row in replayed_raw} - {None}
    assert [call["page"] for call in calls] == [1, 2, 3]
    assert all(call["perpage"] == 100 for call in calls)
    assert len(payload["rows"]) == len(expected_symbols)
    assert payload["expected_count"] == capture["total_rows"]
    assert payload["partial"] is True and payload["page_count"] == 3
    assert payload["source_dates_consistent"] is True
    assert payload["source_dates"] == {field: day for field in (
        "amount_yuan", "open", "close", "high", "low", "pct_chg", "pre_close", "volume")}
    assert payload["field_units"]["volume"] == "shares"
    assert payload["field_units"]["amount_yuan"] == "yuan"

    source_row = first_rows[0]
    normalized = WencaiDailyProvider._normalize_rows([source_row], day)[0]
    canonical = canonicalize_daily_kline_row("wencai", normalized, field_units=payload["field_units"])
    assert canonical["date"] == day and canonical["symbol"] == _symbol(source_row)
    assert canonical["volume"] == _number(adapter._field_value(source_row, "volume", day))
    assert canonical["amount_yuan"] == _number(adapter._field_value(source_row, "amount_yuan", day))


def test_q2_snapshot_drives_q2_final_and_intraday_checkpoint():
    from engine_core.data_pipeline import intraday_checkpoint as checkpoint
    from engine_core.data_pipeline.providers.daily_kline_chain import Q2FinalProvider

    capture, snapshot = _q2_snapshot()
    day = snapshot["trade_date"]
    redis = _snapshot_redis(snapshot)
    final = Q2FinalProvider(redis).fetch(day, now=f"{day}T15:05:00+08:00")
    assert final["expected_count"] == len(snapshot["active_symbols"])
    assert final["source_field_units"]["volume"] == "hands"
    assert final["rows"]
    final_row = final["rows"][0]
    raw = snapshot["hashes"][final_row["symbol"]]
    assert final_row["source"] == "q2_final" and final_row["date"] == day
    assert final["degraded"] is True and final["tier"] == 2
    assert final_row["close"] == pytest.approx(float(raw["px"]) / 1000)
    assert final_row["volume"] == pytest.approx(float(raw["vol"]) * 100)
    assert final_row["amount_yuan"] == pytest.approx(float(raw["amt"]))

    symbols, raw_by_symbol = checkpoint.read_q2(redis, day)
    sample_symbol = next(symbol for symbol in symbols if checkpoint.row_time(raw_by_symbol[symbol]))
    observed = checkpoint.row_time(raw_by_symbol[sample_symbol])
    part = checkpoint.build_intraday_checkpoint({
        "trade_date": day, "cutoff": observed.strftime("%H:%M:%S"), "tolerance_seconds": 60,
        "min_symbols": 1, "symbols": symbols, "raw": raw_by_symbol,
        "read_at": capture["captured_at"],
    })
    checkpoint_row = next(row for row in part.rows if row["symbol"] == sample_symbol)
    assert checkpoint_row["date"] == day
    assert checkpoint_row["volume_hands"] == pytest.approx(float(raw_by_symbol[sample_symbol]["vol"]))
    assert checkpoint_row["price"] == pytest.approx(float(raw_by_symbol[sample_symbol]["px"]) / 1000)


@pytest.mark.live
def test_q2_live_readonly_contract():
    import os
    import redis as redis_lib
    from engine_core.data_pipeline import intraday_checkpoint as checkpoint
    from engine_core.data_pipeline.providers.daily_kline_chain import Q2FinalProvider

    if "REDIS_URL" not in os.environ:
        pytest.skip("UPSTREAM_UNREACHABLE: Redis URL is unavailable")
    day = datetime.now(TZ).date().isoformat()
    client = redis_lib.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True,
                                      socket_connect_timeout=5, socket_timeout=5)
    read_only = _ReadOnlyRedis(client)
    try:
        symbols, raw = checkpoint.read_q2(read_only, day)
    except Exception:
        client.close()
        pytest.skip("UPSTREAM_UNREACHABLE: Redis read-only q2 probe failed")
    try:
        if not symbols:
            pytest.skip("UPSTREAM_UNREACHABLE: current q2 active set is empty")
        result = Q2FinalProvider(read_only).fetch(day, now=datetime.now(TZ))
        assert result["expected_count"] == len(symbols)
        assert read_only.commands and set(read_only.commands) <= {"SMEMBERS", "HGETALL"}
        sample = next((s for s in symbols if raw.get(s)), None)
        assert sample is not None
        observed = checkpoint.row_time(raw[sample])
        assert observed is not None
        part = checkpoint.build_intraday_checkpoint({
            "trade_date": day, "cutoff": observed.strftime("%H:%M:%S"), "tolerance_seconds": 60,
            "min_symbols": 1, "symbols": symbols, "raw": raw,
            "read_at": datetime.now(TZ).isoformat(timespec="seconds"),
        })
        assert any(row["symbol"] == sample for row in part.rows)
        print(json.dumps({"trade_date": day, "active_symbols": len(symbols),
                          "q2_final_rows": len(result["rows"]), "checkpoint_rows": len(part.rows),
                          "readonly_commands": len(read_only.commands)}, sort_keys=True))
    finally:
        client.close()


@pytest.mark.live
def test_kaipan_live_limit_pool_and_historical_streak_contract():
    import importlib
    from engine_core.data_pipeline.limit_pool import build_kaipan
    from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider

    day = "2026-10-27"
    sdk = importlib.import_module("pykaipan.pykaipan")

    class Client:
        def get_his_bans(self, trade_date, level):
            return sdk.getHisBans(date=trade_date, ban=str(level), size="2000", index="0")

    provider = KaipanProvider(Client())
    try:
        result = provider.limit_up_pool(day, max_levels=10)
    except RuntimeError as exc:
        if "request_failed" in str(exc) or "provider_blocked" in str(exc):
            pytest.skip("UPSTREAM_UNREACHABLE: Kaipan read-only request failed")
        raise
    assert result["request_count"] > 0 and result["rows"]
    assert result["pages"]
    assert {page["date_echo"] for page in result["pages"].values()} == {day}
    part = build_kaipan(day, result["pages"])
    assert part.rows
    assert part.notes["lb_days_source"] == "record field [15], ladder level when empty"
    assert all(row["trade_date"] == day and row["lb_days"] >= 1 for row in part.rows)
    print(json.dumps({"trade_date": day, "request_count": result["request_count"],
                      "levels": sorted(result["pages"]), "pool_rows": len(result["rows"]),
                      "partition_rows": len(part.rows)}, sort_keys=True))
